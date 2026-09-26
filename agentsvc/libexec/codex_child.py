"""Sandboxed Codex process manager. Runs as user agent-codex via sudo.

Subcommands `prepare | exec | package | preflight | cleanup` read one JSON
request object (<=1 MiB, 12 MiB for `preflight`) from stdin and print JSON
(single object for prepare/package/preflight/cleanup, JSONL for exec) on
stdout. This process trusts nothing from stdin beyond the allowlists below:
every model/effort/sandbox/lane/cwd value is checked against a fixed set,
every run_id is checked against a fixed pattern, and every path the child
touches is resolved and confirmed to live inside `<work_root>/<run_id>/` (or
inside the mirrors directory for `prepare`'s read, or `TOOLS_DIR` for
`preflight`'s tool executables).

`preflight` runs the trusted formatter/lint script (ruff/black/compileall) as
agent-codex, never agent-svc: those tools execute inside a directory whose
content is entirely attacker-controlled (the agent's own patch), so running
them as agent-svc would let a malicious patch read every credential agent-svc
can reach (see agent-svc-design.md security round 2, blocker 1).

`agent-codex` is a dedicated system user for this sandbox only (design spec
REVISION 2) — it is deliberately NOT `codex-runner`, which is also the live
GitHub Actions self-hosted runner identity and whose home holds credentials
and tokens that Codex's own sandbox (same uid) could otherwise read.

Production invocation (pinned in sudoers): `/usr/bin/python3 -I
/opt/agent-svc/libexec/codex_child.py <sub>`. `-I` (isolated mode) is set by
the caller (`agent_svc.codex.CodexRunner`), not by this script, so that
running it directly in tests behaves the same as production.

Test overrides: module constants below can be overridden with environment
variables prefixed `AGENT_CHILD_TEST_`, but ONLY when `AGENT_CHILD_TEST_MODE=1`
is also set. This exists so unit tests can point the child at a scratch
directory and a fake `codex` binary without sudo. It is safe in production
because `sudo -n -u agent-codex` resets the environment before this script
ever runs (Defaults env_reset is the sudoers default and this deployment does
not add these variables to env_keep), so a caller cannot smuggle
`AGENT_CHILD_TEST_*` variables through sudo. As a second, independent guard we
also refuse test mode outright when the effective user is root or is already
`agent-codex` — the only user test mode is meant to run as.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import importlib.util
import json
import os
import pwd
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_REQUEST_BYTES = 1024 * 1024  # 1 MiB: a 250k-char diff prompt needs headroom.
# `preflight` carries a base64 patch (<=5 MB raw -> ~6.7 MB encoded) plus a
# tools map; 1 MiB is not enough headroom for that one subcommand.
PREFLIGHT_MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024  # cap any single stdout/stderr line we ever buffer.
STDOUT_QUEUE_MAXSIZE = 8192
MAX_FINAL_MESSAGE_CHARS = 20_000
MAX_STDERR_LINES = 40
MAX_STDERR_LINE_CHARS = 500
MAX_PATCH_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 8
MAX_TIMEOUT_S = 3600.0
MAX_IDLE_TIMEOUT_S = 1800.0
TREE_KILL_GRACE_S = 10.0
RUN_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LANES = {"code", "chat"}
MODELS = {"gpt-6-luna", "gpt-6-sol"}
EFFORTS = {"low", "medium", "high"}
SANDBOXES = {"read-only", "workspace-write"}
CWD_KINDS = {"wt", "empty"}
BAD_GIT_MODES = {"120000", "160000"}
BLOCKED_PATH_COMPONENTS = {".codex", ".agents", ".git"}

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36


def _test_mode_enabled() -> bool:
    if os.environ.get("AGENT_CHILD_TEST_MODE") != "1":
        return False
    if os.geteuid() == 0:
        return False
    try:
        user_name = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        return False
    return user_name != "agent-codex"


TEST_MODE = _test_mode_enabled()


def _test_override(name: str) -> str | None:
    if not TEST_MODE:
        return None
    return os.environ.get(f"AGENT_CHILD_TEST_{name}")


def _env_path(name: str, default: str) -> Path:
    override = _test_override(name)
    return Path(override) if override else Path(default)


def _env_str(name: str, default: str) -> str:
    override = _test_override(name)
    return override if override else default


def _env_float(name: str, default: float) -> float:
    override = _test_override(name)
    if not override:
        return default
    try:
        return float(override)
    except ValueError:
        return default


WORK_ROOT = _env_path("WORK_ROOT", "/srv/agent-svc/work")
MIRRORS_DIR = _env_path("MIRRORS_DIR", "/srv/agent-svc/mirrors")
# `preflight`'s tools map may only point inside here in production: pinned,
# root-owned executables under a path agent-codex cannot write to.
TOOLS_DIR = _env_path("TOOLS_DIR", "/opt/agent-svc/tools")
# `preflight` imports the trusted formatter/lint script from here by file
# path -- never via `sys.path`/package import -- the same trust boundary
# `agent_svc.trusted` uses on the agent-svc side.
TRUSTED_DIR = _env_path("TRUSTED_DIR", "/opt/agent-svc/trusted")
HOME_DIR = _env_str("HOME", "/home/agent-codex")
CODEX_BINARY = _env_str("CODEX_BINARY", "/opt/agent-svc/codex-cli/bin/codex")
PATH_VALUE = _env_str(
    "PATH",
    "/opt/agent-svc/node24/bin:/opt/agent-svc/codex-cli/bin:/usr/bin:/bin",
)
# Not one of the paths/values the design spec calls out as configurable, but
# tree-kill's 10s grace period would make every timeout/idle/SIGTERM test take
# 10+ seconds; letting tests shrink it (same AGENT_CHILD_TEST_MODE gate as
# everything else above) keeps the suite fast without changing production
# behavior (the default below matches the spec unless test mode is active).
TREE_KILL_GRACE_S = _env_float("TREE_KILL_GRACE_S", 10.0)


def _codex_homes() -> dict[str, str]:
    code = _test_override("CODEX_HOME_CODE")
    chat = _test_override("CODEX_HOME_CHAT")
    return {
        "code": code or "/home/agent-codex/.codex-code",
        "chat": chat or "/home/agent-codex/.codex-chat",
    }


CODEX_HOMES = _codex_homes()

GIT_ENV_BASE = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


class ChildRefusal(Exception):
    """A validated, expected refusal: reported as ``{"reason": ...}`` with exit 3."""


def _emit_error(reason: str) -> None:
    print(json.dumps({"reason": reason}), flush=True)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, never raising -- a malformed/spoofed usage
    field must not crash the whole process."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_run_id(value: Any) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ChildRefusal("invalid run_id")
    return value


def _run_dir(run_id: str) -> Path:
    root = WORK_ROOT.resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root:
        raise ChildRefusal("invalid run directory")
    return candidate


def _ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ChildRefusal("path escapes the run directory")
    return resolved


def _positive_number(value: Any, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChildRefusal("invalid timeout")
    number = float(value)
    if not (0 < number <= maximum):
        raise ChildRefusal("invalid timeout")
    return number


def _fresh_dir(path: Path, *, mode: int) -> None:
    """Remove `path` if present and recreate it empty at `mode`, so nothing
    left over from a previous call (stale output, a symlink planted by an
    escaped process, ...) is still there."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, mode=mode)


# --------------------------------------------------------------------------
# git helpers, shared by prepare and package
# --------------------------------------------------------------------------


def _git_argv(
    *args: str, no_textconv: bool = False, safe_directory: str | None = None
) -> list[str]:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.file.allow=always",
    ]
    if safe_directory is not None:
        # agent-svc owns the mirror, agent-codex clones it: modern git
        # refuses to operate across that ownership boundary ("dubious
        # ownership") unless explicitly told this exact path is fine.
        command += ["-c", f"safe.directory={safe_directory}"]
    if no_textconv:
        # Untrusted repositories can configure .gitattributes textconv/diff
        # filters that execute arbitrary commands on `git diff`. This is not
        # in the literal spec command line but is required so `package`
        # cannot be tricked into running attacker-controlled programs.
        command += ["-c", "diff.noTextconv=true", "-c", "core.attributesFile=/dev/null"]
    command += list(args)
    return command


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**GIT_ENV_BASE, "PATH": PATH_VALUE, "HOME": HOME_DIR}
    # Test-only passthrough: lets a test simulate git's "dubious ownership"
    # check firing (mirror owned by agent-svc, cloned by agent-codex)
    # without needing two real unix users. This only ever makes git MORE
    # strict, never disables a safety check, and -- like AGENT_CHILD_TEST_*
    # -- sudo's env_reset strips it from this process's environment in
    # production regardless.
    assume_different_owner = os.environ.get("GIT_TEST_ASSUME_DIFFERENT_OWNER")
    if assume_different_owner:
        env["GIT_TEST_ASSUME_DIFFERENT_OWNER"] = assume_different_owner
    if extra:
        env.update(extra)
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 120.0,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        raise ChildRefusal(f"git {args[0] if args else ''} failed: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChildRefusal("git command timed out") from exc


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def _validate_mirror(mirror: Any, repo: Any) -> Path:
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise ChildRefusal("invalid repo")
    if not isinstance(mirror, str):
        raise ChildRefusal("invalid mirror")
    root = MIRRORS_DIR.resolve()
    path = Path(mirror).resolve()
    if path != root and root not in path.parents:
        raise ChildRefusal("mirror is outside the configured mirrors directory")
    expected_name = repo.replace("/", "__") + ".git"
    if path.name != expected_name:
        raise ChildRefusal("mirror does not match repo")
    if not path.is_dir():
        raise ChildRefusal("mirror does not exist")
    return path


def _refuse_if_agent_config_present(root: Path) -> None:
    for current, dirs, files in os.walk(root):
        if Path(current) == root and ".git" in dirs:
            dirs.remove(".git")
        if any(name in dirs or name in files for name in BLOCKED_PATH_COMPONENTS):
            raise ChildRefusal("repository contains .codex, .agents, or .git")


def _clone_mirror(mirror: Path, wt: Path, *, run_dir: Path, env: dict[str, str]) -> None:
    """Clone the agent-svc-owned mirror as agent-codex across the ownership boundary.

    git only honors `safe.directory` from system/global config on older
    releases (the server's 2.43 ignores `-c safe.directory=`), so this exact
    mirror path is trusted through a private, single-use global config file
    that exists only for the duration of the clone. `-c` is kept too for
    newer git. Nothing else is trusted: no wildcard, no persistent config.
    """
    fd, config_path = tempfile.mkstemp(dir=run_dir, prefix=".gitconfig-clone-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f'[safe]\n\tdirectory = "{mirror}"\n')
        _run_git(
            _git_argv(
                "clone",
                "--no-checkout",
                "--no-hardlinks",
                str(mirror),
                str(wt),
                safe_directory=str(mirror),
            ),
            cwd=run_dir,
            env={**env, "GIT_CONFIG_GLOBAL": config_path},
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(config_path)


def cmd_prepare(request: dict[str, Any]) -> int:
    try:
        run_id = _validate_run_id(request.get("run_id"))
        run_dir = _run_dir(run_id)
        mirror = _validate_mirror(request.get("mirror"), request.get("repo"))
        base_sha = request.get("base_sha")
        if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
            raise ChildRefusal("invalid base_sha")
        wt = run_dir / "wt"
        if wt.exists():
            raise ChildRefusal("worktree already exists")
        for name in ("tmp", "images"):
            (run_dir / name).mkdir(parents=True, exist_ok=True, mode=0o770)
        env = _git_env()
        _clone_mirror(mirror, wt, run_dir=run_dir, env=env)
        _run_git(
            _git_argv("-C", str(wt), "checkout", "--detach", base_sha), cwd=run_dir, env=env
        )
        _refuse_if_agent_config_present(wt)
        head = _run_git(_git_argv("-C", str(wt), "rev-parse", "HEAD"), cwd=run_dir, env=env)
        head_sha = head.stdout.decode("ascii", "replace").strip()
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"prepare failed: {exc}")
        return 3
    print(json.dumps({"ok": True, "head": head_sha}), flush=True)
    return 0


# --------------------------------------------------------------------------
# package
# --------------------------------------------------------------------------


def _parse_raw_diff_z(raw_bytes: bytes) -> tuple[list[str], str | None]:
    """Path listing + safety, from `git diff --cached --raw -z --no-renames`.

    `-z` makes git emit each path exactly as it is on disk (NUL-terminated,
    never C-quoted), so this is the only place changed paths should be read
    from. Mode/symlink/submodule checking is handled separately, against the
    single `--binary` patch this run actually returns (see
    `_scan_patch_for_bad_modes`), not duplicated here.
    """
    parts = raw_bytes.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    changed_paths: list[str] = []
    index = 0
    while index < len(parts):
        meta = parts[index]
        index += 1
        if not meta.startswith(b":"):
            continue
        fields = meta[1:].split()
        if len(fields) < 4:
            continue
        if index >= len(parts):
            break
        path_bytes = parts[index]
        index += 1
        try:
            path = path_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return [], "changed path is not valid UTF-8"
        if any(
            component in BLOCKED_PATH_COMPONENTS for component in PurePosixPath(path).parts
        ):
            return [], f"refuses a blocked path: {path}"
        changed_paths.append(path)
    return changed_paths, None


_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(?:.*) b/(.*)$")
_MODE_LINE_PATTERNS = (
    re.compile(r"^new file mode (\d+)$"),
    re.compile(r"^old mode (\d+)$"),
    re.compile(r"^new mode (\d+)$"),
    re.compile(r"^deleted file mode (\d+)$"),
)
_INDEX_LINE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: (\d+))?$")


def _scan_patch_for_bad_modes(patch_text: str) -> str | None:
    """Parse the patch's own text headers (`new file mode`, `old mode`, `new
    mode`, `deleted file mode`, `index <a>..<b> <mode>`) for a symlink
    (120000) or submodule (160000) entry. Reading this off the one patch we
    actually return -- instead of a second, separate `--raw` call against the
    same index -- means the mode check can never disagree with what's in the
    patch."""
    current_path = "<unknown>"
    for line in patch_text.splitlines():
        header = _DIFF_GIT_HEADER_RE.match(line)
        if header:
            current_path = header.group(1)
            continue
        for pattern in _MODE_LINE_PATTERNS:
            match = pattern.match(line)
            if match and match.group(1) in BAD_GIT_MODES:
                return current_path
        match = _INDEX_LINE_RE.match(line)
        if match and match.group(1) in BAD_GIT_MODES:
            return current_path
    return None


def _package_worktree(directory: Path, *, out_dir: Path) -> tuple[bytes, list[str]]:
    """Diff `directory` (working tree + index) against HEAD: raw -z path
    listing for safety, then the one `--binary` patch actually returned.
    Shared by `package` (against `wt`) and `preflight` (against `pf`, after
    the trusted formatter/lint step has run) so the two can never disagree
    on what counts as a safe changed path or a bad file mode."""
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A fresh, 0700 (agent-codex only) directory for the temporary index:
    # codex's own sandbox is never given `out/` as a writable root, so
    # nothing spawned during `exec`/`preflight` could race this by writing
    # here.
    pkg_dir = Path(tempfile.mkdtemp(prefix="pkg-", dir=str(out_dir)))
    os.chmod(pkg_dir, 0o700)
    try:
        index_file = pkg_dir / "index"
        env = _git_env({"GIT_INDEX_FILE": str(index_file)})
        _run_git(_git_argv("read-tree", "HEAD"), cwd=directory, env=env)
        _run_git(_git_argv("add", "-A"), cwd=directory, env=env)
        raw = _run_git(
            _git_argv(
                "diff",
                "--cached",
                "--raw",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "HEAD",
                "--",
            ),
            cwd=directory,
            env=env,
        )
        changed_paths, bad_path_reason = _parse_raw_diff_z(raw.stdout)
        if bad_path_reason is not None:
            raise ChildRefusal(bad_path_reason)
        if not changed_paths:
            return b"", []
        patch = _run_git(
            _git_argv(
                "diff",
                "--cached",
                "--binary",
                "--no-renames",
                "--no-ext-diff",
                "HEAD",
                "--",
                no_textconv=True,
            ),
            cwd=directory,
            env=env,
        )
        patch_bytes = patch.stdout
        bad_mode_path = _scan_patch_for_bad_modes(patch_bytes.decode("utf-8", "replace"))
        if bad_mode_path is not None:
            raise ChildRefusal(f"unsupported change (symlink or submodule): {bad_mode_path}")
        if len(patch_bytes) > MAX_PATCH_BYTES:
            raise ChildRefusal("patch exceeds the size limit")
        return patch_bytes, changed_paths
    finally:
        shutil.rmtree(pkg_dir, ignore_errors=True)


def cmd_package(request: dict[str, Any]) -> int:
    try:
        run_id = _validate_run_id(request.get("run_id"))
        run_dir = _run_dir(run_id)
        wt = run_dir / "wt"
        if not wt.is_dir():
            raise ChildRefusal("worktree is not prepared")
        # The agent's own edits could have added .codex/.agents/.git since
        # prepare's one-time check; the diff-based check below catches new
        # ones specifically, this catches anything already sitting in the
        # tree regardless of whether this call's diff touches it.
        _refuse_if_agent_config_present(wt)
        patch_bytes, changed_paths = _package_worktree(wt, out_dir=run_dir / "out")
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"package failed: {exc}")
        return 3
    print(
        json.dumps(
            {
                "patch_b64": base64.b64encode(patch_bytes).decode("ascii") if patch_bytes else "",
                "changed_paths": changed_paths,
                "bytes": len(patch_bytes),
            }
        ),
        flush=True,
    )
    return 0


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------


def cmd_cleanup(request: dict[str, Any]) -> int:
    try:
        run_id = _validate_run_id(request.get("run_id"))
        run_dir = _run_dir(run_id)
        for name in ("wt", "tmp", "out", "pf"):
            target = run_dir / name
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ChildRefusal(f"refusing to remove non-directory {name}")
            if target.exists():
                shutil.rmtree(target)
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"cleanup failed: {exc}")
        return 3
    print(json.dumps({"ok": True}), flush=True)
    return 0


# --------------------------------------------------------------------------
# preflight: run the trusted formatter/lint step as agent-codex, never
# agent-svc -- see agent-svc-design.md security round 2, blocker 1. Ruff,
# Black and (worst of all) `python -m compileall` all execute in a directory
# whose *content* is fully attacker-controlled (the agent's own patch);
# running any of them as agent-svc would let a patch read every credential
# agent-svc can reach. This subcommand clones the mirror, applies the patch,
# and runs the pinned trusted preflight script -- all as agent-codex, in a
# scratch directory agent-svc never touches directly.
# --------------------------------------------------------------------------


def _validate_tools(tools: Any) -> dict[str, str]:
    """A `{name: absolute path}` map, every path pinned inside `TOOLS_DIR`.

    Always returns a real `dict` (never `None`): the trusted
    ``agent_preflight.run`` treats `tools=None` as "legacy mode" and may
    `pip install` -- agent-codex must never do that, so an absent/empty
    request map still becomes `{}`, which keeps it on the local-executor
    path (resolve-and-version-check only, no installs).
    """
    if tools is None:
        return {}
    if not isinstance(tools, dict):
        raise ChildRefusal("invalid tools")
    tools_root = TOOLS_DIR.resolve()
    resolved: dict[str, str] = {}
    for name, raw_path in tools.items():
        if not isinstance(name, str) or not isinstance(raw_path, str):
            raise ChildRefusal("invalid tools entry")
        if not os.path.isabs(raw_path):
            raise ChildRefusal(f"tool path must be absolute: {name}")
        path = Path(raw_path).resolve()
        if path != tools_root and tools_root not in path.parents:
            raise ChildRefusal(f"tool path is outside the tools directory: {name}")
        resolved[name] = str(path)
    return resolved


def _load_trusted_preflight() -> Any:
    path = (TRUSTED_DIR / "agent_preflight.py").resolve()
    spec = importlib.util.spec_from_file_location(
        "agent_svc_child._trusted_agent_preflight", path
    )
    if spec is None or spec.loader is None:
        raise ChildRefusal(f"cannot load trusted preflight: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_preflight(request: dict[str, Any]) -> int:
    pf_dir: Path | None = None
    try:
        run_id = _validate_run_id(request.get("run_id"))
        run_dir = _run_dir(run_id)
        repo = request.get("repo")
        mirror = _validate_mirror(request.get("mirror"), repo)
        base_sha = request.get("base_sha")
        if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
            raise ChildRefusal("invalid base_sha")
        patch_b64 = request.get("patch_b64")
        if not isinstance(patch_b64, str) or not patch_b64:
            raise ChildRefusal("invalid patch_b64")
        try:
            patch_bytes = base64.b64decode(patch_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ChildRefusal("invalid patch encoding") from exc
        if not patch_bytes:
            raise ChildRefusal("empty patch")
        if len(patch_bytes) > MAX_PATCH_BYTES:
            raise ChildRefusal("patch exceeds the size limit")
        # Reject a symlink/submodule entry in the CALLER's patch before ever
        # touching disk with it -- the same check `package` runs on its own
        # output, run here first against the input.
        bad_mode_path = _scan_patch_for_bad_modes(patch_bytes.decode("utf-8", "replace"))
        if bad_mode_path is not None:
            raise ChildRefusal(f"unsupported change (symlink or submodule): {bad_mode_path}")
        tools = _validate_tools(request.get("tools"))

        pf_dir = run_dir / "pf"
        _fresh_dir(pf_dir, mode=0o700)
        env = _git_env()
        _clone_mirror(mirror, pf_dir, run_dir=run_dir, env=env)
        _run_git(
            _git_argv("-C", str(pf_dir), "checkout", "--detach", base_sha),
            cwd=run_dir,
            env=env,
        )
        _refuse_if_agent_config_present(pf_dir)
        _run_git(
            _git_argv("-C", str(pf_dir), "apply", "--index"),
            cwd=run_dir,
            env=env,
            input_bytes=patch_bytes,
        )
        _refuse_if_agent_config_present(pf_dir)

        if not isinstance(repo, str):
            raise ChildRefusal("invalid repo")
        preflight_module = _load_trusted_preflight()
        try:
            preflight_result = preflight_module.run(repo, pf_dir, tools=tools)
        except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            # A trusted-preflight failure is not a child refusal: report the
            # exact legacy failure text the caller forwards to the owner,
            # alongside a generic `reason` for anything reading only that.
            failure_text = preflight_module.failure_reason(exc)
            print(
                json.dumps({"reason": "trusted preflight failed", "preflight_failure": failure_text}),
                flush=True,
            )
            return 3

        patch_bytes_out, changed_paths = _package_worktree(pf_dir, out_dir=run_dir / "out")
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"preflight failed: {exc}")
        return 3
    finally:
        if pf_dir is not None:
            shutil.rmtree(pf_dir, ignore_errors=True)
    print(
        json.dumps(
            {
                "ok": True,
                "patch_b64": (
                    base64.b64encode(patch_bytes_out).decode("ascii") if patch_bytes_out else ""
                ),
                "changed_paths": changed_paths,
                "preflight_result": preflight_result,
            }
        ),
        flush=True,
    )
    return 0


# --------------------------------------------------------------------------
# exec: process tree management
# --------------------------------------------------------------------------

ProcTable = Sequence[tuple[int, int]]
ProcTableProvider = Callable[[], ProcTable]


def _proc_table_linux() -> list[tuple[int, int]]:
    table: list[tuple[int, int]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat_text = Path("/proc", entry, "stat").read_text()
        except OSError:
            continue
        close = stat_text.rfind(")")
        if close == -1:
            continue
        rest = stat_text[close + 2 :].split()
        if len(rest) < 2:
            continue
        try:
            ppid = int(rest[1])
        except ValueError:
            continue
        table.append((int(entry), ppid))
    return table


def _proc_table_ps() -> list[tuple[int, int]]:
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return []
    table: list[tuple[int, int]] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            table.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return table


def default_proc_table() -> list[tuple[int, int]]:
    if sys.platform.startswith("linux"):
        return _proc_table_linux()
    return _proc_table_ps()


def _descendant_pids(root_pid: int, table: ProcTable) -> list[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in table:
        children.setdefault(ppid, []).append(pid)
    result: list[int] = []
    frontier = [root_pid]
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            if child in result:
                continue
            result.append(child)
            frontier.append(child)
    return result


def _proc_state(pid: int) -> str | None:
    """The process's state code (e.g. "Z" for zombie), Linux only. A zombie
    still answers `kill(pid, 0)` successfully (its pid is still allocated
    until reaped), so treating it as "alive" would spin forever waiting for
    something that already exited."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = stat_text.rfind(")")
    if close == -1:
        return None
    rest = stat_text[close + 2 :].split()
    return rest[0] if rest else None


def _pid_alive(pid: int) -> bool:
    if (_proc_state(pid) or "").upper() == "Z":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_pid(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def _reap_if_child(pid: int) -> None:
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def kill_tree(
    root_pid: int,
    *,
    proc_table_provider: ProcTableProvider = default_proc_table,
    grace_s: float = TREE_KILL_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """SIGTERM the whole descendant tree rooted at ``root_pid``, then SIGKILL
    whatever a FRESH snapshot still finds alive after ``grace_s`` seconds.
    Sandboxed commands run in a different session/process group than the
    codex process, so we find them by walking the ppid chain rather than by
    process group. Only signaling pids a fresh snapshot re-confirms (never
    the original, now possibly-stale, target list) avoids SIGKILLing an
    unrelated process that happened to reuse a pid after the real target
    already exited."""
    table = proc_table_provider()
    targets = [root_pid, *_descendant_pids(root_pid, table)]
    for pid in targets:
        _signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in targets):
            return
        sleep(0.2)
    fresh_table = proc_table_provider()
    fresh_targets = {root_pid, *_descendant_pids(root_pid, fresh_table)}
    for pid in fresh_targets:
        if _pid_alive(pid):
            _signal_pid(pid, signal.SIGKILL)


def _reap_all_descendants(
    *,
    proc_table_provider: ProcTableProvider = default_proc_table,
    grace_s: float = TREE_KILL_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Terminate and reap every descendant of THIS process -- not just the
    spawned `codex` pid's own tree. Called on every `cmd_exec` exit path
    (normal EOF, timeout, idle, SIGTERM, exception).

    With `PR_SET_CHILD_SUBREAPER` set on us (see `cmd_exec`), a
    double-forked/setsid grandchild that outlives its immediate parent
    re-parents to US on Linux, not to init, which is what actually makes it
    reachable by walking descendants of our own pid instead of just the
    `codex` pid's tree (`kill_tree` above catches the common case fast; this
    is the catch-all net for anything that slipped past it). Subreaper is
    Linux-only, so this is a best-effort no-op for genuinely orphaned
    processes on other platforms -- there is no portable equivalent.
    """
    own_pid = os.getpid()
    table = proc_table_provider()
    targets = _descendant_pids(own_pid, table)
    for pid in targets:
        _signal_pid(pid, signal.SIGTERM)
        _reap_if_child(pid)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        for pid in targets:
            _reap_if_child(pid)
        if not any(_pid_alive(pid) for pid in targets):
            break
        sleep(0.2)
    fresh_table = proc_table_provider()
    fresh_targets = _descendant_pids(own_pid, fresh_table)
    for pid in fresh_targets:
        if _pid_alive(pid):
            _signal_pid(pid, signal.SIGKILL)
    reap_deadline = time.monotonic() + 2.0
    remaining = set(fresh_targets)
    while remaining and time.monotonic() < reap_deadline:
        for pid in list(remaining):
            try:
                reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                remaining.discard(pid)
                continue
            if reaped_pid == pid:
                remaining.discard(pid)
        if remaining:
            sleep(0.05)


def _prctl(option: int, arg2: int = 0) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(option, arg2, 0, 0, 0) == 0
    except OSError:
        return False


# --------------------------------------------------------------------------
# exec: request validation and command construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecParams:
    run_dir: Path
    lane: str
    cwd_kind: str
    cwd_path: Path
    model: str
    effort: str
    sandbox: str
    multi_agent: bool
    prompt: str
    images: list[Path]
    output_schema: dict[str, Any] | None
    timeout_s: float
    idle_timeout_s: float
    codex_home: Path


def _validate_exec_request(request: dict[str, Any]) -> ExecParams:
    run_id = _validate_run_id(request.get("run_id"))
    run_dir = _run_dir(run_id)
    lane = request.get("lane")
    if lane not in LANES:
        raise ChildRefusal("invalid lane")
    cwd_kind = request.get("cwd")
    if cwd_kind not in CWD_KINDS:
        raise ChildRefusal("invalid cwd")
    model = request.get("model")
    if model not in MODELS:
        raise ChildRefusal("invalid model")
    effort = request.get("effort")
    if effort not in EFFORTS:
        raise ChildRefusal("invalid effort")
    sandbox = request.get("sandbox")
    if sandbox not in SANDBOXES:
        raise ChildRefusal("invalid sandbox")
    multi_agent = request.get("multi_agent")
    if not isinstance(multi_agent, bool):
        raise ChildRefusal("invalid multi_agent")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ChildRefusal("invalid prompt")
    raw_images = request.get("images", [])
    if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGES:
        raise ChildRefusal("invalid images")
    images: list[Path] = []
    images_root = run_dir / "images"
    for raw in raw_images:
        if not isinstance(raw, str):
            raise ChildRefusal("invalid image path")
        images.append(_ensure_within(Path(raw), images_root))
    output_schema = request.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise ChildRefusal("invalid output_schema")
    timeout_s = _positive_number(request.get("timeout_s"), maximum=MAX_TIMEOUT_S)
    idle_timeout_s = _positive_number(
        request.get("idle_timeout_s"), maximum=MAX_IDLE_TIMEOUT_S
    )
    if cwd_kind == "wt":
        cwd_path = run_dir / "wt"
        if not cwd_path.is_dir():
            raise ChildRefusal("worktree is not prepared")
        # The agent's own previous turn (correction flows re-run exec on the
        # same worktree) could have added .codex/.agents/.git since prepare
        # or the last exec; refuse before letting codex run again.
        _refuse_if_agent_config_present(cwd_path)
    else:
        cwd_path = run_dir / "empty"
        _fresh_dir(cwd_path, mode=0o700)
    return ExecParams(
        run_dir=run_dir,
        lane=lane,
        cwd_kind=cwd_kind,
        cwd_path=cwd_path,
        model=model,
        effort=effort,
        sandbox=sandbox,
        multi_agent=multi_agent,
        prompt=prompt,
        images=images,
        output_schema=output_schema,
        timeout_s=timeout_s,
        idle_timeout_s=idle_timeout_s,
        codex_home=Path(CODEX_HOMES[lane]),
    )


def _build_exec_command(params: ExecParams, schema_path: Path, final_path: Path) -> list[str]:
    command = [
        CODEX_BINARY,
        "exec",
        "--sandbox",
        params.sandbox,
        "-c",
        "approval_policy=never",
        "-c",
        f"model_reasoning_effort={params.effort}",
        "--model",
        params.model,
        "--json",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
    ]
    if params.cwd_kind == "empty":
        # Nothing to check out a git repo from in a fresh scratch dir --
        # codex would otherwise warn/refuse about not being in one.
        command.append("--skip-git-repo-check")
    if not params.multi_agent:
        command += ["-c", "features.multi_agent=false"]
    if params.output_schema is not None:
        command += ["--output-schema", str(schema_path)]
    command += ["--output-last-message", str(final_path)]
    for image in params.images:
        command += ["--image", str(image)]
    # `-i/--image` takes multiple values, so it would otherwise greedily
    # swallow the trailing `-` (read prompt from stdin) as one more image
    # path; `--` forces everything after it to be positional.
    command += ["--", "-"]
    return command


def _exec_env(params: ExecParams) -> dict[str, str]:
    return {
        "HOME": HOME_DIR,
        "CODEX_HOME": str(params.codex_home),
        "PATH": PATH_VALUE,
        "TMPDIR": str(params.run_dir / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def _preexec_pdeathsig() -> None:
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


# --------------------------------------------------------------------------
# exec: streaming and usage accounting
# --------------------------------------------------------------------------


def _put_important(sink: queue.Queue[Any], item: Any) -> None:
    """Enqueue `item` even if the queue is full, evicting the oldest buffered
    item if necessary. Used only for the EOF sentinel (losing it would hang
    the consumer forever) -- ordinary lines are dropped instead when full."""
    while True:
        try:
            sink.put_nowait(item)
            return
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                sink.get_nowait()


def _readline_capped(stream: Any, limit: int) -> bytes:
    return stream.readline(limit)


def _pump_stdout_lines(stream: Any, sink: queue.Queue[str | None]) -> None:
    try:
        while True:
            raw_line = _readline_capped(stream, MAX_LINE_BYTES)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            # The consumer is falling behind; drop, don't block codex.
            with contextlib.suppress(queue.Full):
                sink.put_nowait(text)
    except (OSError, ValueError):
        pass
    finally:
        _put_important(sink, None)


def _pump_stderr(stream: Any, sink: deque[str]) -> None:
    try:
        while True:
            raw_line = _readline_capped(stream, MAX_LINE_BYTES)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            sink.append(text[:MAX_STDERR_LINE_CHARS])
    except (OSError, ValueError):
        pass


@dataclass
class StreamOutcome:
    timed_out: bool = False
    idle_killed: bool = False
    thread_started: dict[str, Any] | None = None
    fallback_usage: dict[str, int] | None = None


def _stream_and_wait(
    process: subprocess.Popen[bytes],
    timeout_s: float,
    idle_timeout_s: float,
    signaled: threading.Event,
    *,
    kill: Callable[[int], None] = lambda pid: kill_tree(pid),
) -> StreamOutcome:
    lines: queue.Queue[str | None] = queue.Queue(maxsize=STDOUT_QUEUE_MAXSIZE)
    reader = threading.Thread(
        target=_pump_stdout_lines, args=(process.stdout, lines), daemon=True
    )
    reader.start()

    outcome = StreamOutcome()
    deadline = time.monotonic() + timeout_s
    idle_deadline = time.monotonic() + idle_timeout_s

    def _handle_line(line: str) -> None:
        idle_nonlocal[0] = time.monotonic() + idle_timeout_s
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        event_type = event.get("type") if isinstance(event, dict) else None
        if isinstance(event_type, str) and event_type.startswith("agent_svc."):
            # Only our own synthesized result frame may use this namespace;
            # a compromised/misbehaving codex forging one must not reach the
            # parent as if it were authoritative.
            return
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if event_type == "thread.started":
            outcome.thread_started = event
        elif event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                outcome.fallback_usage = {key: _safe_int(usage.get(key)) for key in USAGE_KEYS}

    idle_nonlocal = [idle_deadline]

    while True:
        now = time.monotonic()
        if signaled.is_set():
            kill(process.pid)
            break
        if now >= deadline:
            outcome.timed_out = True
            kill(process.pid)
            break
        if now >= idle_nonlocal[0]:
            outcome.idle_killed = True
            kill(process.pid)
            break
        remaining = min(deadline, idle_nonlocal[0]) - now
        wait_for = min(max(remaining, 0.05), 0.5)
        try:
            line = lines.get(timeout=wait_for)
        except queue.Empty:
            continue
        if line is None:
            break
        _handle_line(line)

    # Drain whatever is already buffered without blocking further.
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            break
        if line is None:
            break
        _handle_line(line)

    reader.join(timeout=2)
    return outcome


USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


# Real Codex 0.156.1 rollout JSONL shape (verified against the live server,
# see spike-results.md and design-doc REVISION 3): each line is
# {"ordinal": int, "timestamp": str, "type": str, "payload": {...}}.
#
# The FIRST line (ordinal 0) is always session_meta:
#   {"type": "session_meta", "payload": {
#       "id": <this file's own thread id>,
#       "session_id": <ROOT session/thread id -- same for every file in the
#                       tree, at any sub-agent depth>,
#       "source": "exec" | {"subagent": {"thread_spawn": {...}}},
#       "parent_thread_id": ... (sub-agents only),
#       "subagent_history_start_ordinal": int (sub-agents only), ...}}
# A sub-agent's rollout ALSO carries a SECOND session_meta line (ordinal 1,
# the forked parent history) that must be ignored -- so we only ever read the
# first line for session identity, never scan further for another one.
#
# Usage lines are {"type": "event_msg", "payload": {"type": "token_count",
# "info": {"total_token_usage": {...}, "last_token_usage": {...}}}};
# total_token_usage is cumulative PER FILE and counts only that thread's own
# requests (a child's usage is not part of the parent's numbers or vice
# versa), so summing each matched file's LAST total_token_usage across the
# whole tree gives the true total. {"type": "token_usage_record", ...} lines
# carry ids only (thread_id/session_id/turn_id) and are not needed here.
#
# Because every file in a run's tree (root + every sub-agent at any depth)
# stamps the SAME root session_id in its own first line, matching is a flat
# equality check against that one id -- no parent/child chain walking needed.


def _session_meta_first_line(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or value.get("type") != "session_meta":
        return None
    payload = value.get("payload")
    return payload if isinstance(payload, dict) else None


def _rollout_is_in_root_tree(payload: dict[str, Any], root_thread_id: str) -> bool:
    # `session_id` covers the root file and every sub-agent at any depth;
    # `id` is a fallback for the root's own file in case its session_id ever
    # differs from its own id.
    return payload.get("session_id") == root_thread_id or payload.get("id") == root_thread_id


def _relevant_rollouts(
    sessions_dir: Path, root_thread_id: str, *, min_mtime: float | None = None
) -> list[Path]:
    matches: list[Path] = []
    if not sessions_dir.is_dir():
        return matches
    # Path shape is <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl;
    # rglob walks the date subdirectories, and the mtime filter is a cheap
    # prefilter against unrelated older runs before opening each file.
    for rollout in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        if min_mtime is not None:
            try:
                if rollout.stat().st_mtime < min_mtime:
                    continue
            except OSError:
                continue
        payload = _session_meta_first_line(rollout)
        if payload is None:
            continue
        if _rollout_is_in_root_tree(payload, root_thread_id):
            matches.append(rollout)
    return matches


def _last_token_usage(path: Path) -> dict[str, int] | None:
    last: dict[str, int] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict) or value.get("type") != "event_msg":
                    continue
                inner = value.get("payload")
                if not isinstance(inner, dict) or inner.get("type") != "token_count":
                    continue
                info = inner.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("total_token_usage")
                if isinstance(usage, dict):
                    last = {key: _safe_int(usage.get(key)) for key in USAGE_KEYS}
    except OSError:
        return None
    return last


def _collect_and_delete_usage(
    codex_home: Path,
    thread_started: dict[str, Any] | None,
    fallback_usage: dict[str, int] | None,
    *,
    min_mtime: float | None = None,
) -> tuple[dict[str, int] | None, str | None]:
    thread_id = thread_started.get("thread_id") if isinstance(thread_started, dict) else None
    if not isinstance(thread_id, str):
        return fallback_usage, None
    matches = _relevant_rollouts(codex_home / "sessions", thread_id, min_mtime=min_mtime)
    if not matches:
        return fallback_usage, thread_id
    totals = dict.fromkeys(USAGE_KEYS, 0)
    found_any = False
    for rollout in matches:
        usage = _last_token_usage(rollout)
        if usage is None:
            continue
        found_any = True
        for key in totals:
            totals[key] += usage[key]
    for rollout in matches:
        with contextlib.suppress(OSError):
            rollout.unlink()
    return (totals if found_any else fallback_usage), thread_id


def _read_final_message(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FINAL_MESSAGE_CHARS)
    except OSError:
        return ""


def cmd_exec(request: dict[str, Any]) -> int:
    try:
        params = _validate_exec_request(request)
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        # `_validate_exec_request` itself creates/wipes the "empty" cwd and
        # re-scans "wt" for blocked paths; either can fail with a plain
        # OSError (permissions, disk full, ...) that must not crash the
        # process instead of reporting a clean refusal.
        _emit_error(f"exec setup failed: {exc}")
        return 3

    try:
        _fresh_dir(params.run_dir / "out", mode=0o700)
        (params.run_dir / "tmp").mkdir(parents=True, exist_ok=True, mode=0o770)
        out_dir = params.run_dir / "out"
        schema_path = out_dir / "schema.json"
        final_path = out_dir / "final.txt"
        # Belt-and-suspenders: `_fresh_dir` above already guarantees neither
        # file can pre-exist, but make that explicit rather than implicit.
        for stale in (schema_path, final_path):
            with contextlib.suppress(OSError):
                stale.unlink()
        if params.output_schema is not None:
            schema_path.write_text(json.dumps(params.output_schema), encoding="utf-8")
    except OSError as exc:
        _emit_error(f"exec setup failed: {exc}")
        return 3

    # Wall-clock start, used only as a cheap mtime prefilter when hunting for
    # this run's rollout files afterwards; a couple of seconds of slack
    # covers clock/mtime granularity without risking excluding this run's own
    # files.
    rollout_min_mtime = time.time() - 2.0

    command = _build_exec_command(params, schema_path, final_path)
    env = _exec_env(params)
    stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)

    try:
        process = subprocess.Popen(
            command,
            cwd=str(params.cwd_path),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_preexec_pdeathsig,
        )
    except OSError as exc:
        _emit_error(f"failed to start codex: {exc}")
        return 3

    # Mark OURSELVES (codex_child.py's own process, not codex) as a reaper
    # for our descendants: a double-forked/setsid grandchild that outlives
    # its immediate parent then re-parents to US (Linux only) instead of
    # init, so `_reap_all_descendants` below can actually find and kill it
    # regardless of how deeply it tried to detach itself.
    _prctl(PR_SET_CHILD_SUBREAPER, 1)

    try:
        assert process.stdin is not None and process.stderr is not None
        try:
            process.stdin.write(params.prompt.encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

        stderr_thread = threading.Thread(
            target=_pump_stderr, args=(process.stderr, stderr_tail), daemon=True
        )
        stderr_thread.start()

        signaled = threading.Event()

        def _handle_sigterm(_signum: int, _frame: Any) -> None:
            signaled.set()

        previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
        try:
            outcome = _stream_and_wait(
                process, params.timeout_s, params.idle_timeout_s, signaled
            )
        finally:
            signal.signal(signal.SIGTERM, previous_handler)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        stderr_thread.join(timeout=2)

        usage, thread_id = _collect_and_delete_usage(
            params.codex_home,
            outcome.thread_started,
            outcome.fallback_usage,
            min_mtime=rollout_min_mtime,
        )
        final_message = _read_final_message(final_path)
        exit_code = process.returncode if process.returncode is not None else -1

        frame = {
            "type": "agent_svc.result",
            "exit_code": exit_code,
            "timed_out": outcome.timed_out,
            "idle_killed": outcome.idle_killed,
            "final_message": final_message,
            "thread_id": thread_id,
            "usage": usage,
            "stderr_tail": list(stderr_tail),
        }
        print(json.dumps(frame, ensure_ascii=False), flush=True)
        return 0
    finally:
        # Every exit path -- normal EOF, timeout, idle, SIGTERM, or an
        # exception above -- reaches here before `cmd_exec` actually
        # returns, so nothing sandboxed can survive us.
        _reap_all_descendants()


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

SUBCOMMANDS: dict[str, Callable[[dict[str, Any]], int]] = {
    "prepare": cmd_prepare,
    "exec": cmd_exec,
    "package": cmd_package,
    "preflight": cmd_preflight,
    "cleanup": cmd_cleanup,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SUBCOMMANDS:
        _emit_error("usage: codex_child.py <prepare|exec|package|preflight|cleanup>")
        return 2
    cap = PREFLIGHT_MAX_REQUEST_BYTES if argv[1] == "preflight" else MAX_REQUEST_BYTES
    raw = sys.stdin.buffer.read(cap + 1)
    if len(raw) > cap:
        _emit_error("request exceeds the size limit")
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _emit_error("invalid JSON request")
        return 2
    if not isinstance(request, dict):
        _emit_error("request must be a JSON object")
        return 2
    return SUBCOMMANDS[argv[1]](request)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
