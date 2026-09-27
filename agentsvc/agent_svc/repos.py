"""The trusted project catalog, and bare-mirror management for local clones.

`load_catalog` imports the root-owned `agent_repos.py` by file path, the same
trust boundary `ops/project_catalog.py` relies on, without mutating
`sys.path`. `MirrorManager` keeps one bare mirror per approved repo under the
mirrors directory, fetching only the one approved branch with a token that is
never written to disk, argv, or a git config file: it lives only in the
environment of the single `git fetch` subprocess. `make_run_dir` provisions
the per-run work directory the same way.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .journal import RUN_ID_RE
from .log import Redactor

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT_S = 120
_STDERR_TAIL_CHARS = 500
# Agent-opened PR branches: `codex/task-<task_id>-<run_id>` (see the backend's
# `_run_branch`). MirrorManager.fetch never pulls anything else besides a
# repo's approved base branch.
_AGENT_BRANCH_RE = re.compile(r"^codex/task-\d+-[0-9a-f-]{36}$")
# Achieves the same group-readable outcome as a process-wide umask, without
# mutating global umask state from multiple lane threads at once. Setgid
# (02000) so new entries inherit the mirrors tree's group (REVISION 2:
# `agentwork`, so agent-codex can clone) instead of the creating process's.
_DIR_MODE = 0o2750
_FILE_MODE = 0o640

# Work-dir permissions (REVISION 2): the service's own UMask (0027) only
# strips rwx/setgid/sticky bits inconsistently, so these are always applied
# explicitly rather than relied on via a directory-creation mode.
# Sticky (01000) + setgid (02000): agent-codex (group agentwork, write access
# to create/edit its own files) cannot rename or delete agent-svc-owned
# entries inside the run dir, only its own.
_RUN_DIR_MODE = 0o3770
# images/ stays agent-svc-owned only; agent-codex does not write here.
_RUN_IMAGES_MODE = 0o2750


@dataclass(frozen=True)
class RepoInfo:
    project_key: str
    full_name: str
    branch: str
    private: bool
    ci_jobs: tuple[str, ...]
    pr_ci_jobs: tuple[str, ...]
    pr_ci_workflow: str
    images: tuple[tuple[str, str], ...]
    qa_only: bool


@dataclass(frozen=True)
class Catalog:
    repos: tuple[RepoInfo, ...]

    def approved_pairs(self) -> dict[str, str]:
        return {item.full_name: item.branch for item in self.repos}

    def get(self, full_name: str) -> RepoInfo | None:
        return next((item for item in self.repos if item.full_name == full_name), None)

    @property
    def public_repos(self) -> tuple[str, ...]:
        return tuple(item.full_name for item in self.repos if not item.private)

    @property
    def qa_repo(self) -> str | None:
        return next((item.full_name for item in self.repos if item.qa_only), None)

    @property
    def dispatch_repo(self) -> str | None:
        return next(
            (item.full_name for item in self.repos if item.private and not item.qa_only), None
        )


def _import_agent_repos(trusted_dir: str | Path) -> ModuleType:
    path = Path(trusted_dir) / "agent_repos.py"
    if not path.is_file():
        raise FileNotFoundError(f"trusted catalog not found: {path}")
    spec = importlib.util.spec_from_file_location("agent_svc._trusted_agent_repos", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trusted catalog: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_catalog(trusted_dir: str | Path) -> Catalog:
    module = _import_agent_repos(trusted_dir)
    module.validate_catalog()
    entries = (*module.REPOSITORIES, module.QA_REPOSITORY)
    return Catalog(
        tuple(
            RepoInfo(
                project_key=item.project_key,
                full_name=item.full_name,
                branch=item.branch,
                private=item.private,
                ci_jobs=tuple(item.ci_jobs),
                pr_ci_jobs=tuple(item.pr_ci_jobs),
                pr_ci_workflow=item.pr_ci_workflow,
                images=tuple(item.images),
                qa_only=item.qa_only,
            )
            for item in entries
        )
    )


def git_auth_env(base_env: dict[str, str], token: str) -> dict[str, str]:
    """Add a one-shot HTTP Basic auth header to `base_env` for one git process.

    Shared by `MirrorManager` (fetch) and `agent_svc.publish` (push) so the
    token lives only in the environment of the single git subprocess that
    needs it: never in argv, a git config file on disk, or a log line.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(base_env)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def _mirror_dir_name(repo: str) -> str:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"invalid repository full name: {repo!r}")
    return f"{owner}__{name}.git"


class MirrorManager:
    def __init__(
        self,
        mirrors_dir: str | Path,
        token_for: Callable[[str], str],
        *,
        approved_branches: Mapping[str, str],
        remote_url_for: Callable[[str], str] | None = None,
        command_runner: Callable[..., Any] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._mirrors_dir = Path(mirrors_dir)
        self._token_for = token_for
        self._approved_branches = dict(approved_branches)
        self._remote_url_for = remote_url_for or (
            lambda repo: f"https://github.com/{repo}.git"
        )
        self._run = command_runner or subprocess.run
        self._redactor = redactor
        # One lock per repo (not one global lock): the code lane (implement/
        # correction/review) and the chat lane (intake/discussion) share
        # this one `MirrorManager` and can each fetch a DIFFERENT repo's
        # mirror concurrently, but two `git fetch`/`rev-parse` calls against
        # the SAME bare mirror directory at once (one from each lane) would
        # otherwise race on its refs/objects.
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_guard = threading.Lock()

    def _lock_for(self, repo: str) -> threading.Lock:
        with self._repo_locks_guard:
            lock = self._repo_locks.get(repo)
            if lock is None:
                lock = threading.Lock()
                self._repo_locks[repo] = lock
            return lock

    def mirror_path(self, repo: str) -> Path:
        return self._mirrors_dir / _mirror_dir_name(repo)

    def remote_url_for(self, repo: str) -> str:
        """The GitHub remote URL `agent_svc.publish` must push to for `repo`.

        Exposes the same `remote_url_for` hook the constructor takes (real
        GitHub in production, a local `file://` bare repo in tests) so the
        publisher pushes to exactly the remote this manager fetches from.
        """
        return self._remote_url_for(repo)

    def token_for(self, repo: str) -> str:
        """The push/fetch credential for `repo`, from the same token selector."""
        return self._token_for(repo)

    def ensure(self, repo: str) -> Path:
        path = self.mirror_path(repo)
        if not (path / "HEAD").is_file():
            self._mirrors_dir.mkdir(parents=True, exist_ok=True)
            self._mirrors_dir.chmod(_DIR_MODE)
            self._git(["init", "--bare", str(path)], cwd=None, env=self._base_env())
            self._set_group_readable(path)
        return path

    def _validate_branch(self, repo: str, branch: str) -> None:
        approved_base = self._approved_branches.get(repo)
        if approved_base is None:
            raise ValueError(f"refusing to fetch from an unapproved repository: {repo}")
        if branch == approved_base or _AGENT_BRANCH_RE.fullmatch(branch):
            return
        raise ValueError(f"refusing to fetch unapproved branch {branch!r} for {repo}")

    def fetch(self, repo: str, branch: str, *, timeout: float | None = None) -> str:
        """Fetch `branch` and return its exact commit sha.

        `timeout` (seconds) overrides the default per-git-call timeout for
        BOTH git calls this makes -- used by the chat lane to clamp a fetch
        to whatever remains of its own job budget (`ChatRun.remaining_s()`);
        omitted (the default), this reproduces the exact prior behavior.
        Serialized per-repo (see `_lock_for`) so a concurrent fetch of the
        SAME mirror from another lane waits instead of racing.
        """
        self._validate_branch(repo, branch)
        with self._lock_for(repo):
            path = self.ensure(repo)
            refspec = f"+refs/heads/{branch}:refs/heads/{branch}"
            self._git(
                ["fetch", "--prune", self._remote_url_for(repo), refspec],
                cwd=path,
                env=self._fetch_env(repo),
                timeout=timeout,
            )
            self._set_group_readable(path)
            result = self._git(
                ["rev-parse", f"refs/heads/{branch}"],
                cwd=path,
                env=self._base_env(),
                timeout=timeout,
            )
        sha = (result.stdout or "").strip()
        if not _SHA_RE.fullmatch(sha):
            raise ValueError(f"mirror fetch for {repo} did not produce a valid commit sha")
        return sha

    def _base_env(self) -> dict[str, str]:
        return {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def _fetch_env(self, repo: str) -> dict[str, str]:
        # The token exists only in this one subprocess's environment: never in
        # argv, never in a git config file, never in the remote URL.
        return git_auth_env(self._base_env(), self._token_for(repo))

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str],
        timeout: float | None = None,
    ) -> Any:
        # Always capture: uncaptured stderr would otherwise inherit our fds
        # and reach journald unredacted, and a failure's error message would
        # be empty instead of carrying the (redacted) reason.
        result = self._run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else _GIT_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            stderr = (getattr(result, "stderr", None) or "").strip()
            if self._redactor is not None:
                stderr = self._redactor.redact(stderr)
            raise RuntimeError(
                f"git {args[0]} failed (exit {result.returncode}): {stderr[-_STDERR_TAIL_CHARS:]}"
            )
        return result

    def _set_group_readable(self, path: Path) -> None:
        for item in path.rglob("*"):
            try:
                item.chmod(_DIR_MODE if item.is_dir() else _FILE_MODE)
            except FileNotFoundError:
                continue
        path.chmod(_DIR_MODE)


def make_run_dir(work_root: str | Path, run_id: str) -> Path:
    """Create `<work_root>/<run_id>/` and its `images/` subdirectory.

    `work_root` itself is provisioned externally (tmpfiles.d, 2750
    agent-svc:agentwork); this only owns the per-run tree beneath it. Modes
    are always set explicitly (3770 / 2750) because the service's UMask
    (0027) never sets the setgid/sticky bits a plain `mkdir(mode=...)` would
    need. The run dir is sticky (in addition to setgid) so agent-codex, which
    only needs group-write to create/edit its own files, cannot rename or
    delete an agent-svc-owned entry (e.g. swap out a subdirectory) inside it.
    Refuses a run directory (or its `images/`) that already exists as a
    symlink, since `mkdir(exist_ok=True)` would otherwise silently follow it.
    """
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    run_dir = Path(work_root) / run_id
    if run_dir.is_symlink():
        raise ValueError(f"refusing a symlinked run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(_RUN_DIR_MODE)

    images_dir = run_dir / "images"
    if images_dir.is_symlink():
        raise ValueError(f"refusing a symlinked images directory: {images_dir}")
    images_dir.mkdir(exist_ok=True)
    images_dir.chmod(_RUN_IMAGES_MODE)

    return run_dir
