"""Sandboxed Codex process manager. Runs as user agent-codex via sudo.

Subcommands `prepare | exec | package | cleanup` read one JSON request object
(<=256 KiB) from stdin and print JSON (single object for prepare/package/cleanup,
JSONL for exec) on stdout. This process trusts nothing from stdin beyond the
allowlists below: every model/effort/sandbox/lane/cwd value is checked against a
fixed set, every run_id is checked against a fixed pattern, and every path the
child touches is resolved and confirmed to live inside
`<work_root>/<run_id>/` (or inside the mirrors directory for `prepare`'s read).

`agent-codex` is a dedicated system user for this sandbox only (design spec
REVISION 2) — it is deliberately NOT `codex-runner`, which is also the live
GitHub Actions self-hosted runner identity and whose home holds credentials
and tokens that Codex's own sandbox (same uid) could otherwise read.

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
import json
import os
import pwd
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 256 * 1024
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


# --------------------------------------------------------------------------
# git helpers, shared by prepare and package
# --------------------------------------------------------------------------


def _git_argv(*args: str, no_textconv: bool = False) -> list[str]:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.file.allow=always",
    ]
    if no_textconv:
        # Untrusted repositories can configure .gitattributes textconv/diff
        # filters that execute arbitrary commands on `git diff`. This is not
        # in the literal spec command line but is required so `package`
        # cannot be tricked into running attacker-controlled programs.
        command += ["-c", "diff.noTextconv=true"]
    command += list(args)
    return command


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**GIT_ENV_BASE, "PATH": PATH_VALUE, "HOME": HOME_DIR}
    if extra:
        env.update(extra)
    return env


def _run_git(
    args: list[str], *, cwd: Path, env: dict[str, str], timeout: float = 120.0
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
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


def _refuse_if_agent_config_present(wt: Path) -> None:
    for root, dirs, files in os.walk(wt):
        if Path(root) == wt and ".git" in dirs:
            dirs.remove(".git")
        if ".codex" in dirs or ".agents" in dirs or ".codex" in files or ".agents" in files:
            raise ChildRefusal("repository contains .codex or .agents")


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
        for name in ("tmp", "out", "images"):
            (run_dir / name).mkdir(parents=True, exist_ok=True, mode=0o770)
        env = _git_env()
        _run_git(
            _git_argv("clone", "--no-checkout", "--no-hardlinks", str(mirror), str(wt)),
            cwd=run_dir,
            env=env,
        )
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


def _parse_raw_diff(raw_text: str) -> tuple[list[str], str | None]:
    changed_paths: list[str] = []
    for line in raw_text.splitlines():
        if not line.startswith(":"):
            continue
        parts = line[1:].split("\t", 1)
        if len(parts) != 2:
            continue
        meta, path = parts
        fields = meta.split()
        if len(fields) < 4:
            continue
        old_mode, new_mode = fields[0], fields[1]
        if old_mode in BAD_GIT_MODES or new_mode in BAD_GIT_MODES:
            return [], path
        changed_paths.append(path)
    return changed_paths, None


def cmd_package(request: dict[str, Any]) -> int:
    try:
        run_id = _validate_run_id(request.get("run_id"))
        run_dir = _run_dir(run_id)
        wt = run_dir / "wt"
        if not wt.is_dir():
            raise ChildRefusal("worktree is not prepared")
        tmp_dir = run_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o770)
        index_file = tmp_dir / "index"
        env = _git_env({"GIT_INDEX_FILE": str(index_file)})
        try:
            _run_git(_git_argv("read-tree", "HEAD"), cwd=wt, env=env)
            _run_git(_git_argv("add", "-A"), cwd=wt, env=env)
            raw = _run_git(
                _git_argv("diff", "--cached", "--raw", "--no-renames", "HEAD"), cwd=wt, env=env
            )
            raw_text = raw.stdout.decode("utf-8", "replace")
            changed_paths, bad_mode_path = _parse_raw_diff(raw_text)
            if bad_mode_path is not None:
                raise ChildRefusal(
                    f"unsupported change (symlink or submodule): {bad_mode_path}"
                )
            if not changed_paths:
                print(
                    json.dumps({"patch_b64": "", "changed_paths": [], "bytes": 0}), flush=True
                )
                return 0
            patch = _run_git(
                _git_argv(
                    "diff", "--cached", "--binary", "--no-renames", "HEAD", no_textconv=True
                ),
                cwd=wt,
                env=env,
            )
            patch_bytes = patch.stdout
            if len(patch_bytes) > MAX_PATCH_BYTES:
                raise ChildRefusal("patch exceeds the size limit")
        finally:
            with contextlib.suppress(OSError):
                index_file.unlink(missing_ok=True)
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3
    except OSError as exc:
        _emit_error(f"package failed: {exc}")
        return 3
    print(
        json.dumps(
            {
                "patch_b64": base64.b64encode(patch_bytes).decode("ascii"),
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
        for name in ("wt", "tmp", "out"):
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


def _pid_alive(pid: int) -> bool:
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


def kill_tree(
    root_pid: int,
    *,
    proc_table_provider: ProcTableProvider = default_proc_table,
    grace_s: float = TREE_KILL_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """SIGTERM the whole descendant tree rooted at ``root_pid``, then SIGKILL
    whatever is still alive after ``grace_s`` seconds. Sandboxed commands run
    in a different session/process group than the codex process, so we find
    them by walking the ppid chain rather than by process group."""
    table = proc_table_provider()
    targets = [root_pid, *_descendant_pids(root_pid, table)]
    for pid in targets:
        _signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in targets):
            return
        sleep(0.2)
    remaining_table = proc_table_provider()
    remaining_targets = (
        set(targets) | set(_descendant_pids(root_pid, remaining_table)) | {root_pid}
    )
    for pid in remaining_targets:
        _signal_pid(pid, signal.SIGKILL)


# --------------------------------------------------------------------------
# exec: request validation and command construction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecParams:
    run_dir: Path
    lane: str
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
    for raw in raw_images:
        if not isinstance(raw, str):
            raise ChildRefusal("invalid image path")
        images.append(_ensure_within(Path(raw), run_dir))
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
    else:
        cwd_path = run_dir / "empty"
        cwd_path.mkdir(parents=True, exist_ok=True, mode=0o770)
    return ExecParams(
        run_dir=run_dir,
        lane=lane,
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
    if not params.multi_agent:
        command += ["-c", "features.multi_agent=false"]
    if params.output_schema is not None:
        command += ["--output-schema", str(schema_path)]
    command += ["--output-last-message", str(final_path)]
    for image in params.images:
        command += ["--image", str(image)]
    command.append("-")
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
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, 9, 0, 0, 0)  # PR_SET_PDEATHSIG, SIGKILL
    except OSError:
        pass


# --------------------------------------------------------------------------
# exec: streaming and usage accounting
# --------------------------------------------------------------------------


def _pump_stdout_lines(stream: Any, sink: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            sink.put(line.rstrip("\n"))
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _pump_stderr(stream: Any, sink: deque[str]) -> None:
    try:
        for line in stream:
            sink.append(line.rstrip("\n")[:MAX_STDERR_LINE_CHARS])
    except (OSError, ValueError):
        pass


@dataclass
class StreamOutcome:
    timed_out: bool = False
    idle_killed: bool = False
    thread_started: dict[str, Any] | None = None
    fallback_usage: dict[str, int] | None = None


def _stream_and_wait(
    process: subprocess.Popen[str],
    timeout_s: float,
    idle_timeout_s: float,
    signaled: threading.Event,
    *,
    kill: Callable[[int], None] = lambda pid: kill_tree(pid),
) -> StreamOutcome:
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_pump_stdout_lines, args=(process.stdout, lines), daemon=True
    )
    reader.start()

    outcome = StreamOutcome()
    deadline = time.monotonic() + timeout_s
    idle_deadline = time.monotonic() + idle_timeout_s

    while True:
        now = time.monotonic()
        if signaled.is_set():
            kill(process.pid)
            break
        if now >= deadline:
            outcome.timed_out = True
            kill(process.pid)
            break
        if now >= idle_deadline:
            outcome.idle_killed = True
            kill(process.pid)
            break
        remaining = min(deadline, idle_deadline) - now
        wait_for = min(max(remaining, 0.05), 0.5)
        try:
            line = lines.get(timeout=wait_for)
        except queue.Empty:
            continue
        if line is None:
            break
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        idle_deadline = time.monotonic() + idle_timeout_s
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            if event.get("type") == "thread.started":
                outcome.thread_started = event
            elif event.get("type") == "turn.completed":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    outcome.fallback_usage = {
                        key: int(usage.get(key, 0) or 0)
                        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
                    }

    # Drain whatever is already buffered without blocking further.
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            break
        if line is None:
            break
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    reader.join(timeout=2)
    return outcome


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
                    last = {
                        key: int(usage.get(key, 0) or 0)
                        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
                    }
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
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
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
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FINAL_MESSAGE_CHARS)
    except OSError:
        return ""


def cmd_exec(request: dict[str, Any]) -> int:
    try:
        params = _validate_exec_request(request)
    except ChildRefusal as exc:
        _emit_error(str(exc))
        return 3

    out_dir = params.run_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o770)
    (params.run_dir / "tmp").mkdir(parents=True, exist_ok=True, mode=0o770)
    schema_path = out_dir / "schema.json"
    final_path = out_dir / "final.txt"
    if params.output_schema is not None:
        schema_path.write_text(json.dumps(params.output_schema), encoding="utf-8")

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
            text=True,
            bufsize=1,
            start_new_session=True,
            preexec_fn=_preexec_pdeathsig,
        )
    except OSError as exc:
        _emit_error(f"failed to start codex: {exc}")
        return 3

    assert process.stdin is not None and process.stderr is not None
    try:
        process.stdin.write(params.prompt)
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
        outcome = _stream_and_wait(process, params.timeout_s, params.idle_timeout_s, signaled)
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


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

SUBCOMMANDS: dict[str, Callable[[dict[str, Any]], int]] = {
    "prepare": cmd_prepare,
    "exec": cmd_exec,
    "package": cmd_package,
    "cleanup": cmd_cleanup,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SUBCOMMANDS:
        _emit_error("usage: codex_child.py <prepare|exec|package|cleanup>")
        return 2
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
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
