"""Sandboxed Codex process manager. Runs as user agent-codex via sudo.

Subcommands `prepare | exec | package | preflight | cleanup | discussion |
check` read one JSON request object (<=1 MiB, 12 MiB for `preflight` and
`check`) from stdin and print JSON (single object for every subcommand but
exec, JSONL for exec) on stdout. `check` (the factory Check Gate) runs one
task's locked acceptance checks inside `codex sandbox`; see its section. This process trusts nothing from stdin beyond the allowlists below:
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
legacy GitHub Actions runner identity (decommissioned, account kept) and whose home holds credentials
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
import resource
import secrets
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
MODELS = {"gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol"}
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
# `preflight`'s own internal deadline for the trusted formatter/lint step,
# strictly less than the parent's own default `preflight` timeout (180s,
# see `agent_svc/codex.py`): if ruff/black/compileall hangs, this fires
# FIRST, so the child gets a chance to kill everything and return a clean
# refusal before the parent starts SIGTERM/SIGKILL-ing `sudo` itself (which
# would not necessarily kill what `sudo` supervises -- see
# `CodexRunner._call_with_grace`).
PREFLIGHT_DEADLINE_S = _env_float("PREFLIGHT_DEADLINE_S", 170.0)


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
        # `core.attributesFile` only overrides the GLOBAL fallback
        # attributes file (like `core.excludesFile` for gitignore) -- it
        # does NOT stop git from reading a `.gitattributes` committed inside
        # the repository tree itself, which is exactly where attacker
        # content lives here. That file alone is not the real protection;
        # the caller must ALSO pass the literal `--no-textconv` flag as one
        # of its own diff arguments (the only thing that reliably disables
        # textconv regardless of where a diff driver was assigned from).
        # `core.attributesFile=/dev/null` stays as a second, harmless layer
        # against the global-config vector.
        command += ["-c", "core.attributesFile=/dev/null"]
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


def _clone_mirror(
    mirror: Path, wt: Path, *, run_dir: Path, env: dict[str, str], timeout: float = 120.0
) -> None:
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
            timeout=timeout,
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


# Git quotes the whole `"a/path"`/`"b/path"` token (C-style backslash
# escapes) whenever a path needs it (non-ASCII bytes by default, embedded
# quotes/backslashes, ...); the unquoted alternative is tried second so a
# name genuinely containing a literal `"` still falls back sanely.
_DIFF_GIT_HEADER_RE = re.compile(
    r'^diff --git (?:"a/(?:[^"\\]|\\.)*"|a/.*) '
    r'(?:"b/(?P<quoted>(?:[^"\\]|\\.)*)"|b/(?P<plain>.*))$'
)
# Header lines that carry a file mode. git parses the mode with
# `strtoul(..., 8)`, so `0120000`, `+120000`, two spaces or a trailing space
# all still mean a symlink: anything but the exact spellings of a regular file
# is refused instead of trying to out-parse git.
_MODE_LINE_PREFIXES = ("new file mode", "old mode", "new mode", "deleted file mode")
_ALLOWED_PATCH_MODES = frozenset({"100644", "100755"})
_INDEX_LINE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: (\S+))?$")


def _scan_patch_for_bad_modes(patch_text: str) -> str | None:
    """Parse the patch's own text headers (`new file mode`, `old mode`, `new
    mode`, `deleted file mode`, `index <a>..<b> <mode>`) for any mode other
    than a plain or executable file -- a symlink (120000), a submodule
    (160000), or a spelling git would still read as one. Reading this off the
    one patch we actually return -- instead of a second, separate `--raw`
    call against the same index -- means the mode check can never disagree
    with what's in the patch. Returns the offending path, else `None`."""
    current_path = "<unknown>"
    for line in patch_text.splitlines():
        header = _DIFF_GIT_HEADER_RE.match(line)
        if header:
            quoted = header.group("quoted")
            current_path = quoted if quoted is not None else header.group("plain")
            continue
        for prefix in _MODE_LINE_PREFIXES:
            if line.startswith(prefix):
                rest = line[len(prefix) :]
                if not (rest.startswith(" ") and rest[1:] in _ALLOWED_PATCH_MODES):
                    return current_path
        if line.startswith("index "):
            match = _INDEX_LINE_RE.match(line)
            if match is None or (
                match.group(1) is not None and match.group(1) not in _ALLOWED_PATCH_MODES
            ):
                return current_path
    return None


def _refuse_bad_staged_modes(directory: Path, *, cwd: Path, env: dict[str, str]) -> None:
    """Ask git itself, after `git apply --index`, whether anything staged
    against HEAD is a symlink or submodule -- the authoritative check behind
    `_scan_patch_for_bad_modes`, which only reads patch text."""
    raw = _run_git(
        _git_argv(
            "-C", str(directory), "diff", "--cached", "--raw", "-z", "--no-renames", "HEAD", "--"
        ),
        cwd=cwd,
        env=env,
    )
    for meta in raw.stdout.split(b"\x00"):
        if not meta.startswith(b":"):
            continue
        fields = meta[1:].split()
        if len(fields) >= 2 and fields[1].decode("ascii", "replace") in BAD_GIT_MODES:
            raise ChildRefusal("unsupported change (symlink or submodule) after apply")


def _package_worktree(
    directory: Path, *, out_dir: Path, use_existing_index: bool = False
) -> tuple[bytes, list[str]]:
    """Diff `directory` (working tree + index) against HEAD: raw -z path
    listing for safety, then the one `--binary` patch actually returned.
    Shared by `package` (against `wt`) and `preflight` (against `pf`, after
    the trusted formatter/lint step has run) so the two can never disagree
    on what counts as a safe changed path or a bad file mode.

    `use_existing_index` (preflight): package exactly what the trusted
    preflight staged — the applied patch plus its formatter fixes — instead
    of `git add -A`, so files the tools leave behind (`__pycache__/*.pyc`,
    caches) are never published."""
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
        if use_existing_index:
            # Snapshot the index into our private 0700 dir before reading it.
            shutil.copyfile(directory / ".git" / "index", index_file)
        else:
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
                "--no-textconv",
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
                "patch_b64": (
                    base64.b64encode(patch_bytes).decode("ascii") if patch_bytes else ""
                ),
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
        for name in ("wt", "tmp", "out", "pf", "ck", "ck-home"):
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


@contextlib.contextmanager
def _preflight_sandbox_environment(private_home: Path, private_cache: Path) -> Any:
    """Diverts real fd 1 to fd 2, and gives the trusted preflight (ruff,
    black, compileall) a minimal, private environment, for the duration of
    the wrapped call only.

    Stdout: `agent_preflight.run` shells out to these tools with no
    `stdout=PIPE` of its own, so their normal, harmless-looking output
    ("All checks passed!", reformatted-file names, ...) writes straight to
    fd 1 -- which for THIS process is the JSON protocol channel the parent
    (`agent_svc.codex.CodexRunner._call_with_grace`) reads one `json.loads`
    over. Any such text corrupts that single JSON line. Redirecting the
    real fd (not just `sys.stdout`, since the tools are separate processes
    that inherit fds, not our Python object) to fd 2 for exactly this call
    routes it to stderr instead, harmlessly.

    Environment: HOME is never `/home/agent-codex` here (these tools must
    not read or write anything under the real agent-codex home, e.g. an
    accidental `~/.cache`), and cache directories are private per-run
    scratch space, never shared across runs or repos.
    """
    overrides = {
        "HOME": str(private_home),
        "RUFF_NO_CACHE": "true",
        "BLACK_CACHE_DIR": str(private_cache / "black"),
        "XDG_CACHE_HOME": str(private_cache / "xdg"),
    }
    saved_env = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    sys.stdout.flush()
    saved_stdout_fd = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved_stdout_fd, 1)
        os.close(saved_stdout_fd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _preflight_watchdog(
    done: threading.Event, timed_out: threading.Event, deadline_s: float
) -> None:
    """If `done` is not set within `deadline_s`, kill every descendant of
    this process (the trusted preflight's own tool subprocesses) and record
    that this was a timeout, not an ordinary tool failure. Requires
    `PR_SET_CHILD_SUBREAPER` already set on this process, so a subprocess
    that itself forked is still reachable."""
    if not done.wait(deadline_s):
        timed_out.set()
        _reap_all_descendants()


def cmd_preflight(request: dict[str, Any]) -> int:
    pf_dir: Path | None = None
    run_dir: Path | None = None
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
        _refuse_bad_staged_modes(pf_dir, cwd=run_dir, env=env)
        _refuse_if_agent_config_present(pf_dir)

        if not isinstance(repo, str):
            raise ChildRefusal("invalid repo")
        preflight_module = _load_trusted_preflight()
        # The trusted preflight shells out to git itself; keep it off any
        # system/user git config, as every other git call here is.
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"

        private_home = run_dir / "pf-home"
        private_cache = run_dir / "pf-cache"
        _fresh_dir(private_home, mode=0o700)
        _fresh_dir(private_cache, mode=0o700)

        # Mark OURSELVES as a reaper before the trusted preflight ever runs
        # a tool subprocess, so a hung/runaway one (and anything IT forks)
        # is reachable by `_reap_all_descendants` regardless of how deep or
        # how fast it tries to detach.
        _prctl(PR_SET_CHILD_SUBREAPER, 1)
        done = threading.Event()
        timed_out = threading.Event()
        watchdog = threading.Thread(
            target=_preflight_watchdog,
            args=(done, timed_out, PREFLIGHT_DEADLINE_S),
            daemon=True,
        )
        watchdog.start()
        failure_text: str | None = None
        try:
            with _preflight_sandbox_environment(private_home, private_cache):
                try:
                    preflight_result = preflight_module.run(repo, pf_dir, tools=tools)
                except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                    preflight_result = None
                    # A trusted-preflight failure is not a child refusal:
                    # report the exact legacy failure text the caller
                    # forwards to the owner, alongside a generic `reason`
                    # for anything reading only that -- unless the watchdog
                    # is the one that actually killed the tool, in which
                    # case that (not whatever ordinary-looking failure text
                    # a SIGTERM'd tool produces) is the real story.
                    if timed_out.is_set():
                        failure_text = "trusted preflight timed out"
                    else:
                        failure_text = preflight_module.failure_reason(exc)
        finally:
            done.set()
            watchdog.join(timeout=2)
            # Always: a tool that forked something it never waited for must
            # not survive this call regardless of how it ended.
            _reap_all_descendants()

        # Fail closed: if the watchdog fired, a killed tool may still look
        # like a success to `subprocess.run` (the watchdog can reap it first,
        # which Popen reports as exit 0), so never trust `run()`'s result.
        if timed_out.is_set():
            failure_text = "trusted preflight timed out"

        if failure_text is not None:
            print(
                json.dumps(
                    {"reason": "trusted preflight failed", "preflight_failure": failure_text}
                ),
                flush=True,
            )
            return 3

        patch_bytes_out, changed_paths = _package_worktree(
            pf_dir, out_dir=run_dir / "out", use_existing_index=True
        )
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"preflight failed: {exc}")
        return 3
    finally:
        if pf_dir is not None:
            shutil.rmtree(pf_dir, ignore_errors=True)
        if run_dir is not None:
            for extra in (run_dir / "pf-home", run_dir / "pf-cache"):
                shutil.rmtree(extra, ignore_errors=True)
    print(
        json.dumps(
            {
                "ok": True,
                "patch_b64": (
                    base64.b64encode(patch_bytes_out).decode("ascii")
                    if patch_bytes_out
                    else ""
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

    # Mark OURSELVES (codex_child.py's own process, not codex) as a reaper
    # for our descendants BEFORE codex ever starts: a double-forked/setsid
    # grandchild that outlives its immediate parent then re-parents to US
    # (Linux only) instead of init, so `_reap_all_descendants` below can
    # actually find and kill it regardless of how deeply -- or how soon
    # after spawn -- it tried to detach itself. Setting this only after
    # `Popen` would leave exactly that window open.
    _prctl(PR_SET_CHILD_SUBREAPER, 1)

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

    signaled = threading.Event()

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        signaled.set()

    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
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

        outcome = _stream_and_wait(process, params.timeout_s, params.idle_timeout_s, signaled)

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
        # returns, so nothing sandboxed can survive us. SIGTERM stays
        # HANDLED (never restored to the default "terminate immediately")
        # until every descendant is confirmed gone: a second SIGTERM
        # arriving mid-reap (e.g. a slow service shutdown) must not kill US
        # before `_reap_all_descendants` finishes, which would leave
        # sandboxed processes orphaned -- the exact leak this whole
        # subreaper mechanism exists to avoid.
        try:
            _reap_all_descendants()
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


# --------------------------------------------------------------------------
# discussion: a single read-only `/suhbat` app-server turn (chat lane only).
#
# Ports `ops/discussion_appserver.py run_turn`'s JSON-RPC exchange and
# ketoshop diagnostics MCP wiring exactly, but as a codex_child subcommand:
# `agent_svc.codex.CodexRunner.run_discussion` speaks the same single
# JSON-request/JSON-reply protocol as `prepare`/`package`/`preflight` (never
# `exec`'s line-streamed JSONL) -- only the final `{"ok", "thread_id",
# "response"}` frame reaches OUR stdout; every app-server JSON-RPC message
# and its own stderr are consumed here and never forwarded. Runs with the
# same descendant reaping/timeout discipline as `cmd_exec`: PDEATHSIG on the
# spawned app-server, a child-subreaper on this process, `kill_tree` on
# SIGTERM/timeout, and `_reap_all_descendants` on every exit path.
# --------------------------------------------------------------------------

DISCUSSION_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_DISCUSSION_PROMPT_CHARS = 10_000
MAX_DISCUSSION_RESPONSE_CHARS = 4_000
DISCUSSION_RPC_LINE_BYTES = 1024 * 1024

# The diagnostics proxy path and socket stay exactly as today (owned by
# `ops/task-manager-diagnostics.service`, unchanged by this move). What
# changes is only how THIS process reaches that socket: agent-codex is never
# a member of the socket's owning group, and this subcommand's own sudoers
# rule grants it that group (`-g`, via `agent_svc.codex.CodexRunner.
# run_discussion`) for the duration of this one call only -- see
# ops/agent-svc.sudoers and ops/install_agent_svc.sh. Overridable in test
# mode, like every other path constant above.
DIAGNOSTIC_PROXY = _env_str("DIAGNOSTIC_PROXY", "/opt/task-manager/ops/diagnostic_proxy.py")
DIAGNOSTIC_SOCKET = _env_str(
    "DIAGNOSTIC_SOCKET", "/run/task-manager-diagnostics/diagnostics.sock"
)


class _DiscussionRefusal(ChildRefusal):
    """A validated app-server protocol failure.

    Carries the same Uzbek text `ops/discussion_appserver.py` raised, for
    the redacted stderr/`reason` detail only -- `agent_svc.discussion`
    always reports one fixed generic user-facing error to the backend
    regardless of this exact reason (see that module's `GENERIC_ERROR`).
    """


@dataclass(frozen=True)
class DiscussionParams:
    run_dir: Path
    cwd_path: Path
    codex_home: Path
    prompt: str
    images: list[Path]
    thread_id: str | None
    model: str
    effort: str
    diagnostics_discussion_id: int | None
    diagnostics_lease_id: str | None
    timeout_s: float


def _validate_discussion_request(request: dict[str, Any]) -> DiscussionParams:
    run_id = _validate_run_id(request.get("run_id"))
    run_dir = _run_dir(run_id)
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not (1 <= len(prompt) <= MAX_DISCUSSION_PROMPT_CHARS):
        raise ChildRefusal("invalid prompt")
    raw_images = request.get("images") or []
    if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGES:
        raise ChildRefusal("invalid images")
    images_root = run_dir / "images"
    images: list[Path] = []
    for raw in raw_images:
        if not isinstance(raw, str):
            raise ChildRefusal("invalid image path")
        images.append(_ensure_within(Path(raw), images_root))
    thread_id = request.get("thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not DISCUSSION_THREAD_ID_RE.fullmatch(thread_id)
    ):
        raise ChildRefusal("invalid thread_id")
    model = request.get("model")
    if model not in MODELS:
        raise ChildRefusal("invalid model")
    effort = request.get("effort")
    if effort not in EFFORTS:
        raise ChildRefusal("invalid effort")
    diagnostics = request.get("diagnostics")
    diagnostics_discussion_id: int | None = None
    diagnostics_lease_id: str | None = None
    if diagnostics is not None:
        if not isinstance(diagnostics, dict):
            raise ChildRefusal("invalid diagnostics")
        discussion_id = diagnostics.get("discussion_id")
        lease_id = diagnostics.get("lease_id")
        if (
            isinstance(discussion_id, bool)
            or not isinstance(discussion_id, int)
            or discussion_id < 1
        ):
            raise ChildRefusal("invalid diagnostics discussion_id")
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 100:
            raise ChildRefusal("invalid diagnostics lease_id")
        diagnostics_discussion_id = discussion_id
        diagnostics_lease_id = lease_id
    timeout_s = _positive_number(request.get("timeout_s"), maximum=MAX_TIMEOUT_S)
    cwd_path = run_dir / "wt"
    if not cwd_path.is_dir():
        raise ChildRefusal("worktree is not prepared")
    # Re-scan for blocked paths every call, like `exec` does for "wt": a
    # discussion thread can be resumed across multiple turns against the
    # SAME prepared snapshot.
    _refuse_if_agent_config_present(cwd_path)
    return DiscussionParams(
        run_dir=run_dir,
        cwd_path=cwd_path,
        codex_home=Path(CODEX_HOMES["chat"]),
        prompt=prompt,
        images=images,
        thread_id=thread_id,
        model=model,
        effort=effort,
        diagnostics_discussion_id=diagnostics_discussion_id,
        diagnostics_lease_id=diagnostics_lease_id,
        timeout_s=timeout_s,
    )


def _app_server_argv(
    diagnostics_discussion_id: int | None, diagnostics_lease_id: str | None
) -> list[str]:
    command = [CODEX_BINARY, "app-server", "--stdio"]
    if diagnostics_discussion_id is not None:
        if not diagnostics_lease_id:
            # `_validate_discussion_request` already guarantees this pairing
            # before ever calling this function; this is a second,
            # defense-in-depth check against a future internal caller that
            # skips that validation.
            raise ChildRefusal("diagnostic lease capability is required")
        proxy_args = json.dumps(
            [
                DIAGNOSTIC_PROXY,
                "--discussion-id",
                str(diagnostics_discussion_id),
                "--lease-id",
                diagnostics_lease_id,
                "--socket",
                DIAGNOSTIC_SOCKET,
            ],
            separators=(",", ":"),
        )
        command.extend(
            [
                "--config",
                'mcp_servers.ketoshop_diagnostics.command="/usr/bin/python3"',
                "--config",
                f"mcp_servers.ketoshop_diagnostics.args={proxy_args}",
            ]
        )
    return command


def _discussion_env(run_dir: Path, codex_home: Path) -> dict[str, str]:
    return {
        "HOME": HOME_DIR,
        "CODEX_HOME": str(codex_home),
        "PATH": PATH_VALUE,
        # `prepare` already created this (alongside "wt"/"images"), which
        # `_validate_discussion_request` requires anyway -- never a fresh
        # mkdir here, so the app-server can never be handed a tmp dir it
        # doesn't already own.
        "TMPDIR": str(run_dir / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _thread_start_params(params: DiscussionParams) -> dict[str, Any]:
    return {
        "model": params.model,
        "cwd": str(params.cwd_path),
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "serviceName": "task_manager_discussion",
    }


def _run_app_server_turn(
    params: DiscussionParams, signaled: threading.Event
) -> tuple[str, str]:
    try:
        process = subprocess.Popen(
            _app_server_argv(params.diagnostics_discussion_id, params.diagnostics_lease_id),
            cwd=str(params.cwd_path),
            env=_discussion_env(params.run_dir, params.codex_home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_preexec_pdeathsig,
        )
    except OSError as exc:
        raise ChildRefusal(f"failed to start codex app-server: {exc}") from exc
    assert (
        process.stdin is not None and process.stdout is not None and process.stderr is not None
    )
    stdin, stdout = process.stdin, process.stdout

    messages: queue.Queue[Any] = queue.Queue()
    stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)

    def _pump_messages() -> None:
        try:
            while True:
                raw_line = stdout.readline(DISCUSSION_RPC_LINE_BYTES)
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    messages.put(json.loads(text))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=_pump_messages, daemon=True)
    stderr_reader = threading.Thread(
        target=_pump_stderr, args=(process.stderr, stderr_tail), daemon=True
    )
    reader.start()
    stderr_reader.start()

    deadline = time.monotonic() + params.timeout_s
    next_id = [0]

    def new_id() -> int:
        next_id[0] += 1
        return next_id[0]

    def send(method: str, request_id: int | None, rpc_params: dict[str, Any]) -> None:
        value: dict[str, Any] = {"method": method, "params": rpc_params}
        if request_id is not None:
            value["id"] = request_id
        try:
            stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _DiscussionRefusal(f"Codex suhbat aloqasi uzildi: {exc}") from exc

    def read() -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _DiscussionRefusal("Codex javobi vaqtida kelmadi")
            if signaled.is_set():
                raise _DiscussionRefusal("cancelled")
            try:
                message = messages.get(timeout=min(max(remaining, 0.05), 0.5))
            except queue.Empty:
                continue
            if message is None or not isinstance(message, dict):
                raise _DiscussionRefusal("Codex suhbat aloqasi uzildi")
            return message

    def await_reply(request_id: int) -> dict[str, Any]:
        """The raw JSON-RPC reply for `request_id` -- which may itself carry
        an "error" field; callers decide whether that is fatal. Only an
        INCOMING request (an approval ask) always raises here: that is never
        acceptable regardless of which call is outstanding."""
        while True:
            message = read()
            if "id" in message and "method" in message:
                # Never grant tool or network approvals to an automated chat.
                raise _DiscussionRefusal("Codex qo‘shimcha ruxsat so‘radi")
            if message.get("id") == request_id:
                return message

    def response(request_id: int) -> dict[str, Any]:
        reply = await_reply(request_id)
        if "error" in reply:
            raise _DiscussionRefusal("Codex suhbatni boshlay olmadi")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise _DiscussionRefusal("Codex noto‘g‘ri javob berdi")
        return result

    def extract_thread_id(reply: dict[str, Any]) -> str | None:
        if "error" in reply:
            return None
        result = reply.get("result")
        if not isinstance(result, dict):
            return None
        thread = result.get("thread")
        if not isinstance(thread, dict):
            return None
        candidate = thread.get("id")
        if not isinstance(candidate, str) or not DISCUSSION_THREAD_ID_RE.fullmatch(candidate):
            return None
        return candidate

    def open_thread() -> str:
        if params.thread_id:
            resume_id = new_id()
            send(
                "thread/resume",
                resume_id,
                {"threadId": params.thread_id, "cwd": str(params.cwd_path)},
            )
            resumed = extract_thread_id(await_reply(resume_id))
            if resumed is not None:
                return resumed
            # The legacy chat worker's thread ids live under a different
            # CODEX_HOME (the old codex-runner ~/.codex, not this lane's own
            # .codex-chat) -- resume can fail forever for any thread that
            # predates the chat-lane cutover. Fall back to a brand new
            # thread instead of failing the whole turn; the caller persists
            # whatever id THIS call returns, so the next turn resumes the
            # new one.
        start_id = new_id()
        send("thread/start", start_id, _thread_start_params(params))
        started = extract_thread_id(await_reply(start_id))
        if started is None:
            raise _DiscussionRefusal("Codex suhbatni saqlay olmadi")
        return started

    refused: ChildRefusal | None = None
    try:
        init_id = new_id()
        send(
            "initialize",
            init_id,
            {
                "clientInfo": {
                    "name": "task_manager_discussion",
                    "title": "Task Manager discussion",
                    "version": "1.0.0",
                }
            },
        )
        response(init_id)
        send("initialized", None, {})

        saved_thread_id = open_thread()
        inputs: list[dict[str, str]] = [{"type": "text", "text": params.prompt}]
        inputs.extend({"type": "localImage", "path": str(image)} for image in params.images)
        turn_id = new_id()
        send(
            "turn/start",
            turn_id,
            {
                "threadId": saved_thread_id,
                "input": inputs,
                "cwd": str(params.cwd_path),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "model": params.model,
                "effort": params.effort,
                "summary": "concise",
            },
        )
        response(turn_id)
        answer = ""
        while True:
            message = read()
            if message.get("method") == "item/completed":
                item = (message.get("params") or {}).get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") in (
                    None,
                    "final_answer",
                ):
                    answer = str(item.get("text") or "")
            elif message.get("method") == "turn/completed":
                turn = (message.get("params") or {}).get("turn") or {}
                if turn.get("status") != "completed" or not answer.strip():
                    raise _DiscussionRefusal("Codex suhbat javobini tugata olmadi")
                return saved_thread_id, answer.strip()[:MAX_DISCUSSION_RESPONSE_CHARS]
            elif "id" in message and "method" in message:
                raise _DiscussionRefusal("Codex qo‘shimcha ruxsat so‘radi")
    except ChildRefusal as exc:
        # Only recorded here (not yet read); attached to the exception down
        # in `finally`, AFTER `stderr_reader.join()` -- reading `stderr_tail`
        # this early would race that thread still draining the app-server's
        # pipe.
        refused = exc
        raise
    finally:
        kill_tree(process.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        if refused is not None:
            # Carried only for the redacted `stderr_tail` in the emitted
            # refusal frame (see `cmd_discussion`) -- never used to change
            # what was raised or how. Safe to mutate here: `finally` always
            # completes before this same exception object reaches the
            # caller.
            refused.stderr_tail = list(stderr_tail)[-MAX_STDERR_LINES:]  # type: ignore[attr-defined]


def cmd_discussion(request: dict[str, Any]) -> int:
    try:
        params = _validate_discussion_request(request)
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"discussion setup failed: {exc}")
        return 3

    signaled = threading.Event()

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        signaled.set()

    previous_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    # Mark ourselves as a reaper BEFORE the app-server ever starts, so a
    # double-forked/setsid grandchild (a sandboxed tool call) is reachable by
    # `_reap_all_descendants` regardless of how deep or how soon it detaches.
    _prctl(PR_SET_CHILD_SUBREAPER, 1)
    try:
        thread_id, answer = _run_app_server_turn(params, signaled)
    except ChildRefusal as exc:
        # `stderr_tail` (see `_run_app_server_turn`'s own `except ChildRefusal`)
        # is the app-server's raw, UNREDACTED stderr: it goes into this one
        # JSON frame on OUR stdout, never printed anywhere by this process,
        # and the parent (`agent_svc.discussion`) redacts it before logging.
        stderr_tail = getattr(exc, "stderr_tail", None)
        body: dict[str, Any] = {"reason": str(exc)}
        if stderr_tail:
            body["stderr_tail"] = stderr_tail
        print(json.dumps(body, ensure_ascii=False), flush=True)
        return 3
    except OSError as exc:
        _emit_error(f"discussion failed: {exc}")
        return 3
    finally:
        try:
            _reap_all_descendants()
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
    print(
        json.dumps(
            {"ok": True, "thread_id": thread_id, "response": answer}, ensure_ascii=False
        ),
        flush=True,
    )
    return 0


# --------------------------------------------------------------------------
# check: the factory Check Gate (agent_svc/implement.py `_check_once`). Runs
# the locked Acceptance Checks of ONE task against the builder's patch, as
# agent-codex, inside `codex sandbox` (no network, writes only inside the
# fresh `ck/` clone). This executes repository code, so:
#   * the command is never taken from the request -- only a profile NAME,
#     looked up in the allowlist below together with the repository;
#   * a trusted canary runs under the very same sandbox argv first and must
#     prove that networking and writes outside `ck/` are denied, otherwise
#     the gate reports `unavailable` and nothing from the repository runs;
#   * the test runner is trusted inline code (`python3 -I -B -c`), it only
#     imports the one pinned test module and reports a JSON result line
#     tagged with a nonce it reads (and deletes) before importing anything
#     from the repository.
# Residual risk (accepted, see docs/adr/0001): repository code runs in the
# same interpreter as the runner, so a deliberately hostile patch could forge
# a result; CI re-runs the same checks independently as regression. As with
# `exec` (workspace-write), sandboxed code can READ what agent-codex can read,
# and `output_tail` (stderr the tests control) carries text back to agent-svc,
# so `_redact` is best effort, not a boundary. The sandbox does not scope
# signals or /proc between same-uid processes, there is no per-check cgroup
# (pids/memory/disk), and a process that detaches AND kills this child can
# outlive the check; per-check uids or cgroups are milestone-2 work.
# --------------------------------------------------------------------------

CHECK_PROFILES: dict[str, dict[str, Any]] = {
    # MUST match agent_svc/acceptance.py `PROFILES`.
    "unittest": {"repos": frozenset({"Asadtop4ik/agent-qa"}), "dir": "tests/acceptance"},
}
CHECK_MIN_TIMEOUT_S = 5.0
CHECK_MAX_TIMEOUT_S = 300.0
CHECK_MAX_EXPECTED_TESTS = 200
CHECK_MAX_TASK_ID = 10**12
CHECK_OUTPUT_CAP_BYTES = 256 * 1024
CHECK_OUTPUT_TAIL_CHARS = 4000
CHECK_DETAIL_CHARS = 300
CHECK_MAX_FAILED_IDS = 50
CHECK_MAX_TEST_ID_CHARS = 200
CHECK_FSIZE_BYTES = 64 * 1024 * 1024
CHECK_CANARY_TIMEOUT_S = 15.0
# Work (clone, canary <= 15s, tests) may use `timeout_s` plus this much, so
# the tests themselves still get their full `timeout_s`; cleanup is
# SIGKILL-only and bounded, so the reply still lands well inside the parent's
# own `timeout_s + 60s` before it SIGTERMs us.
CHECK_OVERHEAD_S = 35.0
CHECK_GIT_TIMEOUT_S = 60.0
# Per-uid (all agent-codex tasks, threads included): only a fork-bomb cap.
CHECK_RUNNER_NPROC = 4096
CHECK_RUNNER_AS_BYTES = 4 * 1024 * 1024 * 1024
CHECK_RESULT_PREFIX = "AGENT_SVC_CHECK_RESULT:"
CANARY_RESULT_PREFIX = "AGENT_SVC_CANARY:"
CANARY_MARKER = "agent-svc-canary"
CHECK_RUNNER_MARKER = "agent-svc-check"
CHECK_TMP_NAME = ".agent-svc-tmp"
CHECK_NONCE_DIR_NAME = ".agent-svc-check"
CHECK_PYTHON = _env_str("CHECK_PYTHON", "/usr/bin/python3")
# Test mode only: a stand-in for `codex sandbox`, invoked as
# `<wrapper> <ck> -- <command...>`. Always `None` in production.
CHECK_SANDBOX_WRAPPER = _test_override("CHECK_SANDBOX")

_CHECK_REDACTIONS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b(\s*[=:]\s*)\S+"),
)

_CANARY_SRC = r"""
import errno, json, os, socket, sys

BLOCKED = (errno.EPERM, errno.EACCES)


def net_blocked(family, kind, address, also=()):
    # Loopback and UDP probes keep a host firewall (which can also answer
    # EPERM, for public TCP) from passing for the sandbox.
    try:
        sock = socket.socket(family, kind)
    except OSError as exc:
        return exc.errno in BLOCKED + also
    try:
        sock.settimeout(3)
        if kind == socket.SOCK_DGRAM:
            sock.sendto(b"x", address)
        else:
            sock.connect(address)
    except OSError as exc:
        return exc.errno in BLOCKED + also
    finally:
        sock.close()
    return False


def try_create(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return exc.errno
    os.close(fd)
    return 0


nonce, outside, tmp, inside = sys.argv[2:6]
inside_errno = try_create(inside)
if inside_errno == 0:
    os.unlink(inside)
no_v6 = (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL)
result = {
    "net_public": net_blocked(socket.AF_INET, socket.SOCK_STREAM, ("1.1.1.1", 443)),
    "net_loopback": net_blocked(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 9)),
    "net_loopback_udp": net_blocked(socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 9)),
    "net_loopback6": net_blocked(socket.AF_INET6, socket.SOCK_STREAM, ("::1", 9), no_v6),
    "write_outside": try_create(outside) in BLOCKED + (errno.EROFS,),
    "write_tmp": try_create(tmp) in BLOCKED + (errno.EROFS,),
    "write_inside": inside_errno == 0,
}
print("AGENT_SVC_CANARY:" + nonce + ":" + json.dumps(result), flush=True)
"""
CANARY_CHECKS = (
    "net_public",
    "net_loopback",
    "net_loopback_udp",
    "net_loopback6",
    "write_outside",
    "write_tmp",
    "write_inside",
)

_RUNNER_SRC = r"""
import importlib, json, os, resource, sys, unittest


def limit(which, value):
    try:
        _soft, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass


def main():
    nonce_path, module_name, expected_file, nproc, address_space = sys.argv[2:7]
    # Python-only caps (the node launcher of `codex sandbox` cannot take
    # them): fork bombs and runaway memory in the repository's tests.
    limit(resource.RLIMIT_NPROC, int(nproc))
    limit(resource.RLIMIT_AS, int(address_space))
    with open(nonce_path, encoding="ascii") as handle:
        nonce = handle.read().strip()
    os.unlink(nonce_path)
    os.rmdir(os.path.dirname(nonce_path))
    # The result goes to a private copy of stdout; everything the repository
    # prints (tests, imports) goes to stderr.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def emit(payload):
        out.write("\nAGENT_SVC_CHECK_RESULT:" + nonce + ":" + json.dumps(payload) + "\n")
        out.flush()

    sys.path.insert(0, os.getcwd())
    try:
        module = importlib.import_module(module_name)
    except BaseException as exc:
        emit({"status": "error", "detail": "import failed: " + type(exc).__name__})
        raise
    loaded = os.path.realpath(getattr(module, "__file__", "") or "")
    if loaded != os.path.realpath(expected_file):
        emit({"status": "error", "detail": "acceptance module resolved elsewhere"})
        return
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    bad = []
    for group in (
        result.failures,
        result.errors,
        result.skipped,
        result.expectedFailures,
        [(test, None) for test in result.unexpectedSuccesses],
    ):
        for test, _ in group:
            test_id = getattr(test, "test_case", test).id()
            if test_id not in bad:
                bad.append(test_id)
    total = result.testsRun
    emit(
        {
            "status": "failed" if bad else "passed",
            "total": total,
            "passed": max(total - len(bad), 0),
            "failed": bad,
        }
    )


main()
"""


class _CheckTerminated(Exception):
    """SIGTERM from the parent (timeout or cancel): unwind through cleanup."""


@dataclass(frozen=True)
class CheckParams:
    run_id: str
    run_dir: Path
    repo: str
    mirror: Path
    base_sha: str
    patch: bytes
    pinned: bytes
    acceptance_dir: str
    task_id: int
    expected_tests: int
    timeout_s: float

    @property
    def task_file(self) -> str:
        return f"{self.acceptance_dir}/test_task_{self.task_id}.py"

    @property
    def module_name(self) -> str:
        return f"{self.acceptance_dir.replace('/', '.')}.test_task_{self.task_id}"


def _decode_patch(value: Any, *, name: str, allow_empty: bool) -> bytes:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ChildRefusal(f"invalid {name}")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ChildRefusal(f"invalid {name} encoding") from exc
    if not data and not allow_empty:
        raise ChildRefusal(f"empty {name}")
    if len(data) > MAX_PATCH_BYTES:
        raise ChildRefusal(f"{name} exceeds the size limit")
    bad_mode_path = _scan_patch_for_bad_modes(data.decode("utf-8", "replace"))
    if bad_mode_path is not None:
        raise ChildRefusal(f"unsupported change (symlink or submodule): {bad_mode_path}")
    return data


def _bounded_int(value: Any, *, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ChildRefusal(f"invalid {name}")
    return value


def _validate_check_request(request: dict[str, Any]) -> CheckParams:
    run_id = _validate_run_id(request.get("run_id"))
    run_dir = _run_dir(run_id)
    repo = request.get("repo")
    mirror = _validate_mirror(request.get("mirror"), repo)
    assert isinstance(repo, str)
    base_sha = request.get("base_sha")
    if not isinstance(base_sha, str) or not SHA_RE.fullmatch(base_sha):
        raise ChildRefusal("invalid base_sha")
    profile_name = request.get("profile")
    profile = CHECK_PROFILES.get(profile_name) if isinstance(profile_name, str) else None
    if profile is None or repo not in profile["repos"]:
        raise ChildRefusal("no check profile for this repository")
    timeout = request.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ChildRefusal("invalid timeout")
    timeout_s = float(timeout)
    if not CHECK_MIN_TIMEOUT_S <= timeout_s <= CHECK_MAX_TIMEOUT_S:
        raise ChildRefusal("invalid timeout")
    return CheckParams(
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        mirror=mirror,
        base_sha=base_sha,
        patch=_decode_patch(request.get("patch_b64"), name="patch_b64", allow_empty=True),
        pinned=_decode_patch(request.get("pinned_b64"), name="pinned_b64", allow_empty=False),
        acceptance_dir=profile["dir"],
        task_id=_bounded_int(
            request.get("task_id"), name="task_id", low=1, high=CHECK_MAX_TASK_ID
        ),
        expected_tests=_bounded_int(
            request.get("expected_tests"),
            name="expected_tests",
            low=1,
            high=CHECK_MAX_EXPECTED_TESTS,
        ),
        timeout_s=timeout_s,
    )


def _pinned_paths(
    params: CheckParams, ck: Path, env: dict[str, str], *, timeout: float
) -> list[str]:
    """Every path the pinned patch touches, as git itself parses it; refuse
    anything outside the acceptance directory, renames/copies, and a pinned
    patch that does not carry this task's own test file."""
    for line in params.pinned.split(b"\n"):
        if line.startswith((b"rename from ", b"rename to ", b"copy from ", b"copy to ")):
            raise ChildRefusal("pinned patch must not rename or copy files")
    numstat = _run_git(
        _git_argv("apply", "--numstat", "-z"),
        cwd=ck,
        env=env,
        input_bytes=params.pinned,
        timeout=timeout,
    )
    paths: list[str] = []
    for field in numstat.stdout.split(b"\0"):
        if not field:
            continue
        parts = field.split(b"\t")
        if len(parts) != 3 or not parts[2]:
            raise ChildRefusal("pinned patch must not rename or copy files")
        try:
            paths.append(parts[2].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ChildRefusal("pinned patch has an undecodable path") from exc
    prefix = params.acceptance_dir + "/"
    for path in paths:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or not path.startswith(prefix):
            raise ChildRefusal("pinned patch touches a path outside the acceptance directory")
    if params.task_file not in paths:
        raise ChildRefusal("pinned patch does not add this task's acceptance file")
    return paths


def _redact(text: str) -> str:
    for pattern in _CHECK_REDACTIONS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def _check_sandbox_argv(ck: Path, command: list[str]) -> list[str]:
    if CHECK_SANDBOX_WRAPPER is not None:
        return [CHECK_SANDBOX_WRAPPER, str(ck), "--", *command]
    # No `-C`: in codex-cli 0.159 it requires `--permission-profile`; the
    # workspace root is the process cwd (`ck`), set by `_run_sandboxed`.
    return [
        CODEX_BINARY,
        "sandbox",
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "-c",
        "sandbox_workspace_write.writable_roots=[]",
        "--",
        *command,
    ]


def _check_env(run_dir: Path, ck: Path) -> dict[str, str]:
    home = run_dir / "ck-home"
    return {
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "PATH": PATH_VALUE,
        "TMPDIR": str(ck / CHECK_TMP_NAME),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


def _check_preexec(cpu_s: int) -> Callable[[], None]:
    def _apply() -> None:
        _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        # No RLIMIT_AS (the node launcher of `codex` reserves huge virtual
        # ranges) and no RLIMIT_NPROC (per-uid, would hit other runs).
        for limit, value in (
            (resource.RLIMIT_CPU, cpu_s),
            (resource.RLIMIT_FSIZE, CHECK_FSIZE_BYTES),
            (resource.RLIMIT_CORE, 0),
        ):
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(limit, (value, value))

    return _apply


def _drain_tail(stream: Any, sink: list[bytes]) -> None:
    buffer = bytearray()
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > CHECK_OUTPUT_CAP_BYTES:
            del buffer[: len(buffer) - CHECK_OUTPUT_CAP_BYTES]
    sink.append(bytes(buffer))


def _run_sandboxed(
    command: list[str], *, ck: Path, env: dict[str, str], timeout_s: float
) -> tuple[int | None, bytes, bytes]:
    """Run `command` under the check sandbox. Returns (returncode, or None on
    timeout; stdout tail; stderr tail) and always leaves no descendant alive."""
    process = subprocess.Popen(
        _check_sandbox_argv(ck, command),
        cwd=str(ck),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        preexec_fn=_check_preexec(int(timeout_s) + 5),
    )
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    readers = [
        threading.Thread(target=_drain_tail, args=(process.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain_tail, args=(process.stderr, err_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()
    returncode: int | None = None
    try:
        returncode = process.wait(timeout=max(timeout_s, 0.1))
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Straight SIGKILL: nothing sandboxed needs a graceful exit, and the
        # cleanup must fit the time budget (see CHECK_OVERHEAD_S).
        if process.poll() is None:
            kill_tree(process.pid, grace_s=0)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        # Anything that detached from the sandboxed tree (and so may hold the
        # pipes open) dies here: we are a subreaper, so it is ours to find.
        _reap_all_descendants(grace_s=0.5)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
    return returncode, b"".join(out_sink), b"".join(err_sink)


def _single_tagged_json(stdout: bytes, prefix: str) -> dict[str, Any] | None:
    """The JSON object on the ONE stdout line tagged `prefix` (which carries
    a per-call nonce); `None` if there is no such line or more than one --
    a second writer means someone else had the descriptor or the nonce."""
    tagged = [
        line
        for line in stdout.decode("utf-8", "replace").splitlines()
        if line.startswith(prefix)
    ]
    if len(tagged) != 1:
        return None
    try:
        body = json.loads(tagged[0][len(prefix) :])
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _canary_failure(
    params: CheckParams, ck: Path, env: dict[str, str], deadline: float
) -> str | None:
    """Prove the sandbox actually confines before any repository code runs.
    Returns `None` when it does, else why the gate is unavailable."""
    suffix = secrets.token_hex(8)
    nonce = secrets.token_hex(16)
    outside = params.run_dir / f"ck-canary-outside-{suffix}"
    tmp_probe = Path("/tmp") / f"agent-svc-check-canary-{params.run_id}-{suffix}"
    inside = ck / CHECK_TMP_NAME / f"canary-{suffix}"
    timeout_s = min(CHECK_CANARY_TIMEOUT_S, deadline - time.monotonic())
    if timeout_s <= 0:
        return "no time left for the sandbox canary"
    try:
        returncode, stdout, _stderr = _run_sandboxed(
            [
                CHECK_PYTHON,
                "-I",
                "-B",
                "-c",
                _CANARY_SRC,
                CANARY_MARKER,
                nonce,
                str(outside),
                str(tmp_probe),
                str(inside),
            ],
            ck=ck,
            env=env,
            timeout_s=timeout_s,
        )
    except OSError as exc:
        return f"sandbox could not start: {type(exc).__name__}"
    leaked = False
    for probe in (outside, tmp_probe):
        if probe.exists() or probe.is_symlink():
            leaked = True
            with contextlib.suppress(OSError):
                probe.unlink()
    if returncode != 0:
        return f"sandbox canary exited with {returncode}"
    body = _single_tagged_json(stdout, f"{CANARY_RESULT_PREFIX}{nonce}:")
    if body is None or leaked or any(body.get(key) is not True for key in CANARY_CHECKS):
        return "sandbox canary: confinement not proven"
    return None


def _refuse_symlinked_components(root: Path, relative: str) -> None:
    """Every existing component of `root/relative` must be a real directory
    or file, never a symlink: the unconfined steps below delete there."""
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ChildRefusal(f"refuses a symlink at {current.relative_to(root)}")
        if not current.exists():
            return
    _ensure_within(current, root)


def _prepare_check_tree(
    params: CheckParams, ck: Path, env: dict[str, str], deadline: float
) -> None:
    """Fresh clone at base + builder patch, with the acceptance directory
    reset to base and the pinned checks applied on top. Runs unconfined (as
    agent-codex, no repository code executes), so every step refuses
    symlinks, submodules and agent config the patches could introduce."""
    acceptance = params.acceptance_dir

    def remaining() -> float:
        left = min(CHECK_GIT_TIMEOUT_S, deadline - time.monotonic())
        if left <= 0:
            raise ChildRefusal("no time left to assemble the checks")
        return left

    def git(*args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return _run_git(
            _git_argv("-C", str(ck), *args),
            cwd=params.run_dir,
            env=env,
            input_bytes=input_bytes,
            timeout=remaining(),
        )

    _clone_mirror(params.mirror, ck, run_dir=params.run_dir, env=env, timeout=remaining())
    git("checkout", "--detach", params.base_sha)
    _refuse_if_agent_config_present(ck)
    _pinned_paths(params, ck, env, timeout=remaining())
    if params.patch:
        git("apply", "--index", input_bytes=params.patch)
        _refuse_bad_staged_modes(ck, cwd=params.run_dir, env=env)
        _refuse_if_agent_config_present(ck)
    _refuse_symlinked_components(ck, acceptance)
    git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", acceptance)
    target = ck / acceptance
    if target.exists() and not target.is_dir():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    if git("ls-tree", "--name-only", params.base_sha, "--", acceptance).stdout.strip():
        git("checkout", params.base_sha, "--", acceptance)
    git("apply", "--index", input_bytes=params.pinned)
    _refuse_bad_staged_modes(ck, cwd=params.run_dir, env=env)
    _refuse_if_agent_config_present(ck)
    for relative in (f"{acceptance}/__init__.py", params.task_file):
        _refuse_symlinked_components(ck, relative)
        if not (ck / relative).is_file():
            raise ChildRefusal(f"missing {relative} after applying the checks")


def _check_reply(
    status: str,
    *,
    total: int = 0,
    passed: int = 0,
    failed: list[Any] | None = None,
    output: str = "",
    detail: str = "",
) -> dict[str, Any]:
    clean_failed: list[str] = []
    for item in failed or []:
        if not isinstance(item, str):
            continue
        text = "".join(ch for ch in item if ch.isprintable())[:CHECK_MAX_TEST_ID_CHARS]
        if text and text not in clean_failed:
            clean_failed.append(text)
    return {
        "ok": True,
        "status": status,
        "total": total,
        "passed": passed,
        "failed": clean_failed[: min(CHECK_MAX_FAILED_IDS, total)],
        "output_tail": _redact(output)[-CHECK_OUTPUT_TAIL_CHARS:],
        "detail": _redact(detail)[:CHECK_DETAIL_CHARS],
    }


def _run_checks(
    params: CheckParams, ck: Path, env: dict[str, str], deadline: float
) -> dict[str, Any]:
    nonce = secrets.token_hex(16)
    nonce_dir = ck / CHECK_NONCE_DIR_NAME
    _fresh_dir(nonce_dir, mode=0o700)
    nonce_path = nonce_dir / "nonce"
    nonce_path.write_text(nonce, encoding="ascii")
    timeout_s = min(params.timeout_s, deadline - time.monotonic())
    if timeout_s <= 0:
        return _check_reply("error", detail="no time left to run the checks")
    returncode, stdout, stderr = _run_sandboxed(
        [
            CHECK_PYTHON,
            "-I",
            "-B",
            "-c",
            _RUNNER_SRC,
            CHECK_RUNNER_MARKER,
            str(nonce_path),
            params.module_name,
            str(ck / params.task_file),
            str(CHECK_RUNNER_NPROC),
            str(CHECK_RUNNER_AS_BYTES),
        ],
        ck=ck,
        env=env,
        timeout_s=timeout_s,
    )
    output = stderr.decode("utf-8", "replace")
    if returncode is None:
        return _check_reply("error", output=output, detail="acceptance checks timed out")
    body = _single_tagged_json(stdout, f"{CHECK_RESULT_PREFIX}{nonce}:")
    if body is None:
        return _check_reply("error", output=output, detail=f"no check result (exit {returncode})")
    status = body.get("status")
    if status != "error" and returncode != 0:
        # The runner itself always exits 0 after reporting a verdict; a
        # killed or crashed runner's verdict line is not trusted.
        return _check_reply("error", output=output, detail=f"check runner exited {returncode}")
    if status == "error":
        detail = body.get("detail")
        return _check_reply(
            "error", output=output, detail=detail if isinstance(detail, str) else "check error"
        )
    total = _safe_int(body.get("total"), -1)
    passed = _safe_int(body.get("passed"), -1)
    failed = body.get("failed")
    if not isinstance(failed, list):
        failed = []
    if status not in ("passed", "failed") or total < 0 or not 0 <= passed <= total:
        return _check_reply("error", output=output, detail="malformed check result")
    if total != params.expected_tests:
        return _check_reply(
            "error",
            output=output,
            detail=f"ran {total} acceptance tests, expected {params.expected_tests}",
        )
    if (status == "passed" and (failed or passed != total)) or (
        status == "failed" and not failed
    ):
        return _check_reply("error", output=output, detail="inconsistent check result")
    return _check_reply(status, total=total, passed=passed, failed=failed, output=output)


def cmd_check(request: dict[str, Any]) -> int:
    started = time.monotonic()
    run_dir: Path | None = None

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        raise _CheckTerminated

    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    reply: dict[str, Any] | None = None
    try:
        params = _validate_check_request(request)
        run_dir = params.run_dir
        deadline = started + params.timeout_s + CHECK_OVERHEAD_S
        ck = run_dir / "ck"
        # Mark OURSELVES as a reaper before anything sandboxed runs, so a
        # test that double-forks is still reachable by `_reap_all_descendants`.
        _prctl(PR_SET_CHILD_SUBREAPER, 1)
        _fresh_dir(ck, mode=0o700)
        ck.rmdir()  # `git clone` wants to create it itself.
        _fresh_dir(run_dir / "ck-home", mode=0o700)
        try:
            _prepare_check_tree(params, ck, _git_env(), deadline)
        except ChildRefusal as exc:
            reply = _check_reply("error", detail=f"could not assemble the checks: {exc}")
        else:
            _fresh_dir(ck / CHECK_TMP_NAME, mode=0o700)
            sandbox_env = _check_env(run_dir, ck)
            unavailable = _canary_failure(params, ck, sandbox_env, deadline)
            if unavailable is not None:
                reply = _check_reply("unavailable", detail=unavailable)
            else:
                reply = _run_checks(params, ck, sandbox_env, deadline)
    except _CheckTerminated:
        reply = None
        _emit_error("check terminated")
        return 3
    except ChildRefusal as exc:
        reply = None
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        reply = None
        _emit_error(f"check failed: {exc}")
        return 3
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with contextlib.suppress(Exception):
            _reap_all_descendants(grace_s=0.5)
        # Reply first: removing a tree the tests filled with many files can
        # take long, and the parent's own `cleanup` call removes it anyway.
        if reply is not None:
            print(json.dumps(reply), flush=True)
        if run_dir is not None:
            for name in ("ck", "ck-home"):
                shutil.rmtree(run_dir / name, ignore_errors=True)
        signal.signal(signal.SIGTERM, previous)
    return 0


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

SUBCOMMANDS: dict[str, Callable[[dict[str, Any]], int]] = {
    "prepare": cmd_prepare,
    "exec": cmd_exec,
    "package": cmd_package,
    "preflight": cmd_preflight,
    "cleanup": cmd_cleanup,
    "discussion": cmd_discussion,
    "check": cmd_check,
}
# Subcommands whose request carries whole patches (base64).
LARGE_REQUEST_SUBCOMMANDS = frozenset({"preflight", "check"})


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SUBCOMMANDS:
        _emit_error(
            "usage: codex_child.py <prepare|exec|package|preflight|cleanup|discussion|check>"
        )
        return 2
    cap = (
        PREFLIGHT_MAX_REQUEST_BYTES
        if argv[1] in LARGE_REQUEST_SUBCOMMANDS
        else MAX_REQUEST_BYTES
    )
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
