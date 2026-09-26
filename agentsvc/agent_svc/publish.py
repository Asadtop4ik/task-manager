"""Clean-checkout publication: apply an agent patch, run the trusted preflight,
commit with the fixed identity/message/trailer, and push — for a fresh
`implement` branch and for an existing `correction` branch alike.

Every git process here runs hardened the same way the trusted GitHub Actions
publisher does (see `agent-svc-design.md`): no system/global git config, no
hooks, no attributes-driven filters that could alter the applied patch. The
push token lives only in the one push subprocess's environment, via
`repos.git_auth_env` — the exact helper `MirrorManager` uses for fetches —
never in argv, a git config file, or a log line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repos
from .api import Work
from .context import ServiceContext

_GIT_TIMEOUT_S = 120
_COMMIT_NAME = "Business AI Codex"
_COMMIT_EMAIL = "codex@users.noreply.github.com"
_PUBLISH_DIR_MODE = 0o700

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
            "diff.noTextconv=true",
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
) -> subprocess.CompletedProcess[bytes]:
    result = _git(args, cwd=cwd, env=env, input_bytes=input_bytes)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
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
    )
    publish_dir.chmod(_PUBLISH_DIR_MODE)
    return publish_dir


def _apply_patch(publish_dir: Path, patch: bytes, env: Mapping[str, str]) -> None:
    if not patch.strip():
        raise PublishError("agent produced no file changes")
    _run_git(
        ["apply", "--index"],
        cwd=publish_dir,
        env=env,
        input_bytes=patch,
        error="patch did not apply cleanly",
    )


def _check_diff(
    checker: Callable[..., bool],
    *,
    cwd: Path,
    task: Mapping[str, Any],
    image_dir: Path | None,
) -> None:
    try:
        checker(cwd=cwd, task=dict(task), image_dir=image_dir)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise PublishError(str(exc)) from exc


def _preflight(ctx: ServiceContext, repo: str, publish_dir: Path) -> str:
    tools = _tools_for_repo(ctx, repo)
    try:
        return str(ctx.trusted.agent_preflight.run(repo, publish_dir, tools=tools))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        reason = ctx.trusted.agent_preflight.failure_reason(exc)
        raise PublishError(reason) from exc


def _commit(publish_dir: Path, env: Mapping[str, str], *, message: str, trailer: str) -> str:
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
    )
    result = _run_git(
        ["rev-parse", "HEAD"],
        cwd=publish_dir,
        env=env,
        error="could not read the new commit sha",
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
    )


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
    summary = " ".join((codex_summary or "").split())[:3000]
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
    report_stage: Callable[[str], None],
) -> PublishResult:
    repo = work.repo_full_name
    mirror_path = ctx.mirrors.mirror_path(repo)
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
    checker = (
        ctx.trusted.public_agent_task.check_diff
        if is_public
        else ctx.trusted.agent_task.check_diff
    )
    try:
        env = _git_env()
        _run_git(
            ["checkout", "-b", branch, base_sha],
            cwd=publish_dir,
            env=env,
            error="could not create the publish branch",
        )
        _apply_patch(publish_dir, patch, env)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=image_dir)
        report_stage("patch_validated")

        preflight_result = _preflight(ctx, repo, publish_dir)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=image_dir)
        report_stage("preflight_passed")

        head_sha = _commit(
            publish_dir,
            env,
            message=f"feat(agent): work on task {work.task_id}",
            trailer=f"Agent-Run-ID: {work.run_id}",
        )

        if ctx.github.get_ref(repo, branch) is not None:
            raise PublishError("branch already exists")

        _push(ctx, repo, publish_dir, branch, env)
        report_stage("branch_pushed")

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
    report_stage: Callable[[str], None],
) -> PublishResult:
    repo = work.repo_full_name
    branch = work.branch
    if branch is None:
        raise PublishError("correction request has no branch")
    if ctx.github.get_ref(repo, branch) != expected_head_sha:
        raise PublishError("PR branch moved before publication")

    mirror_path = ctx.mirrors.mirror_path(repo)
    publish_dir = _prepare_checkout(ctx, work.run_id, mirror_path)
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
    try:
        env = _git_env()
        _run_git(
            ["checkout", "--detach", expected_head_sha],
            cwd=publish_dir,
            env=env,
            error="could not check out the PR head",
        )
        _apply_patch(publish_dir, patch, env)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=None)
        report_stage("patch_validated")

        _preflight(ctx, repo, publish_dir)
        _check_diff(checker, cwd=publish_dir, task=task, image_dir=None)
        report_stage("preflight_passed")

        new_head = _commit(
            publish_dir,
            env,
            message=f"fix(agent): apply owner correction for task {work.task_id}",
            trailer=f"Agent-Run-ID: {work.run_id}",
        )
        if new_head == expected_head_sha:
            raise PublishError("correction did not create a new commit")

        if ctx.github.get_ref(repo, branch) != expected_head_sha:
            raise PublishError("PR branch moved before publication")

        _push(ctx, repo, publish_dir, branch, env)
        report_stage("correction_pushed")

        pushed_sha = ctx.github.get_ref(repo, branch)
        if pushed_sha != new_head:
            raise PublishError("GitHub branch head differs from published correction")

        return PublishResult(head_sha=new_head)
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)
