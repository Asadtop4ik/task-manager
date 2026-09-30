"""`handle_correction`: apply an owner-requested correction to an open agent PR.

Mirrors the trusted `scripts/agent_release.py` `verify-correction` /
`publish-correction` / `reject-action` flow exactly: fast-forward-only, a
stale PR head is refused rather than retried, and an empty correction patch
keeps the current head (matching legacy: it still reports success and lets a
fresh review run against the unchanged SHA).
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Mapping
from typing import Any

from .api import Work
from .ci_logs import correction_ci_block
from .context import ServiceContext
from .ops_requests import split_trailer
from .prompts import compose_correction_prompt, route_correction
from .publish import PublishError, publish_correction
from .runctx import RunScaffold

_STALE_HEAD_MESSAGE = "PR head changed; fetch the current run and retry"


def handle_correction(ctx: ServiceContext, work: Work, cancel: threading.Event) -> None:
    with RunScaffold(ctx, work, cancel) as run:
        _run_correction(ctx, work, run)


def _run_correction(ctx: ServiceContext, work: Work, run: RunScaffold) -> None:
    if not (
        work.branch
        and work.pr_number
        and work.expected_head_sha
        and work.instruction
        and work.action_id
    ):
        _reject(ctx, work, run, "correction request is missing required fields")
        return

    expected_branch = f"codex/task-{work.task_id}-{work.run_id}"
    if work.branch != expected_branch:
        _reject(ctx, work, run, "correction branch does not match this run")
        return

    try:
        mirror_sha = ctx.mirrors.fetch(work.repo_full_name, work.branch)
    except Exception as exc:
        _reject(ctx, work, run, f"could not fetch the PR branch: {exc}")
        return
    if run.cancel.is_set():
        return

    try:
        pr = ctx.github.get_pull(work.repo_full_name, work.pr_number)
    except Exception as exc:
        _reject(ctx, work, run, f"could not verify the PR: {exc}")
        return

    head = pr.get("head") or {}
    base = pr.get("base") or {}
    pr_matches = (
        pr.get("state") == "open"
        and not pr.get("merged")
        and head.get("sha") == work.expected_head_sha
        and head.get("ref") == work.branch
        and (head.get("repo") or {}).get("full_name", "").lower()
        == work.repo_full_name.lower()
        and base.get("ref") == work.base_branch
    )
    if not pr_matches or mirror_sha != work.expected_head_sha:
        _reject(ctx, work, run, _STALE_HEAD_MESSAGE)
        return

    run.stage("workspace_ready", base_sha=work.expected_head_sha, branch=work.branch)
    if run.cancel.is_set():
        return

    try:
        ctx.codex.prepare(
            {
                "run_id": work.run_id,
                "repo": work.repo_full_name,
                "mirror": str(ctx.mirrors.mirror_path(work.repo_full_name)),
                "base_sha": work.expected_head_sha,
            }
        )
    except Exception as exc:
        _reject(ctx, work, run, f"workspace preparation failed: {exc}")
        return
    if run.cancel.is_set():
        return

    route = route_correction(work, ctx.settings)
    base_prompt = ctx.trusted.agent_release.correction_prompt(
        {"task_id": work.task_id, "pr_url": work.pr_url},
        work.expected_head_sha,
        work.instruction,
    )
    prompt = compose_correction_prompt(
        base_prompt,
        complex_route=route.complex,
        ci_log_block=correction_ci_block(ctx, work, run.cancel),
    )

    run.stage("codex_started")
    result = ctx.codex.run_exec(
        {
            "run_id": work.run_id,
            "lane": "code",
            "cwd": "wt",
            "model": route.model,
            "effort": route.effort,
            "sandbox": route.sandbox,
            "multi_agent": route.multi_agent,
            "prompt": prompt,
            "images": [],
            "output_schema": None,
            "timeout_s": route.timeout_s,
            "idle_timeout_s": ctx.settings.idle_timeout_s,
        },
        on_event=lambda _event: None,
        heartbeat=None,
        cancel=run.cancel,
    )
    run.stage("codex_finished")

    if run.cancel.is_set() or result.cancelled:
        return

    if result.timed_out or result.idle_killed or result.exit_code != 0:
        # Correction never acts on an ops-request trailer (v1); still strip
        # it before this text reaches the owner via `action_result` -- an
        # env value must never leak into a rejection message.
        summary, _raw, _note = split_trailer(result.final_message or "")
        reason = summary.strip() or "Codex correction failed"
        _reject(ctx, work, run, reason[:900])
        return

    try:
        package = ctx.codex.package({"run_id": work.run_id})
    except Exception as exc:
        _reject(ctx, work, run, f"packaging the correction patch failed: {exc}")
        return

    patch_b64 = package.get("patch_b64") or ""
    if not patch_b64:
        # Legacy `agent_release.publish_correction`: an empty patch keeps the
        # current head and still completes, re-triggering review at the
        # unchanged SHA.
        _complete(
            ctx,
            work,
            run,
            work.expected_head_sha,
            "Correction produced no changes; current head kept",
        )
        return

    try:
        patch = base64.b64decode(patch_b64, validate=True)
    except (ValueError, TypeError) as exc:
        _reject(ctx, work, run, f"invalid patch encoding: {exc}")
        return

    try:
        publish_result = publish_correction(
            ctx,
            work,
            expected_head_sha=work.expected_head_sha,
            patch=patch,
            cancel=run.cancel,
            report_stage=lambda name: run.stage(
                name, base_sha=work.expected_head_sha, branch=work.branch
            ),
        )
    except PublishError as exc:
        _reject(ctx, work, run, exc.reason)
        return
    except Exception as exc:
        ctx.logger.error(exc, event="publish_unexpected_error", run_id=work.run_id)
        # Legacy `agent_release.reject_action`'s exact fixed text, plus a
        # redacted detail legacy's own catch-all never carried.
        detail = ctx.redactor.redact(str(exc))[:500]
        _reject(ctx, work, run, f"Trusted correction publisher failed: {detail}")
        return

    _complete(
        ctx,
        work,
        run,
        publish_result.head_sha,
        "Correction published; CI and independent review must pass again",
    )


def _reject(ctx: ServiceContext, work: Work, run: RunScaffold, message: str) -> None:
    _action_result(ctx, work, run, {"status": "rejected", "message": message[:1000]})


def _complete(
    ctx: ServiceContext, work: Work, run: RunScaffold, head_sha: str, message: str
) -> None:
    _action_result(
        ctx,
        work,
        run,
        {"status": "completed", "head_sha": head_sha, "message": message[:1000]},
    )


def _action_result(
    ctx: ServiceContext, work: Work, run: RunScaffold, payload: Mapping[str, Any]
) -> None:
    if run.cancel.is_set():
        return
    body: dict[str, Any] = {"action_id": work.action_id, **payload}
    run.deliver(lambda: ctx.api.action_result(work.run_id, work.lease_id, body))
