"""WP8: CI status, external merge, and production-deploy watch checks.

Ports `ops/agent_deploy_monitor.py` (`check_ci_once`, `run_once`,
`_latest_pr_ci`, `_successful_deploy_run`, `_production_matches`) onto
`ctx.api` / `ctx.github` / `ctx.catalog`, with two differences the local
executor needs and the old cron-timer script did not:

* Production image state is read via the root-owned
  `libexec/image_state.py` helper over `sudo -n`, never by calling `docker`
  directly (this process is not in the `docker` group) -- see
  `_default_image_state_command`.
* Every pending record is isolated from every other one (a record whose
  processing raises is logged and skipped, exactly like a lane's per-record
  isolation), and each record is only actually re-checked on its own
  schedule: 15s for the first 15 minutes after it is first seen, then 60s
  (`_RecordScheduler`) -- so a quiet PR does not cost a GitHub call on every
  5-15s watch tick.
* A QA-repo record when the optional QA GitHub token was never provisioned
  (`github.build_token_selector` raises `UnknownRepository` for it, by
  design) is not a "bad record": it is logged once, at info, the first time
  it is seen, and silently skipped on every tick after that -- never an
  error on every cycle.

`build_watch_checks(ctx)` returns the list of checks `agent_svc.lanes.WatchLoop`
runs; `ctx` must expose `api` (`api.TaskManagerApi`), `github`
(`github.GitHubClient`), `logger` (`log.Logger`), `catalog` (`repos.Catalog`),
and `settings` (`config.Settings`, for `settings.libexec_dir`).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from .github import UnknownRepository

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_PAGE_SIZE = 50

FAST_INTERVAL_S = 15.0
SLOW_INTERVAL_S = 60.0
FAST_WINDOW_S = 15 * 60.0

DEFAULT_IMAGE_STATE_SUDO_PREFIX: tuple[str, ...] = (
    "/usr/bin/sudo",
    "-n",
    "/usr/bin/python3",
    "-I",
)
IMAGE_STATE_TIMEOUT_S = 15.0


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _pr_number(pr_url: Any, repo: str) -> int | None:
    prefix = f"https://github.com/{repo}/pull/"
    if not isinstance(pr_url, str) or not pr_url.startswith(prefix):
        return None
    tail = pr_url[len(prefix) :]
    return int(tail) if tail.isdigit() else None


class _RecordScheduler:
    """Per-record adaptive check cadence: 15s for the first 15 minutes a
    record is seen, then 60s. Purely in-memory, as the design allows."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._first_seen: dict[str, float] = {}
        self._next_check: dict[str, float] = {}

    def due(self, record_id: str) -> bool:
        now = self._now()
        first_seen = self._first_seen.setdefault(record_id, now)
        next_check = self._next_check.get(record_id)
        if next_check is not None and now < next_check:
            return False
        interval = FAST_INTERVAL_S if (now - first_seen) < FAST_WINDOW_S else SLOW_INTERVAL_S
        self._next_check[record_id] = now + interval
        return True

    def forget(self, record_id: str) -> None:
        self._first_seen.pop(record_id, None)
        self._next_check.pop(record_id, None)


def _parse_ci_record(raw: Any, catalog: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid pending CI run")
    row_id = raw.get("id")
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
        raise ValueError("invalid pending CI run ID")
    repo = raw.get("repo_full_name")
    repo_info = catalog.get(repo) if isinstance(repo, str) else None
    if (
        repo_info is None
        or raw.get("base_branch") != repo_info.branch
        or not repo_info.pr_ci_jobs
    ):
        raise ValueError("CI run is outside approved repositories")
    assert isinstance(repo, str)  # implied by `repo_info is not None` above
    run_id = str(UUID(str(raw.get("run_id"))))
    pr_number = _pr_number(raw.get("pr_url"), repo)
    if pr_number is None:
        raise ValueError("invalid CI PR reference")
    if raw.get("status") not in {"pr_opened", "pr_ready"} or not _valid_sha(
        raw.get("head_sha")
    ):
        raise ValueError("invalid CI run status or SHA")
    if raw.get("ci_status") not in {None, "pending", "failure", "success"}:
        raise ValueError("invalid CI conclusion")
    return {
        "id": row_id,
        "run_id": run_id,
        "repo": repo,
        "repo_info": repo_info,
        "pr_number": pr_number,
        "head_sha": raw["head_sha"],
        "ci_status": raw.get("ci_status"),
        "ci_verified_sha": raw.get("ci_verified_sha"),
        "ci_url": raw.get("ci_url"),
    }


def _parse_external_record(raw: Any, catalog: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid pending run")
    row_id = raw.get("id")
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
        raise ValueError("invalid pending run ID")
    repo = raw.get("repo_full_name")
    repo_info = catalog.get(repo) if isinstance(repo, str) else None
    if (
        repo_info is None
        or repo_info.private
        or raw.get("base_branch") != repo_info.branch
        or not repo_info.images
    ):
        raise ValueError("run is outside approved public repositories")
    assert isinstance(repo, str)  # implied by `repo_info is not None` above
    run_id = str(UUID(str(raw.get("run_id"))))
    pr_number = _pr_number(raw.get("pr_url"), repo)
    if pr_number is None:
        raise ValueError("invalid PR reference")
    if raw.get("status") not in {"pr_ready", "merged"}:
        raise ValueError("invalid pending status")
    if not isinstance(raw.get("notified"), bool):
        raise ValueError("invalid notification state")
    sha = raw.get("merged_sha")
    if raw["status"] == "merged" and not _valid_sha(sha):
        raise ValueError("merged run lacks its commit")
    return {
        "id": row_id,
        "run_id": run_id,
        "repo": repo,
        "repo_info": repo_info,
        "pr_number": pr_number,
        "status": raw["status"],
        "sha": sha,
        "notified": raw["notified"],
    }


def _default_image_state_command(libexec_dir: str) -> list[str]:
    script = str(Path(libexec_dir) / "image_state.py")
    return [*DEFAULT_IMAGE_STATE_SUDO_PREFIX, script]


class WatchChecks:
    def __init__(
        self,
        ctx: Any,
        *,
        image_state_command: Sequence[str] | None = None,
        command_runner: Callable[..., Any] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx = ctx
        self._image_state_command = list(
            image_state_command
            if image_state_command is not None
            else _default_image_state_command(ctx.settings.libexec_dir)
        )
        self._run = command_runner or subprocess.run
        self._ci_schedule = _RecordScheduler(now=now)
        self._deploy_schedule = _RecordScheduler(now=now)
        # Repos for which we have already logged "no QA token configured"
        # once; every check after the first is a silent, expected skip.
        self._qa_disabled_logged: set[str] = set()

    def _skip_if_qa_disabled(self, repo: str, exc: UnknownRepository) -> None:
        if repo in self._qa_disabled_logged:
            return
        self._qa_disabled_logged.add(repo)
        self._ctx.logger.event(
            "watch_qa_repo_disabled",
            level="info",
            detail=f"no QA GitHub token configured; skipping {repo} ({exc})",
        )

    # ---------------------------------------------------------------
    # CI check (ports `check_ci_once` / `_latest_pr_ci`)
    # ---------------------------------------------------------------

    def check_ci(self) -> None:
        api = self._ctx.api
        logger = self._ctx.logger
        after_id = 0
        while True:
            try:
                pending = api.ci_pending(after_id)
            except Exception as exc:
                logger.error(exc, event="watch_ci_pending_fetch_failed")
                return
            if not isinstance(pending, list):
                logger.event("watch_ci_pending_invalid", level="error")
                return
            if not pending:
                return
            start_after_id = after_id
            for raw in pending:
                row_id = raw.get("id") if isinstance(raw, dict) else None
                if (
                    isinstance(row_id, int)
                    and not isinstance(row_id, bool)
                    and row_id > after_id
                ):
                    after_id = row_id
                self._isolate_ci_record(raw)
            if len(pending) < _PAGE_SIZE:
                return
            if after_id == start_after_id:
                logger.event("watch_ci_pending_cursor_stuck", level="error")
                return

    def _isolate_ci_record(self, raw: Any) -> None:
        logger = self._ctx.logger
        try:
            record = _parse_ci_record(raw, self._ctx.catalog)
        except ValueError as exc:
            logger.error(exc, event="watch_ci_record_invalid")
            return
        if not self._ci_schedule.due(record["run_id"]):
            return
        try:
            self._apply_ci_record(record)
        except UnknownRepository as exc:
            self._skip_if_qa_disabled(record["repo"], exc)
        except Exception as exc:
            logger.error(exc, event="watch_ci_record_failed", run_id=record["run_id"])

    def _apply_ci_record(self, record: dict[str, Any]) -> None:
        github = self._ctx.github
        api = self._ctx.api
        pr = github.get_pull(record["repo"], record["pr_number"])
        if pr.get("state") == "closed" and not pr.get("merged"):
            return
        head = pr.get("head") or {}
        sha = head.get("sha")
        branch = head.get("ref")
        if (
            not _valid_sha(sha)
            or not isinstance(branch, str)
            or (head.get("repo") or {}).get("full_name", "").lower() != record["repo"].lower()
        ):
            return
        assert isinstance(sha, str)  # implied by `_valid_sha(sha)` above
        conclusion, url = self._ci_conclusion(record["repo_info"], sha, branch)
        if (
            record["head_sha"] == sha
            and record["ci_status"] == conclusion
            and record["ci_url"] == url
            and (conclusion != "success" or record["ci_verified_sha"] == sha)
        ):
            return
        api.ci_result(record["run_id"], sha=sha, conclusion=conclusion, github_run_url=url)
        # Only after the API accepted it: lets an operator see the watch lane
        # actually moving runs (ids and a conclusion only, never a secret).
        self._ctx.logger.event(
            "watch_ci_result_reported",
            level="info",
            run_id=record["run_id"],
            repo=record["repo"],
            pr_number=record["pr_number"],
            sha=sha,
            conclusion=conclusion,
        )

    def _ci_conclusion(self, repo_info: Any, sha: str, branch: str) -> tuple[str, str | None]:
        github = self._ctx.github
        workflow_file = repo_info.pr_ci_workflow.rsplit("/", 1)[-1]
        runs = sorted(
            github.list_workflow_runs(
                repo_info.full_name, workflow_file, event="pull_request", head_sha=sha
            ),
            key=lambda item: item.get("id", 0),
            reverse=True,
        )
        for run in runs:
            if (
                run.get("head_sha") != sha
                or run.get("head_branch") != branch
                or run.get("event") != "pull_request"
                or run.get("path") != repo_info.pr_ci_workflow
                or not isinstance(run.get("id"), int)
            ):
                continue
            if run.get("status") != "completed":
                return "pending", None
            url = f"https://github.com/{repo_info.full_name}/actions/runs/{run['id']}"
            if run.get("conclusion") != "success":
                return "failure", url
            jobs = github.run_jobs(repo_info.full_name, run["id"])
            successful = {
                job.get("name") for job in jobs if job.get("conclusion") == "success"
            }
            required = set(repo_info.pr_ci_jobs)
            return ("success" if required.issubset(successful) else "failure"), url
        return "pending", None

    # ---------------------------------------------------------------
    # External merge/deploy check (ports `run_once` / `_successful_deploy_run`
    # / `_production_matches`)
    # ---------------------------------------------------------------

    def check_merge_deploy(self) -> None:
        api = self._ctx.api
        logger = self._ctx.logger
        after_id = 0
        while True:
            try:
                pending = api.external_pending(after_id)
            except Exception as exc:
                logger.error(exc, event="watch_external_pending_fetch_failed")
                return
            if not isinstance(pending, list):
                logger.event("watch_external_pending_invalid", level="error")
                return
            if not pending:
                return
            start_after_id = after_id
            for raw in pending:
                row_id = raw.get("id") if isinstance(raw, dict) else None
                if (
                    isinstance(row_id, int)
                    and not isinstance(row_id, bool)
                    and row_id > after_id
                ):
                    after_id = row_id
                self._isolate_external_record(raw)
            if len(pending) < _PAGE_SIZE:
                return
            if after_id == start_after_id:
                logger.event("watch_external_pending_cursor_stuck", level="error")
                return

    def _isolate_external_record(self, raw: Any) -> None:
        logger = self._ctx.logger
        try:
            record = _parse_external_record(raw, self._ctx.catalog)
        except ValueError as exc:
            logger.error(exc, event="watch_external_record_invalid")
            return
        if not record["notified"]:
            # Deliver the PR/merge notice before advancing to the next state
            # (matches legacy `run_once`).
            return
        if not self._deploy_schedule.due(record["run_id"]):
            return
        try:
            self._apply_external_record(record)
        except UnknownRepository as exc:
            self._skip_if_qa_disabled(record["repo"], exc)
        except Exception as exc:
            logger.error(exc, event="watch_external_record_failed", run_id=record["run_id"])

    def _apply_external_record(self, record: dict[str, Any]) -> None:
        github = self._ctx.github
        api = self._ctx.api
        repo = record["repo"]
        repo_info = record["repo_info"]
        if record["status"] == "pr_ready":
            pr = github.get_pull(repo, record["pr_number"])
            sha = pr.get("merge_commit_sha")
            if pr.get("merged") and _valid_sha(sha):
                api.merged(record["run_id"], sha=sha)
                self._ctx.logger.event(
                    "watch_merged_reported",
                    level="info",
                    run_id=record["run_id"],
                    repo=repo,
                    pr_number=record["pr_number"],
                    sha=sha,
                )
                # The run now becomes "merged" under the same id; let it be
                # rescheduled fresh (it may take a while to actually deploy).
                self._deploy_schedule.forget(record["run_id"])
            return
        sha = record["sha"]
        run_url = self._successful_deploy_run(repo_info, sha)
        if run_url is not None and self._production_matches(repo_info, sha):
            api.deployed(record["run_id"], sha=sha, github_run_url=run_url)
            self._ctx.logger.event(
                "watch_deployed_reported",
                level="info",
                run_id=record["run_id"],
                repo=repo,
                sha=sha,
            )

    def _successful_deploy_run(self, repo_info: Any, sha: str) -> str | None:
        github = self._ctx.github
        runs = github.list_workflow_runs(
            repo_info.full_name, "deploy.yml", event="push", head_sha=sha
        )
        for run in runs:
            if not (
                run.get("head_sha") == sha
                and run.get("head_branch") == repo_info.branch
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and isinstance(run.get("id"), int)
            ):
                continue
            jobs = github.run_jobs(repo_info.full_name, run["id"])
            succeeded = {job.get("name") for job in jobs if job.get("conclusion") == "success"}
            if set(repo_info.ci_jobs).issubset(succeeded) and "deploy" in succeeded:
                return f"https://github.com/{repo_info.full_name}/actions/runs/{run['id']}"
        return None

    def _production_matches(self, repo_info: Any, sha: str) -> bool:
        if not repo_info.images:
            return True
        names = [name for name, _ in repo_info.images]
        state = self._query_image_state(names)
        if state is None:
            return False
        for name, image in repo_info.images:
            info = state.get(name)
            if not isinstance(info, dict):
                return False
            if info.get("image") != f"{image}:{sha}":
                return False
            if info.get("running") is not True:
                return False
            if info.get("health") != "healthy":
                return False
        return True

    def _query_image_state(self, container_names: list[str]) -> dict[str, Any] | None:
        argv = [*self._image_state_command, *container_names]
        try:
            completed = self._run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=IMAGE_STATE_TIMEOUT_S,
                text=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


def build_watch_checks(ctx: Any, **kwargs: Any) -> list[Callable[[], None]]:
    """Build the two checks `lanes.WatchLoop` should run: CI, then merge/deploy."""
    checks = WatchChecks(ctx, **kwargs)
    return [checks.check_ci, checks.check_merge_deploy]
