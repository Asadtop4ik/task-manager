"""WP7: an independent, read-only Codex review of a leased pull request head.

Mirrors `scripts/agent_pr_review.py` (`prepare` / `finalize` / `fail`) exactly,
adapted to the leased-work model. Unlike that legacy GitHub-Actions script --
which is also triggered for PRs with no agent run behind them at all, and
whose `fail()` step is a no-op unless `prepare()` had already written its
target file -- every review `Work` item handled here already carries an
active `run_id` / `lease_id`, so any failure past the initial PR fetch gets
the same real "fail()" treatment: an error review result plus an error commit
status. A pre-run stale/closed head is a variant of that: it posts an error
review result (not a status), specifically because the backend releases the
review lease as a side effect of that exact call when the head no longer
matches -- see `_release_stale_head_lease` -- rather than leaving it to expire
on a timer. Only the lease being lost mid-run posts nothing at all (posting
after that would race whatever reclaimed the lease).

`ctx` (an `agent_svc.main.ServiceContext`, accepted here as `Any` so this
module never has to import that not-yet-stable type) must expose:
`settings` (`config.Settings`), `logger` (`log.Logger`), `api`
(`api.TaskManagerApi`), `github` (`github.GitHubClient`), `codex`
(`codex.CodexRunner`), `trusted.agent_pr_review` (the trusted
`scripts/agent_pr_review.py` module, loaded by file path from
`settings.trusted_dir` -- never imported from a repo checkout or worktree),
and `catalog` (`repos.Catalog`).
"""

from __future__ import annotations

import shutil
import threading
from typing import Any

from . import repos
from .api import LeaseLost, TaskManagerApi, Work
from .github import GitHubClient
from .log import Logger

REVIEW_LANE = "code"
REVIEW_MODEL_KEY = "review"
STATUS_CONTEXT = "codex-review"
MAX_DIFF_CHARS = 250_000
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

# The exact wording legacy's `fail()` uses for every failure it reports.
GENERIC_FAILURE_SUMMARY = (
    "Independent review failed to complete; inspect the review workflow logs."
)

# The JSON shape `_parse_result` (in the trusted script) requires, passed to
# Codex as `--output-schema` so its final message already matches it.
REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
                "required": ["severity", "title", "evidence"],
            },
        },
    },
    "required": ["summary", "findings"],
}


def handle_review(ctx: Any, work: Work, cancel: threading.Event) -> None:
    logger: Logger = ctx.logger
    api: TaskManagerApi = ctx.api
    github: GitHubClient = ctx.github
    trusted = ctx.trusted.agent_pr_review
    repo = work.repo_full_name

    if work.pr_number is None or work.head_sha is None:
        _stage_error(api, work, logger, "review work is missing pr_number/head_sha")
        return

    run_dir = repos.make_run_dir(ctx.settings.work_root, work.run_id)
    try:
        try:
            pr = github.get_pull(repo, work.pr_number)
        except Exception as exc:
            logger.error(exc, event="review_get_pull_failed", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return

        if not _pr_is_current(trusted, pr, work):
            logger.event(
                "review_stale_head",
                level="warning",
                run_id=work.run_id,
                task_id=work.task_id,
            )
            _release_stale_head_lease(api, work, logger)
            return

        try:
            diff = github.pull_diff(repo, work.pr_number)
        except Exception as exc:
            logger.error(exc, event="review_diff_fetch_failed", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return
        if not diff or len(diff) > MAX_DIFF_CHARS:
            logger.event(
                "review_diff_too_large",
                level="warning",
                run_id=work.run_id,
                task_id=work.task_id,
                chars=len(diff),
            )
            _post_error_review(api, github, work, logger)
            return

        prompt = trusted.build_review_prompt(repo, work.pr_number, work.head_sha, diff)

        try:
            api.stage(work.run_id, work.lease_id, "review_started")
        except LeaseLost as exc:
            logger.event("lease_lost", level="warning", run_id=work.run_id, detail=exc.detail)
            cancel.set()
            return

        model_config = ctx.settings.model_matrix.get(REVIEW_MODEL_KEY, {})
        model = model_config.get("model", "gpt-6-sol")
        effort = model_config.get("effort", "medium")
        timeout_s = ctx.settings.timeouts.get(REVIEW_MODEL_KEY, 480)

        request: dict[str, Any] = {
            "run_id": work.run_id,
            "lane": REVIEW_LANE,
            "cwd": "empty",
            "model": model,
            "effort": effort,
            "sandbox": "read-only",
            "multi_agent": False,
            "prompt": prompt,
            "images": [],
            "output_schema": REVIEW_OUTPUT_SCHEMA,
            "timeout_s": float(timeout_s),
            "idle_timeout_s": float(ctx.settings.idle_timeout_s),
        }

        def _on_event(_event: dict[str, Any]) -> None:
            return  # forwarded only for counters/logging; content is never logged

        def _heartbeat() -> bool:
            if cancel.is_set():
                return False
            try:
                api.heartbeat(work.run_id, work.lease_id)
            except LeaseLost as exc:
                logger.event(
                    "lease_lost", level="warning", run_id=work.run_id, detail=exc.detail
                )
                cancel.set()
                return False
            except Exception as exc:
                logger.error(exc, event="heartbeat_failed", run_id=work.run_id)
            return True

        try:
            result = ctx.codex.run_exec(
                request,
                on_event=_on_event,
                heartbeat=_heartbeat,
                heartbeat_interval_s=DEFAULT_HEARTBEAT_INTERVAL_S,
                cancel=cancel,
            )
        except Exception as exc:
            logger.error(exc, event="review_codex_exec_raised", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return

        if result.cancelled or cancel.is_set():
            # The lease was lost (or the run was cancelled for some other
            # reason) mid-run: whoever reclaimed the lease owns the outcome
            # now, so post nothing.
            logger.event("review_cancelled", level="warning", run_id=work.run_id)
            return

        if result.exit_code != 0 or result.timed_out or result.idle_killed:
            logger.event(
                "review_codex_failed",
                level="error",
                run_id=work.run_id,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                idle_killed=result.idle_killed,
            )
            _post_error_review(api, github, work, logger)
            return

        result_path = run_dir / "review-result.json"
        try:
            result_path.write_text(result.final_message, encoding="utf-8")
            summary, findings = trusted._parse_result(result_path)
        except Exception as exc:
            logger.error(exc, event="review_parse_failed", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return

        try:
            pr_again = github.get_pull(repo, work.pr_number)
        except Exception as exc:
            logger.error(exc, event="review_recheck_failed", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return
        if not _pr_is_current(trusted, pr_again, work):
            # The head moved (or the PR closed) while Codex was reviewing it.
            # Unlike the pre-run check, `finalize()`'s own re-check already
            # ran with a target file on disk, so legacy's `fail()` step DOES
            # publish here -- mirror that.
            logger.event("review_stale_head_after_run", level="warning", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return

        state, is_ready, description = trusted._review_decision(findings)

        try:
            api.review_result(
                work.run_id,
                work.lease_id,
                {
                    "sha": work.head_sha,
                    "state": state,
                    "summary": summary,
                    "findings": findings,
                },
            )
        except LeaseLost as exc:
            logger.event("lease_lost", level="warning", run_id=work.run_id, detail=exc.detail)
            return
        except Exception as exc:
            # The backend never recorded this outcome, so the real
            # success/failure status and the dispatch below must not run
            # either -- report the same error the backend would see as
            # missing, exactly like legacy's `fail()`.
            logger.error(exc, event="review_result_post_failed", run_id=work.run_id)
            _post_error_review(api, github, work, logger)
            return

        try:
            github.set_status(
                repo,
                work.head_sha,
                "success" if is_ready else "failure",
                STATUS_CONTEXT,
                description,
                target_url=None,
            )
        except Exception as exc:
            logger.error(exc, event="review_status_post_failed", run_id=work.run_id)

        if repo == ctx.catalog.dispatch_repo:
            try:
                github.dispatch(
                    repo,
                    "agent_review_completed",
                    {"pull_number": work.pr_number, "head_sha": work.head_sha},
                )
            except Exception as exc:
                logger.error(exc, event="review_dispatch_failed", run_id=work.run_id)
    finally:
        try:
            ctx.codex.cleanup({"run_id": work.run_id})
        except Exception as exc:
            logger.error(exc, event="review_cleanup_failed", run_id=work.run_id)
        shutil.rmtree(run_dir, ignore_errors=True)


def _pr_is_current(trusted: Any, pr: dict[str, Any], work: Work) -> bool:
    """`trusted._current_pr` plus the head-branch check legacy's `prepare()` adds."""
    if not trusted._current_pr(pr, work.repo_full_name, work.head_sha):
        return False
    if work.branch is not None:
        head = pr.get("head") or {}
        if head.get("ref") != work.branch:
            return False
    return True


def _stage_error(api: TaskManagerApi, work: Work, logger: Logger, message: str) -> None:
    try:
        api.stage(work.run_id, work.lease_id, "review_started", error=message)
    except LeaseLost as exc:
        logger.event("lease_lost", level="warning", run_id=work.run_id, detail=exc.detail)


def _release_stale_head_lease(api: TaskManagerApi, work: Work, logger: Logger) -> None:
    """Release a pre-run stale-head review lease promptly, not on a timer.

    `POST .../review-result` on the Task Manager backend compares
    `payload.sha` against the run's *current* head (see
    `agent_pr_review_result` in `backend/app/api/v1/agent_runs.py`): since
    `work.head_sha` is the head this run was leased against and the PR has
    since moved (or closed), the backend rejects this with 409 and -- because
    we still hold the matching `review` lease -- clears that lease as part of
    the same request, before ever reaching state/finding validation. That
    lets the run be re-leased (at its new head, if it still needs a review)
    immediately, instead of sitting on our now-useless lease for the full 5
    minutes until it expires on its own. A plain 200 response (the backend's
    view of the head happened to still match) also clears the lease
    unconditionally, so that outcome is fine too. `agent_svc.api` maps every
    409 from a lease-bound call to `LeaseLost`, so that is the expected
    result here, not a real failure. Only some other, unexpected failure
    (e.g. a network error) falls back to a stage error and lets the lease
    expire naturally -- still without burning an implement-style "attempts"
    counter, since review leases are never subject to that.
    """
    assert work.head_sha is not None
    try:
        api.review_result(
            work.run_id,
            work.lease_id,
            {
                "sha": work.head_sha,
                "state": "error",
                "summary": "pull request head changed before review",
                "findings": [],
            },
        )
    except LeaseLost as exc:
        logger.event(
            "review_stale_head_lease_released",
            level="info",
            run_id=work.run_id,
            detail=exc.detail,
        )
        return
    except Exception as exc:
        logger.error(exc, event="review_stale_head_release_failed", run_id=work.run_id)
        _stage_error(api, work, logger, "pull request head changed before review")


def _post_error_review(
    api: TaskManagerApi, github: GitHubClient, work: Work, logger: Logger
) -> None:
    """The `fail()` equivalent: best-effort error review result + error status."""
    if work.head_sha is None:
        return
    try:
        api.review_result(
            work.run_id,
            work.lease_id,
            {
                "sha": work.head_sha,
                "state": "error",
                "summary": GENERIC_FAILURE_SUMMARY,
                "findings": [],
            },
        )
    except Exception as exc:
        logger.error(exc, event="review_error_result_failed", run_id=work.run_id)
    try:
        github.set_status(
            work.repo_full_name,
            work.head_sha,
            "error",
            STATUS_CONTEXT,
            GENERIC_FAILURE_SUMMARY,
            target_url=None,
        )
    except Exception as exc:
        logger.error(exc, event="review_error_status_failed", run_id=work.run_id)
