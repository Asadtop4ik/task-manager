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

from .api import Work
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
        ctx.trusted.public_agent_task.build_prompt(task, qa_enabled=True)
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
    is_public = is_public_repo(ctx, work.repo_full_name)
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
        task = ctx.trusted.public_agent_task.parse_public_task(raw, qa_enabled=True)
    else:
        task = ctx.trusted.agent_task.parse_task(raw)
    return dict(task), is_public


def is_public_repo(ctx: ServiceContext, repo_full_name: str) -> bool:
    """Legacy `public_agent_task.approved_repositories`: the 3 public repos
    PLUS agent-qa when QA is enabled -- never by GitHub visibility alone.
    agent-qa is `private=True` in the catalog (its own token/visibility is
    private), but `qa_only` repos get the same PUBLIC validator/prompt path
    (the extra AGENTS.md/CLAUDE.md/.github//.codex//.agents/ path blocks
    `public_agent_task.check_diff` adds) as the 3 genuinely public repos,
    exactly like the trusted GitHub Actions publisher does. `ctx.api` already
    proved `repo_full_name` is one of `ctx.catalog`'s approved repos before
    handing us this `Work`, so there is no separate "is QA enabled" flag to
    consult here -- if agent-svc leased work for agent-qa at all, QA is
    enabled.
    """
    info = ctx.catalog.get(repo_full_name)
    return info is not None and (not info.private or info.qa_only)


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
    image_dir = target / "agent-images"
    _make_images_readable_by_agent_codex(image_dir, paths)
    return image_dir, list(paths)


def _make_images_readable_by_agent_codex(image_dir: Path, paths: list[Path]) -> None:
    """`agent_images.download_images` (trusted, runs as agent-svc) creates
    `image_dir` mode 0700 and each file mode 0600 -- agent-svc only.
    agent-codex (group `agentwork`) must be able to READ these: they are
    handed straight to `codex exec --image <path>`, which runs as
    agent-codex. `image_dir`'s GROUP is already `agentwork` (inherited via
    the setgid bit on `run_dir/images`, its parent -- see
    `repos.make_run_dir` -- POSIX propagates a setgid directory's group to
    everything created under it); only the permission bits need relaxing.
    """
    image_dir.chmod(0o2750)
    for path in paths:
        path.chmod(0o640)


def _pr_matches_this_run(
    pr: Mapping[str, Any], repo: str, branch: str, base_branch: str
) -> bool:
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return (
        head.get("ref") == branch
        and (head.get("repo") or {}).get("full_name", "").lower() == repo.lower()
        and base.get("ref") == base_branch
    )


def _recover_existing_branch(
    ctx: ServiceContext, work: Work, run: RunScaffold, branch: str
) -> None:
    """The branch this run would push already exists: a previous attempt got
    at least as far as `branch_pushed` before agent-svc crashed, was killed,
    or otherwise never got to report back. Never push to it (see the branch-
    exists guard above `_run_implement`); instead prove it is genuinely THIS
    run's own commit (the exact `Agent-Run-ID` trailer, not merely a branch
    that happens to share the name) and recover from wherever publication
    actually stopped: reopen the PR if one is missing (a crash between push
    and PR creation), or just report the existing one.
    """
    trailer = f"Agent-Run-ID: {work.run_id}"
    try:
        head_sha = ctx.github.get_ref(work.repo_full_name, branch)
        if head_sha is None:
            raise ValueError("branch disappeared during recovery")
        message = ctx.github.commit_message(work.repo_full_name, head_sha)
        if trailer not in message.splitlines():
            _fail(ctx, work, run, "implement", "branch already exists")
            return

        pr = ctx.github.find_open_pull_by_head(work.repo_full_name, branch)
        if pr is not None:
            if not _pr_matches_this_run(pr, work.repo_full_name, branch, work.base_branch):
                _fail(ctx, work, run, "implement", "branch already exists")
                return
            pr_url = pr.get("html_url")
        else:
            # The push succeeded but agent-svc never got to (or failed to)
            # open the PR. Do it now instead of failing a run whose branch
            # is already live and correctly attributed.
            created = ctx.github.create_pull(
                work.repo_full_name,
                head=branch,
                base=work.base_branch,
                title=f"Task #{work.task_id}: Codex change",
                body=(
                    f"Task Manager task #{work.task_id}\n\n"
                    "Recovered after an interrupted publish: the branch was already "
                    "pushed by this exact run. Review the diff and CI results before "
                    "merging.\n"
                ),
            )
            number = created.get("number")
            pr_url = created.get("html_url") or (
                f"https://github.com/{work.repo_full_name}/pull/{number}"
            )

        _callback(
            ctx,
            work,
            run,
            {
                "run_id": work.run_id,
                "status": "pr_opened",
                "pr_url": pr_url,
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
    run.deliver(lambda: ctx.api.callback(work.run_id, work.lease_id, payload))
