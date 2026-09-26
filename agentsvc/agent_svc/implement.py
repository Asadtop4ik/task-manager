"""`handle_implement`: lease work of kind "implement" through prepare -> Codex -> publish.

Every failure path reports through `api.callback` with the exact fields the
Task Manager API expects (see `backend/app/schemas/agent_run.py`
`AgentRunCallback`): `status`, `error`, `failure_phase`, `pr_url`, `head_sha`,
plus token usage. A lost lease (heartbeat `LeaseLost`, or `RunScaffold.cancel`
already set) means the API already owns the outcome, so no callback is sent.
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .api import LeaseLost, Work
from .context import ServiceContext
from .prompts import compose_implement_prompt, route_implement
from .publish import PublishError, publish_implement
from .runctx import RunScaffold

_NO_CHANGES_MESSAGE = "agent produced no file changes"


def handle_implement(ctx: ServiceContext, work: Work, cancel: threading.Event) -> None:
    with RunScaffold(ctx, work, cancel) as run:
        _run_implement(ctx, work, run)


def _run_implement(ctx: ServiceContext, work: Work, run: RunScaffold) -> None:
    if work.mode != "pr":
        _fail(ctx, work, run, "implement", "fast mode is not handled by agent-svc")
        return

    try:
        task, is_public = _validate_task(ctx, work)
    except (ValueError, TypeError) as exc:
        _fail(ctx, work, run, "implement", str(exc))
        return

    branch = ctx.trusted.agent_task.branch_name(task)

    try:
        base_sha = ctx.mirrors.fetch(work.repo_full_name, work.base_branch)
    except Exception as exc:
        ctx.logger.error(exc, event="mirror_fetch_failed", run_id=work.run_id)
        _fail(ctx, work, run, "implement", f"could not fetch the base branch: {exc}")
        return
    if run.cancel.is_set():
        return

    try:
        existing = ctx.github.get_ref(work.repo_full_name, branch)
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"could not check for an existing branch: {exc}")
        return
    if existing is not None:
        _recover_existing_branch(ctx, work, run, branch)
        return

    run.stage("workspace_ready", base_sha=base_sha, branch=branch)
    if run.cancel.is_set():
        return

    try:
        image_dir, image_paths = _download_images(ctx, work, run)
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"could not download task images: {exc}")
        return
    if run.cancel.is_set():
        return

    mirror_path = ctx.mirrors.mirror_path(work.repo_full_name)
    try:
        ctx.codex.prepare(
            {
                "run_id": work.run_id,
                "repo": work.repo_full_name,
                "mirror": str(mirror_path),
                "base_sha": base_sha,
            }
        )
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"workspace preparation failed: {exc}")
        return
    if run.cancel.is_set():
        return

    route = route_implement(work, ctx.settings)
    base_prompt = (
        ctx.trusted.public_agent_task.build_prompt(task)
        if is_public
        else ctx.trusted.agent_task.build_prompt(task)
    )
    prompt = compose_implement_prompt(base_prompt, work, complex_route=route.complex)

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
            "images": [str(path) for path in image_paths],
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
        reason = ctx.trusted.agent_task.failure_reason(
            failure_phase="implement",
            codex_step_outcome="failure",
            load_result_text=lambda: result.final_message or None,
            load_policy_error_text=lambda: None,
            load_fast_error_text=lambda: None,
        )
        _fail(ctx, work, run, "implement", reason, usage=result.usage)
        return

    try:
        package = ctx.codex.package({"run_id": work.run_id})
    except Exception as exc:
        _fail(
            ctx,
            work,
            run,
            "implement",
            f"packaging the patch failed: {exc}",
            usage=result.usage,
        )
        return

    patch_b64 = package.get("patch_b64") or ""
    if not patch_b64:
        reason = _NO_CHANGES_MESSAGE
        summary = " ".join((result.final_message or "").split())
        if summary:
            reason += "\nCodex izohi (tasdiqlanmagan): " + summary[:650]
        _fail(ctx, work, run, "implement", reason[:900], usage=result.usage)
        return

    try:
        patch = base64.b64decode(patch_b64, validate=True)
    except (ValueError, TypeError) as exc:
        _fail(
            ctx, work, run, "implement", f"invalid patch encoding: {exc}", usage=result.usage
        )
        return

    try:
        publish_result = publish_implement(
            ctx,
            work,
            base_sha=base_sha,
            branch=branch,
            patch=patch,
            task=task,
            is_public=is_public,
            image_dir=image_dir,
            codex_summary=result.final_message,
            cancel=run.cancel,
            report_stage=lambda name: run.stage(name, base_sha=base_sha, branch=branch),
        )
    except PublishError as exc:
        _fail(ctx, work, run, exc.phase, exc.reason, usage=result.usage)
        return
    except Exception as exc:
        ctx.logger.error(exc, event="publish_unexpected_error", run_id=work.run_id)
        _fail(
            ctx,
            work,
            run,
            "publish",
            "Publisher failed before PR/deploy; inspect the agent-svc log.",
            usage=result.usage,
        )
        return

    _callback(
        ctx,
        work,
        run,
        {
            "run_id": work.run_id,
            "status": "pr_opened",
            "pr_url": publish_result.pr_url,
            "head_sha": publish_result.head_sha,
            **_usage_fields(result.usage),
        },
    )


def _validate_task(ctx: ServiceContext, work: Work) -> tuple[dict[str, Any], bool]:
    is_public = work.repo_full_name in ctx.catalog.public_repos
    raw: dict[str, Any] = {
        "task_id": work.task_id,
        "run_id": work.run_id,
        "title": work.title,
        "description": work.description,
        "base_branch": work.base_branch,
        "mode": work.mode,
        "repo_full_name": work.repo_full_name,
        "task_revision": work.task_revision,
    }
    if is_public:
        task = ctx.trusted.public_agent_task.parse_public_task(raw)
    else:
        task = ctx.trusted.agent_task.parse_task(raw)
    return dict(task), is_public


def _download_images(
    ctx: ServiceContext, work: Work, run: RunScaffold
) -> tuple[Path | None, list[Path]]:
    if work.image_count <= 0:
        return None, []
    target = run.run_dir / "images"
    paths = ctx.trusted.agent_images.download_images(
        work.run_id,
        ctx.settings.callback_token,
        target,
        api_base=ctx.settings.api_base_url,
    )
    return target / "agent-images", list(paths)


def _recover_existing_branch(
    ctx: ServiceContext, work: Work, run: RunScaffold, branch: str
) -> None:
    trailer = f"Agent-Run-ID: {work.run_id}"
    try:
        pr = ctx.github.find_open_pull_by_head(work.repo_full_name, branch)
        if pr is not None:
            head_sha = pr["head"]["sha"]
            message = ctx.github.commit_message(work.repo_full_name, head_sha)
            if trailer in message.splitlines():
                _callback(
                    ctx,
                    work,
                    run,
                    {
                        "run_id": work.run_id,
                        "status": "pr_opened",
                        "pr_url": pr.get("html_url"),
                        "head_sha": head_sha,
                    },
                )
                return
    except Exception as exc:
        ctx.logger.error(exc, event="branch_recovery_failed", run_id=work.run_id)
    _fail(ctx, work, run, "implement", "branch already exists")


def _usage_fields(usage: Mapping[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    fields: dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            fields[key] = value
    return fields


def _fail(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    phase: str,
    reason: str,
    *,
    usage: Mapping[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": work.run_id,
        "status": "failed",
        "error": reason[:900],
        "failure_phase": phase,
    }
    payload.update(_usage_fields(usage))
    _callback(ctx, work, run, payload)


def _callback(
    ctx: ServiceContext, work: Work, run: RunScaffold, payload: Mapping[str, Any]
) -> None:
    if run.cancel.is_set():
        return
    try:
        ctx.api.callback(work.run_id, work.lease_id, payload)
    except LeaseLost as exc:
        ctx.logger.event(
            "callback_lease_lost", level="warning", run_id=work.run_id, detail=exc.detail
        )
    except Exception as exc:
        ctx.logger.error(exc, event="callback_failed", run_id=work.run_id)
