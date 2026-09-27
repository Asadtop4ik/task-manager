"""Shared test fakes and fixtures for the WP6/WP9 handler tests.

`FakeApi`/`FakeGitHub`/`FakeCodexRunner` implement just the methods the
handlers call, with recorded calls and scriptable results — no network, no
sudo, no real Codex. `make_github_remote`/`FakeGitHub.get_ref` use a real
local bare git repository standing in for GitHub, so a push (via
`agent_svc.publish`, going through the same `MirrorManager.remote_url_for`
hook the trusted mirrors use) is proven with real git, not a mock.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_svc import repos
from agent_svc.codex import CodexResult
from agent_svc.config import build_settings, load_config
from agent_svc.context import ServiceContext
from agent_svc.journal import Journal
from agent_svc.log import Logger, Redactor
from agent_svc.trusted import TrustedModules

REPO_ROOT = Path(__file__).resolve().parents[2]
TRUSTED_SOURCE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "scripts" / "agent_task.py",
    REPO_ROOT / "scripts" / "public_agent_task.py",
    REPO_ROOT / "scripts" / "agent_preflight.py",
    REPO_ROOT / "scripts" / "agent_release.py",
    REPO_ROOT / "scripts" / "agent_images.py",
    REPO_ROOT / "scripts" / "agent_pr_review.py",
    REPO_ROOT / "backend" / "app" / "services" / "agent_repos.py",
)


def copy_trusted_dir(dest: Path) -> Path:
    """A flat copy of the real trusted scripts, exactly as `install_agent_svc.sh` lays them out."""
    dest.mkdir(parents=True, exist_ok=True)
    for source in TRUSTED_SOURCE_FILES:
        shutil.copy2(source, dest / source.name)
    return dest


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "seed",
    "GIT_AUTHOR_EMAIL": "seed@example.com",
    "GIT_COMMITTER_NAME": "seed",
    "GIT_COMMITTER_EMAIL": "seed@example.com",
}


def _run(args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )


# Public alias: other test modules building their own small git fixtures use
# this instead of reaching into the private `_run`.
run_git = _run


def make_github_remote(path: Path, *, branch: str = "main") -> str:
    """A bare repo with one commit on `branch`, standing in for GitHub. Returns its sha."""
    _run(["git", "init", "--bare", "--quiet", "--initial-branch=main", str(path)])
    seed = path.parent / f"{path.name}-seed"
    _run(["git", "clone", "--quiet", str(path), str(seed)])
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=seed)
    _run(["git", "commit", "--quiet", "-m", "seed"], cwd=seed)
    _run(["git", "branch", "-M", branch], cwd=seed)
    _run(["git", "push", "--quiet", "origin", branch], cwd=seed)
    sha = _run(["git", "rev-parse", branch], cwd=seed).stdout.strip()
    shutil.rmtree(seed, ignore_errors=True)
    return sha


def make_patch(remote: Path, base_sha: str, files: dict[str, str]) -> bytes:
    """A real, appliable unified-diff patch, built the same way `codex_child.py package` does."""
    with tempfile.TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str) / "scratch"
        _run(["git", "clone", "--quiet", str(remote), str(scratch)])
        _run(["git", "checkout", "--quiet", "--detach", base_sha], cwd=scratch)
        for relative, content in files.items():
            target = scratch / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _run(["git", "add", "-A"], cwd=scratch)
        result = subprocess.run(
            ["git", "diff", "--cached", "--binary", "HEAD"],
            cwd=str(scratch),
            check=True,
            capture_output=True,
        )
        return result.stdout


def push_new_branch(remote: Path, base_sha: str, branch: str) -> None:
    """Push `branch` at `base_sha` into `remote`, without changing any content."""
    with tempfile.TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str) / "scratch"
        _run(["git", "clone", "--quiet", str(remote), str(scratch)])
        _run(["git", "checkout", "--quiet", "--detach", base_sha], cwd=scratch)
        _run(["git", "push", "--quiet", str(remote), f"HEAD:refs/heads/{branch}"], cwd=scratch)


def force_move_branch(remote: Path, base_sha: str, branch: str) -> None:
    """Simulate a race: push one extra commit onto `branch`, moving it past `base_sha`."""
    with tempfile.TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str) / "scratch"
        _run(["git", "clone", "--quiet", str(remote), str(scratch)])
        _run(["git", "checkout", "--quiet", "--detach", base_sha], cwd=scratch)
        (scratch / "RACE.md").write_text("raced\n", encoding="utf-8")
        _run(["git", "add", "-A"], cwd=scratch)
        _run(["git", "commit", "--quiet", "-m", "race"], cwd=scratch)
        _run(
            ["git", "push", "--quiet", "--force", str(remote), f"HEAD:refs/heads/{branch}"],
            cwd=scratch,
        )


def push_run_commit(remote: Path, branch: str, run_id: str, *, task_id: int = 7) -> str:
    """Push a branch whose tip commit carries `Agent-Run-ID: <run_id>` — for
    branch-exists-recovery tests. Returns the new tip sha."""
    with tempfile.TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str) / "scratch"
        _run(["git", "clone", "--quiet", str(remote), str(scratch)])
        (scratch / "WORK.md").write_text("work\n", encoding="utf-8")
        _run(["git", "add", "-A"], cwd=scratch)
        _run(
            [
                "git",
                "-c",
                "user.name=Business AI Codex",
                "-c",
                "user.email=codex@users.noreply.github.com",
                "commit",
                "--quiet",
                "-m",
                f"feat(agent): work on task {task_id}",
                "-m",
                f"Agent-Run-ID: {run_id}",
            ],
            cwd=scratch,
        )
        _run(["git", "push", "--quiet", str(remote), f"HEAD:refs/heads/{branch}"], cwd=scratch)
        return _run(["git", "rev-parse", "HEAD"], cwd=scratch).stdout.strip()


def rev_parse_or_none(remote: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def commit_log(remote: Path, ref: str, fmt: str = "%an <%ae>%n%s%n%b") -> str:
    return _run(["git", "log", "-1", f"--format={fmt}", ref], cwd=remote).stdout


class FakeApi:
    """Records every call; `heartbeat` effects are consumed in queued order."""

    def __init__(self) -> None:
        self.heartbeats: list[tuple[str, str]] = []
        self.stages: list[tuple[str, str, str, str | None]] = []
        self.callbacks: list[dict[str, Any]] = []
        self.action_results: list[dict[str, Any]] = []
        self._heartbeat_effects: list[Any] = []

    def queue_heartbeat_effects(self, *effects: Any) -> None:
        self._heartbeat_effects.extend(effects)

    def heartbeat(self, run_id: str, lease_id: str) -> datetime:
        self.heartbeats.append((run_id, lease_id))
        if self._heartbeat_effects:
            effect = self._heartbeat_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect
        return datetime.now(UTC) + timedelta(minutes=5)

    def stage(
        self, run_id: str, lease_id: str, stage: str, *, error: str | None = None
    ) -> None:
        self.stages.append((run_id, lease_id, stage, error))

    def callback(self, run_id: str, lease_id: str, payload: dict[str, Any]) -> None:
        self.callbacks.append(dict(payload))

    def action_result(self, run_id: str, lease_id: str, payload: dict[str, Any]) -> None:
        self.action_results.append(dict(payload))


class FakeGitHub:
    """`get_ref`/`commit_message` read a real bare repo; PR bookkeeping is a plain dict."""

    def __init__(self, *, remote_path: Path | None = None) -> None:
        self.remote_path = remote_path
        self.pulls: dict[tuple[str, int], dict[str, Any]] = {}
        self.open_pull_by_head: dict[tuple[str, str], dict[str, Any] | None] = {}
        self.created_pulls: list[dict[str, Any]] = []
        self._next_pr_number = 100
        self.statuses: list[dict[str, Any]] = []

    def get_ref(self, repo: str, branch: str) -> str | None:
        if self.remote_path is None:
            return None
        result = subprocess.run(
            ["git", "-C", str(self.remote_path), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def commit_message(self, repo: str, sha: str) -> str:
        assert self.remote_path is not None
        result = subprocess.run(
            ["git", "-C", str(self.remote_path), "log", "-1", "--format=%B", sha],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        number = self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "head": {"ref": head},
            "base": {"ref": base},
            "title": title,
            "body": body,
        }
        self.created_pulls.append(pr)
        self.pulls[(repo, number)] = pr
        return pr

    def get_pull(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self.pulls[(repo, pr_number)]

    def find_open_pull_by_head(self, repo: str, branch: str) -> dict[str, Any] | None:
        return self.open_pull_by_head.get((repo, branch))

    def set_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str,
        *,
        target_url: str | None = None,
    ) -> None:
        self.statuses.append(
            {
                "repo": repo,
                "sha": sha,
                "state": state,
                "context": context,
                "description": description,
            }
        )


class FakeCodexRunner:
    """Scripted `CodexResult`s and `package` patches; records every call."""

    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, Any]] = []
        self.run_exec_calls: list[dict[str, Any]] = []
        self.package_calls: list[dict[str, Any]] = []
        self.preflight_calls: list[dict[str, Any]] = []
        self.cleanup_calls: list[dict[str, Any]] = []
        self._exec_results: list[CodexResult] = []
        self._package_results: list[dict[str, Any]] = []
        self._preflight_results: list[dict[str, Any] | Exception] = []
        self.prepare_error: Exception | None = None
        self.package_error: Exception | None = None
        self.run_discussion_calls: list[dict[str, Any]] = []
        self._discussion_results: list[dict[str, Any] | Exception] = []

    def queue_exec_result(self, result: CodexResult) -> None:
        self._exec_results.append(result)

    def queue_discussion_result(self, result: dict[str, Any] | Exception) -> None:
        self._discussion_results.append(result)

    def run_discussion(
        self, request: dict[str, Any], *, timeout_s: float, cancel: Any = None
    ) -> dict[str, Any]:
        self.run_discussion_calls.append(request)
        result = self._discussion_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def queue_package_result(self, result: dict[str, Any]) -> None:
        self._package_results.append(result)

    def queue_preflight_result(self, result: dict[str, Any] | Exception) -> None:
        self._preflight_results.append(result)

    def prepare(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        self.prepare_calls.append(request)
        if self.prepare_error is not None:
            raise self.prepare_error
        return {"ok": True, "head": request.get("base_sha")}

    def run_exec(
        self,
        request: dict[str, Any],
        *,
        on_event: Any,
        heartbeat: Any = None,
        heartbeat_interval_s: float = 30.0,
        cancel: Any = None,
    ) -> CodexResult:
        self.run_exec_calls.append(request)
        if cancel is not None and cancel.is_set():
            return CodexResult(
                exit_code=0,
                timed_out=False,
                cancelled=True,
                idle_killed=False,
                final_message="",
                usage=None,
                stderr_tail=[],
                frame=None,
            )
        return self._exec_results.pop(0)

    def package(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        self.package_calls.append(request)
        if self.package_error is not None:
            raise self.package_error
        return self._package_results.pop(0)

    def preflight(
        self, request: dict[str, Any], *, timeout_s: float = 180.0
    ) -> dict[str, Any]:
        self.preflight_calls.append(request)
        if self._preflight_results:
            result = self._preflight_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        # Default: preflight passes the same patch straight through --
        # matching a real trusted run that found nothing to reformat, so
        # existing tests that never queue a preflight result keep working.
        return {
            "ok": True,
            "patch_b64": request.get("patch_b64", ""),
            "changed_paths": [],
            "preflight_result": "fake preflight ok",
        }

    def cleanup(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        self.cleanup_calls.append(request)
        return {"ok": True}


class FakeChatApi:
    """Minimal `TaskManagerApi` stand-in for chat lane handler tests.

    `handle_intake`/`handle_discussion` never lease their own work (the
    `ChatLane` already did that and hands them an already-validated
    `IntakeLease`/`DiscussionLease`), so this only needs the image-download
    and result-report methods those two handlers actually call, plus call
    recording for assertions.
    """

    def __init__(self) -> None:
        self._images: dict[tuple[str, int], tuple[bytes, str]] = {}
        self.intake_results: list[dict[str, Any]] = []
        self.discussion_results: list[dict[str, Any]] = []

    def set_image(self, kind: str, index: int, data: bytes, mime: str) -> None:
        self._images[(kind, index)] = (data, mime)

    def intake_image(
        self, intake_id: int, lease_id: str, index: int, *, timeout: float | None = None
    ) -> tuple[bytes, str]:
        return self._images[("intake", index)]

    def report_intake_result(
        self, intake_id: int, *, revision: int, lease_id: str, result: dict[str, Any]
    ) -> None:
        self.intake_results.append(
            {"intake_id": intake_id, "revision": revision, "lease_id": lease_id, **result}
        )

    def discussion_image(
        self,
        discussion_id: int,
        lease_id: str,
        index: int,
        *,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        return self._images[("discussion", index)]

    def report_discussion_result(
        self,
        discussion_id: int,
        *,
        revision: int,
        lease_id: str,
        thread_id: str | None,
        response: str | None,
        error: str | None,
    ) -> None:
        self.discussion_results.append(
            {
                "discussion_id": discussion_id,
                "revision": revision,
                "lease_id": lease_id,
                "thread_id": thread_id,
                "response": response,
                "error": error,
            }
        )


_SECRETS = {
    "agent_svc_token": "svc-token",
    "callback_token": "callback-token",
    "intake_worker_token": "intake-token",
    "github_agent_token": "agent-token",
    "github_public_agent_token": "public-token",
    "github_qa_token": "qa-token",
}


def build_test_context(
    tmp: Path,
    *,
    github_remote: Path | None = None,
    codex: FakeCodexRunner | None = None,
    api: FakeApi | None = None,
    github: FakeGitHub | None = None,
) -> ServiceContext:
    """A `ServiceContext` wired from real trusted scripts and a real mirror,
    with `FakeApi`/`FakeGitHub`/`FakeCodexRunner` standing in for the network,
    GitHub, and the sandboxed Codex child."""
    trusted_dir = copy_trusted_dir(tmp / "trusted")
    state_dir = tmp / "state"
    work_root = tmp / "work"
    mirrors_dir = tmp / "mirrors"
    for directory in (state_dir, work_root, mirrors_dir):
        directory.mkdir(parents=True, exist_ok=True)
    config = load_config(tmp / "absent-config.json")
    config = {
        **config,
        "trusted_dir": str(trusted_dir),
        "state_dir": str(state_dir),
        "work_root": str(work_root),
        "mirrors_dir": str(mirrors_dir),
        "runs_dir": str(state_dir / "runs"),
    }
    settings = build_settings(config, _SECRETS)
    catalog = repos.load_catalog(trusted_dir)
    redactor = Redactor(settings.secret_values())
    logger = Logger(redactor, stream=io.StringIO())
    remote_url = (
        str(github_remote) if github_remote is not None else "https://example.invalid/none.git"
    )
    mirrors = repos.MirrorManager(
        settings.mirrors_dir,
        lambda _repo: "dummy-token",
        approved_branches=catalog.approved_pairs(),
        remote_url_for=lambda _repo: remote_url,
        redactor=redactor,
    )
    return ServiceContext(
        settings=settings,
        logger=logger,
        redactor=redactor,
        api=api if api is not None else FakeApi(),  # type: ignore[arg-type]
        github=github if github is not None else FakeGitHub(remote_path=github_remote),  # type: ignore[arg-type]
        mirrors=mirrors,
        journal=Journal(settings.runs_dir, logger=logger),
        codex=codex if codex is not None else FakeCodexRunner(),  # type: ignore[arg-type]
        catalog=catalog,
        trusted=TrustedModules(trusted_dir),
    )
