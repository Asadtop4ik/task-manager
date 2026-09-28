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
from .ops_requests import build_ops_note, split_trailer
from .ops_requests import validate as validate_ops_requests
from .prompts import compose_implement_prompt, route_implement
from .publish import PublishError, _check_not_cancelled, publish_implement
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
    # Loaded fresh, once per run (never cached across runs -- an operator may
    # edit the allowlist between two runs of the same project): used both to
    # decide whether the prompt gets ops-request instructions, and later to
    # validate whatever trailer Codex actually emitted. `None` means "missing
    # or invalid file" -- treated as "no allowlist" throughout, never a crash.
    ops_project_key = _ops_project_key(ctx, work)
    allowlist = _load_ops_allowlist(ctx, work.run_id)
    ops_project = (
        allowlist.projects.get(ops_project_key)
        if allowlist is not None and ops_project_key is not None
        else None
    )
    prompt = compose_implement_prompt(
        base_prompt, work, complex_route=route.complex, ops_project=ops_project
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

    # Every downstream use of Codex's own text -- a failure reason, the "no
    # changes" note, or the eventual PR body -- must never carry the ops
    # trailer line: an env value (however harmless-looking) must never reach
    # a public PR or a Telegram message. This one call covers all of them.
    summary, raw_ops, trailer_note = split_trailer(result.final_message or "")

    if result.timed_out or result.idle_killed or result.exit_code != 0:
        reason = ctx.trusted.agent_task.failure_reason(
            failure_phase="implement",
            codex_step_outcome="failure",
            load_result_text=lambda: summary or None,
            load_policy_error_text=lambda: None,
            load_fast_error_text=lambda: None,
        )
        if result.error_message and not result.final_message:
            # e.g. a usage limit or model error reported by Codex itself.
            reason = f"{reason} (Codex: {result.error_message[:300]})"
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

    proposals = validate_ops_requests(
        raw_ops,
        project_key=ops_project_key,
        repo_full_name=work.repo_full_name,
        allowlist=allowlist,
        policy_module=ctx.trusted.agent_ops_policy,
    )
    ops_note = build_ops_note(trailer_note, ctx.trusted.agent_ops_policy)

    patch_b64 = package.get("patch_b64") or ""
    if not patch_b64:
        if any(proposal["policy"] == "allowed" for proposal in proposals):
            _callback(
                ctx,
                work,
                run,
                {
                    "run_id": work.run_id,
                    "status": "ops_pending",
                    "ops_requests": proposals,
                    "ops_note": ops_note,
                    **_usage_fields(result.usage),
                },
            )
            return
        reason = _NO_CHANGES_MESSAGE
        flat_summary = " ".join(summary.split())
        if flat_summary:
            reason += "\nCodex izohi (tasdiqlanmagan): " + flat_summary[:650]
        _fail(
            ctx,
            work,
            run,
            "implement",
            reason[:900],
            usage=result.usage,
            ops_requests=proposals,
            ops_note=ops_note,
        )
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
            codex_summary=summary,
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
            "ops_requests": proposals,
            "ops_note": ops_note,
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


def _ops_project_key(ctx: ServiceContext, work: Work) -> str | None:
    """The allowlist project key for `work.repo_full_name`, or `None` if
    (implausibly, since `parse_work` already proved this repo is in the
    approved catalog) it has none. The allowlist itself keys projects by
    this same `project_key`, never by `repo_full_name` directly."""
    info = ctx.catalog.get(work.repo_full_name)
    return info.project_key if info is not None else None


def _load_ops_allowlist(ctx: ServiceContext, run_id: str) -> Any:
    """Load the ops-requests allowlist fresh for this one run (never cached
    across runs -- an operator may edit it between two runs). Missing or
    invalid -> `None`: every caller treats that exactly like "no allowlist
    entry for this project" (no ops rules in the prompt, every proposal
    denied with `policy_reason="no_allowlist"`); this must never crash an
    implement run over a secondary feature. Logged once, here, on failure.

    A separate, private function (rather than inlining `ctx.trusted.
    agent_ops_policy.load_allowlist(...)` at each call site) so tests can
    substitute a canned `Allowlist` with `unittest.mock.patch.object` instead
    of needing a real root-owned file on disk (`require_root_owned=True` is
    always passed in production, per the spec, and would refuse any file a
    non-root test process could create).
    """
    try:
        return ctx.trusted.agent_ops_policy.load_allowlist(
            ctx.settings.ops_allowlist_path, ctx.catalog.repos, require_root_owned=True
        )
    except Exception as exc:
        ctx.logger.error(exc, event="ops_allowlist_load_failed", run_id=run_id)
        return None


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
            _check_not_cancelled(ctx, work, run.cancel)
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
    ops_requests: list[dict[str, Any]] | None = None,
    ops_note: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": work.run_id,
        "status": "failed",
        "error": reason[:900],
        "failure_phase": phase,
    }
    payload.update(_usage_fields(usage))
    # Only attached when this failure actually has ops requests to report
    # (the "no patch, none allowed" flow); every other `_fail` call site is
    # unaffected and keeps sending exactly the payload it always has.
    if ops_requests:
        payload["ops_requests"] = ops_requests
        payload["ops_note"] = ops_note
    _callback(ctx, work, run, payload)


def _callback(
    ctx: ServiceContext, work: Work, run: RunScaffold, payload: Mapping[str, Any]
) -> None:
    if run.cancel.is_set():
        return
    run.deliver(lambda: ctx.api.callback(work.run_id, work.lease_id, payload))
