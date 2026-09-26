"""Clean-checkout publication: validate an agent patch, run the trusted
preflight in the sandbox, apply the FINAL (post-preflight) patch, commit with
the fixed identity/message/trailer, and push — for a fresh `implement` branch
and for an existing `correction` branch alike.

Security round 2 (blocker 1): agent-svc never runs ruff/black/compileall
itself — those tools execute inside a directory whose content is entirely
attacker-controlled (the agent's own patch), so running them as agent-svc
would let a malicious patch read every credential agent-svc can reach.
`_run_preflight` hands the patch to `codex.preflight`, which runs the trusted
formatter/lint step as agent-codex in the sandbox and returns the resulting
patch; agent-svc only ever applies that (like the original) to its own
git-only checkout and runs the trusted `check_diff` (pure path/credential
checks, no code execution) before and after.

Every git process here runs hardened the same way the trusted GitHub Actions
publisher does (see `agent-svc-design.md`): no system/global git config, no
hooks, no attributes-driven filters that could alter the applied patch. The
push token lives only in the one push subprocess's environment, via
`repos.git_auth_env` — the exact helper `MirrorManager` uses for fetches —
never in argv, a git config file, or a log line.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repos
from .api import LeaseLost, Work
from .codex import CodexChildError
from .context import ServiceContext
from .log import Redactor

_GIT_TIMEOUT_S = 120
_COMMIT_NAME = "Business AI Codex"
_COMMIT_EMAIL = "codex@users.noreply.github.com"
_PUBLISH_DIR_MODE = 0o700
_BAD_MODES = ("120000", "160000")

# Per-repository preflight tool versions (see scripts/agent_preflight.py
# `run`): the same repo can only ever need one pinned version of each tool,
# but different repos pin different `ruff` versions, so the tools map handed
# to the trusted preflight is built per repository from `settings.
# preflight_tool_paths` rather than being one fixed map.
_PREFLIGHT_TOOL_KEYS: dict[str, dict[str, str]] = {
    "Asadtop4ik/task-manager": {"ruff": "ruff-0.16.0", "black": "black-26.5.1"},
    "muradjanov-dev/kans-shop": {"ruff": "ruff-0.16.0", "black": "black-26.5.1"},
    "Asadtop4ik/agent-qa": {"ruff": "ruff-0.7.4"},
    "muradjanov-dev/qurbot": {"ruff": "ruff-0.7.4"},
}


class PublishError(Exception):
    """A publish step failed; `reason` is the trusted-script failure text."""

    def __init__(self, reason: str, *, phase: str = "publish") -> None:
        super().__init__(reason)
        self.reason = reason
        self.phase = phase


@dataclass(frozen=True)
class PublishResult:
    head_sha: str
    pr_url: str | None = None


def _tools_for_repo(ctx: ServiceContext, repo: str) -> dict[str, str]:
    keys = _PREFLIGHT_TOOL_KEYS.get(repo, {})
    return {name: ctx.settings.preflight_tool_paths[key] for name, key in keys.items()}


def _git_env() -> dict[str, str]:
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }


def _git(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            *args,
        ],
        cwd=str(cwd),
        env=dict(env),
        input=input_bytes,
        capture_output=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_bytes: bytes | None = None,
    error: str,
    redactor: Redactor | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = _git(args, cwd=cwd, env=env, input_bytes=input_bytes)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        if redactor is not None:
            stderr = redactor.redact(stderr)
        raise PublishError(f"{error}: {stderr[-2000:] or 'git command failed'}")
    return result


def _prepare_checkout(ctx: ServiceContext, run_id: str, mirror_path: Path) -> Path:
    publish_root = Path(ctx.settings.state_dir) / "publish"
    publish_root.mkdir(parents=True, exist_ok=True)
    publish_dir = publish_root / run_id
    if publish_dir.exists():
        shutil.rmtree(publish_dir, ignore_errors=True)
    env = _git_env()
    _run_git(
        ["clone", "--no-hardlinks", "--no-checkout", str(mirror_path), str(publish_dir)],
        cwd=publish_root,
        env=env,
        error="could not clone the mirror for publication",
        redactor=ctx.redactor,
    )
    publish_dir.chmod(_PUBLISH_DIR_MODE)
    return publish_dir


def _reject_bad_modes(publish_dir: Path, patch: bytes, env: Mapping[str, str]) -> None:
    """Defense in depth, independent of the sandboxed child's own check: a
    symlink or submodule entry applied directly in agent-svc's OWN checkout
    would create a real filesystem symlink/gitlink owned by agent-svc, before
    the trusted `check_diff` ever runs. `git apply --summary` parses the
    patch (never touching the working tree) and lists every file mode it
    would create/change; scanning that output for the two disallowed modes
    catches this without agent-svc needing its own patch-format parser."""
    result = _git(["apply", "--summary"], cwd=publish_dir, env=env, input_bytes=patch)
    if result.returncode != 0:
        # An unparseable/inapplicable patch is reported by the real `apply
        # --index` call right after this; nothing to reject here yet.
        return
    summary = result.stdout.decode("utf-8", "replace")
    if any(mode in summary for mode in _BAD_MODES):
        raise PublishError("unsupported change (symlink or submodule)")


def _apply_patch(
    publish_dir: Path, patch: bytes, env: Mapping[str, str], *, redactor: Redactor | None
) -> None:
    if not patch.strip():
        raise PublishError("agent produced no file changes")
    _reject_bad_modes(publish_dir, patch, env)
    _run_git(
        ["apply", "--index"],
        cwd=publish_dir,
        env=env,
        input_bytes=patch,
        error="patch did not apply cleanly",
        redactor=redactor,
    )


def _check_diff(
    checker: Callable[..., bool],
    *,
    cwd: Path,
    task: Mapping[str, Any],
    image_dir: Path | None,
    is_public: bool = False,
) -> None:
    kwargs: dict[str, Any] = {"cwd": cwd, "task": dict(task), "image_dir": image_dir}
    if is_public:
        # `public_agent_task.check_diff` re-validates repo/branch approval
        # via `parse_public_task(qa_enabled=...)`; agent-svc's own catalog
        # (already proven for this `Work` before it ever reaches here) is
        # the actual approval gate, so this is always explicitly `True`,
        # never read from an environment variable the trusted script would
        # otherwise fall back to.
        kwargs["qa_enabled"] = True
    try:
        checker(**kwargs)
    except (ValueError, subprocess.CalledProcessError) as exc:
        # The exact prefix each trusted script's own CLI wrapper uses --
        # `public_agent_task.py`'s `__main__` says "public agent task
        # rejected: ...", `agent_task.py`'s says "agent task failed: ...".
        # `check_diff` itself never raises anything else, but matching this
        # text means a callback's `error` field reads identically to what
        # the legacy GitHub Actions publisher would have reported.
        prefix = "public agent task rejected" if is_public else "agent task failed"
        raise PublishError(f"{prefix}: {exc}") from exc


def _run_preflight(
    ctx: ServiceContext, work: Work, *, patch: bytes, base_sha: str
) -> tuple[str, bytes]:
    """Hand the patch to the sandboxed `preflight` subcommand (runs the
    trusted formatter/lint step as agent-codex) and return
    `(preflight_result_text, final_patch)`. Never runs ruff/black/compileall
    in this process — see the module docstring."""
    repo = work.repo_full_name
    mirror_path = ctx.mirrors.mirror_path(repo)
    request = {
        "run_id": work.run_id,
        "repo": repo,
        "mirror": str(mirror_path),
        "base_sha": base_sha,
        "patch_b64": base64.b64encode(patch).decode("ascii"),
        "tools": _tools_for_repo(ctx, repo),
    }
    try:
        result = ctx.codex.preflight(request)
    except CodexChildError as exc:
        reason = exc.body.get("preflight_failure") if exc.body else None
        raise PublishError(str(reason) if reason else exc.reason) from exc
    patch_b64 = result.get("patch_b64") or ""
    try:
        final_patch = base64.b64decode(patch_b64, validate=True) if patch_b64 else b""
    except (ValueError, TypeError) as exc:
        raise PublishError(f"invalid patch encoding from preflight: {exc}") from exc
    return str(result.get("preflight_result", "")), final_patch


def _commit(
    publish_dir: Path,
    env: Mapping[str, str],
    *,
    message: str,
    trailer: str,
    redactor: Redactor | None,
) -> str:
    _run_git(
        [
            "-c",
            f"user.name={_COMMIT_NAME}",
            "-c",
            f"user.email={_COMMIT_EMAIL}",
            "commit",
            "-m",
            message,
            "-m",
            trailer,
        ],
        cwd=publish_dir,
        env=env,
        error="commit failed",
        redactor=redactor,
    )
    result = _run_git(
        ["rev-parse", "HEAD"],
        cwd=publish_dir,
        env=env,
        error="could not read the new commit sha",
        redactor=redactor,
    )
    return result.stdout.decode("utf-8").strip()


def _push(
    ctx: ServiceContext, repo: str, publish_dir: Path, branch: str, env: Mapping[str, str]
) -> None:
    remote = ctx.mirrors.remote_url_for(repo)
    push_env = repos.git_auth_env(dict(env), ctx.mirrors.token_for(repo))
    _run_git(
        ["push", remote, f"HEAD:refs/heads/{branch}"],
        cwd=publish_dir,
        env=push_env,
        error="push failed",
        redactor=ctx.redactor,
    )


def _check_not_cancelled(ctx: ServiceContext, work: Work, cancel: threading.Event) -> None:
    """A synchronous lease check right before an irreversible step (push,
    PR creation). If the lease was lost since the last background
    heartbeat, or `cancel` is already set, abort before doing anything that
    cannot be undone. Setting `cancel` here means the caller's own
    callback/action-result helper -- which always checks `cancel` first --
    sends nothing: the Task Manager API already owns the outcome.
    """
    if cancel.is_set():
        raise PublishError("run was cancelled before publication", phase="publish")
    try:
        ctx.api.heartbeat(work.run_id, work.lease_id)
    except LeaseLost as exc:
        cancel.set()
        raise PublishError(
            f"lease lost before publication: {exc.detail}", phase="publish"
        ) from exc


def _pr_body(
    work: Work,
    task: Mapping[str, Any],
    *,
    is_public: bool,
    codex_summary: str,
    preflight_result: str,
) -> str:
    if is_public:
        body = (
            f"Task Manager task #{work.task_id} in {work.repo_full_name}\n\n"
            f"Requested: {task['title']}\n\n{task['description']}\n\n"
            "Created by Codex. Review the diff and target repository CI before merging.\n"
        )
    else:
        body = (
            f"Task Manager task #{work.task_id}\n\n"
            f"Requested: {task['title']}\n\n{task['description']}\n\n"
            "Created by Codex. Review the diff and CI results before merging.\n"
        )
    # Legacy `agent_task.check_diff` only `.strip()`s the Codex result text
    # before appending it to the PR body -- internal newlines/formatting are
    # kept, unlike the whitespace-collapsed text `failure_reason` uses for
    # the short "Codex izohi" callback note.
    summary = (codex_summary or "").strip()[:3000]
    if summary:
        body += f"\nCodex summary:\n\n{summary}\n"
    body += f"\nTrusted publisher preflight: {preflight_result}.\n"
    return body


def publish_implement(
    ctx: ServiceContext,
    work: Work,
    *,
    base_sha: str,
    branch: str,
    patch: bytes,
    task: Mapping[str, Any],
    is_public: bool,
    image_dir: Path | None,
    codex_summary: str,
    cancel: threading.Event,
    report_stage: Callable[[str], None],
) -> PublishResult:
    repo = work.repo_full_name
    mirror_path = ctx.mirrors.mirror_path(repo)
    checker = (
        ctx.trusted.public_agent_task.check_diff
        if is_public
        else ctx.trusted.agent_task.check_diff
    )

    # 1) Cheap, pure validation of the ORIGINAL patch in agent-svc's own
    #    checkout -- refuses blocked paths/credentials before the sandboxed
    #    preflight step ever runs.
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
    try:
        env = _git_env()
        _run_git(
            ["checkout", "-b", branch, base_sha],
            cwd=publish_dir,
            env=env,
            error="could not create the publish branch",
            redactor=ctx.redactor,
        )
        _apply_patch(publish_dir, patch, env, redactor=ctx.redactor)
        _check_diff(
            checker, cwd=publish_dir, task=task, image_dir=image_dir, is_public=is_public
        )
        report_stage("patch_validated")
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)

    # 2) Sandboxed preflight (ruff/black/compileall) as agent-codex.
    preflight_result, final_patch = _run_preflight(ctx, work, patch=patch, base_sha=base_sha)

    # 3) Apply the FINAL (post-preflight) patch to a fresh checkout and
    #    validate again before anything irreversible happens.
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
    try:
        env = _git_env()
        _run_git(
            ["checkout", "-b", branch, base_sha],
            cwd=publish_dir,
            env=env,
            error="could not create the publish branch",
            redactor=ctx.redactor,
        )
        _apply_patch(publish_dir, final_patch, env, redactor=ctx.redactor)
        _check_diff(
            checker, cwd=publish_dir, task=task, image_dir=image_dir, is_public=is_public
        )
        report_stage("preflight_passed")

        head_sha = _commit(
            publish_dir,
            env,
            message=f"feat(agent): work on task {work.task_id}",
            trailer=f"Agent-Run-ID: {work.run_id}",
            redactor=ctx.redactor,
        )

        if ctx.github.get_ref(repo, branch) is not None:
            raise PublishError("branch already exists")

        _check_not_cancelled(ctx, work, cancel)
        _push(ctx, repo, publish_dir, branch, env)
        report_stage("branch_pushed")

        _check_not_cancelled(ctx, work, cancel)
        body = _pr_body(
            work,
            task,
            is_public=is_public,
            codex_summary=codex_summary,
            preflight_result=preflight_result,
        )
        pr = ctx.github.create_pull(
            repo,
            head=branch,
            base=work.base_branch,
            title=f"Task #{work.task_id}: Codex change",
            body=body,
        )
        number = pr.get("number")
        pr_url = pr.get("html_url") or f"https://github.com/{repo}/pull/{number}"
        return PublishResult(head_sha=head_sha, pr_url=str(pr_url))
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)


def publish_correction(
    ctx: ServiceContext,
    work: Work,
    *,
    expected_head_sha: str,
    patch: bytes,
    cancel: threading.Event,
    report_stage: Callable[[str], None],
) -> PublishResult:
    repo = work.repo_full_name
    branch = work.branch
    if branch is None:
        raise PublishError("correction request has no branch")
    if ctx.github.get_ref(repo, branch) != expected_head_sha:
        raise PublishError("PR branch moved before publication")

    mirror_path = ctx.mirrors.mirror_path(repo)
    # Corrections always use the plain (non-public) validator, exactly like
    # the legacy `agent_release.py publish-correction`: repository/branch
    # approval was already proven when the original task/PR was opened, so
    # only the credential/image/symlink checks apply here, for every repo.
    checker = ctx.trusted.agent_task.check_diff
    task: dict[str, Any] = {
        "task_id": work.task_id,
        "run_id": work.run_id,
        "title": f"Owner correction for task {work.task_id}",
        "description": "",
        "base_branch": work.base_branch,
        "mode": "pr",
    }

    # 1) Cheap, pure validation of the ORIGINAL patch.
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
    try:
        env = _git_env()
        _run_git(
            ["checkout", "--detach", expected_head_sha],
            cwd=publish_dir,
            env=env,
            error="could not check out the PR head",
            redactor=ctx.redactor,
        )
        _apply_patch(publish_dir, patch, env, redactor=ctx.redactor)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=None)
        report_stage("patch_validated")
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)

    # 2) Sandboxed preflight as agent-codex.
    _preflight_result, final_patch = _run_preflight(
        ctx, work, patch=patch, base_sha=expected_head_sha
    )

    # 3) Apply the FINAL (post-preflight) patch to a fresh checkout.
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
    try:
        env = _git_env()
        _run_git(
            ["checkout", "--detach", expected_head_sha],
            cwd=publish_dir,
            env=env,
            error="could not check out the PR head",
            redactor=ctx.redactor,
        )
        _apply_patch(publish_dir, final_patch, env, redactor=ctx.redactor)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=None)
        report_stage("preflight_passed")

        new_head = _commit(
            publish_dir,
            env,
            message=f"fix(agent): apply owner correction for task {work.task_id}",
            trailer=f"Agent-Run-ID: {work.run_id}",
            redactor=ctx.redactor,
        )
        if new_head == expected_head_sha:
            raise PublishError("correction did not create a new commit")

        if ctx.github.get_ref(repo, branch) != expected_head_sha:
            raise PublishError("PR branch moved before publication")

        _check_not_cancelled(ctx, work, cancel)
        _push(ctx, repo, publish_dir, branch, env)
        report_stage("correction_pushed")

        pushed_sha = ctx.github.get_ref(repo, branch)
        if pushed_sha != new_head:
            raise PublishError("GitHub branch head differs from published correction")

        return PublishResult(head_sha=new_head)
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)
