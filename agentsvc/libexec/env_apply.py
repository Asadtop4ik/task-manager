"""Root env-apply helper for agent "ops requests" (owner-approved env changes).

Runs as root via the static oneshot unit `agent-ops-apply.service`
(``ExecStart=/usr/bin/python3 -I /opt/agent-svc/libexec/env_apply.py apply``),
started on demand by agent-svc through the one-argv, no-wildcard sudo rule in
``ops/agent-svc.sudoers`` (``sudo -n systemctl start agent-ops-apply.service``
-- no request data ever reaches this process through sudo itself). An
operator can also run ``env_apply.py rollback --request-id <uuid>`` by hand;
that subcommand is deliberately NOT in sudoers.

Spec: agent-svc-notes/ops-requests-spec.md section 3-4 and 11, work package
WP-D.

Trust boundary: this is the ONLY thing in the whole ops-requests feature
that ever reads or writes a production ``/srv/stack/env/<stack>.env`` file
or restarts a production stack. It never trusts what agent-svc (itself
unprivileged, and the thing this feature is designed to survive a
compromise of) wrote to ``/run/agent-svc/ops/request.json`` beyond the
file's own ownership/shape: every field is independently re-validated
against the root-owned allowlist and the request_hash is recomputed from
scratch -- exactly the way ``codex_child.py`` never trusts the caller
beyond a few fixed allowlists, and ``image_state.py`` never trusts argv
beyond the trusted catalog's own container names.

Imports ``agent_repos.py``, ``agent_ops_policy.py`` and ``env_file_lock.py``
BY PATH from ``/opt/agent-svc/trusted`` -- the same pattern
``agentsvc/libexec/image_state.py::_load_agent_repos`` uses for
``agent_repos.py``. ``env_file_lock.py`` is loaded (and required by the
installer) for its lock-file-naming *contract* -- a sibling ``<path>.lock``
file -- so this helper's own locking is compatible with anything else that
ever locks the same env file; its own ``locked_env_file`` blocks
indefinitely (``LOCK_EX``, no ``LOCK_NB``), which is wrong for a helper that
must fail fast with ``busy`` rather than hang the whole oneshot unit, so the
actual non-blocking-with-retry acquisition is reimplemented here against the
identical lock-file path.

Test overrides: like ``codex_child.py``, module-level path/identity
constants can be overridden with environment variables prefixed
``AGENT_OPS_APPLY_TEST_``, only when ``AGENT_OPS_APPLY_TEST_MODE=1`` is also
set, and refused outright when the effective user is root (the only
identity this helper ever really runs as in production) -- so a stray
environment variable can never affect a real invocation: a real
``systemctl start``/operator ``sudo`` run always execs as root, and
``env_reset`` (sudoers default) strips the caller's environment for the
``systemctl start`` path regardless. Dependencies that cannot be expressed
as a path or a string (the docker/compose subprocess runner, the
verification wait, the readiness prober, the syslog call) are plain keyword
parameters on ``main``/``cmd_apply``/``cmd_rollback``, following
``image_state.py``'s ``runner=subprocess.run`` shape.

Never prints, logs, or persists any config VALUE anywhere except the
root-only (0700) backup file, which must hold the real old/new text to be
able to restore it -- exactly as sensitive as the env file itself, and never
read by agent-svc. Stdout, the result file (0640 root:agent-svc), and the
audit log carry only sha256 hashes of values and a `message` drawn from the
fixed vocabulary in `MESSAGES` below -- never request data interpolated in.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Sleep = Callable[[float], None]
Probe = Callable[[str, float], bool]
LoggerRunner = Callable[[str], None]

# --------------------------------------------------------------------------
# Test-mode overrides (see module docstring). Mirrors codex_child.py's
# `_test_mode_enabled`/`_test_override` exactly, minus the second "not this
# process's one fixed production identity" guard: codex_child.py's
# production identity is `agent-codex` (a second, distinct guard on top of
# "not root"); this helper's only production identity IS root, so the single
# euid check already covers it completely.
# --------------------------------------------------------------------------


def _test_mode_enabled() -> bool:
    if os.environ.get("AGENT_OPS_APPLY_TEST_MODE") != "1":
        return False
    return os.geteuid() != 0


TEST_MODE = _test_mode_enabled()


def _test_override(name: str) -> str | None:
    if not TEST_MODE:
        return None
    return os.environ.get(f"AGENT_OPS_APPLY_TEST_{name}")


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


def _env_int(name: str, default: int) -> int:
    override = _test_override(name)
    if not override:
        return default
    try:
        return int(override)
    except ValueError:
        return default


REQUEST_PATH = _env_path("REQUEST_PATH", "/run/agent-svc/ops/request.json")
TRUSTED_DIR = _env_path("TRUSTED_DIR", "/opt/agent-svc/trusted")
ALLOWLIST_PATH = _env_path("ALLOWLIST_PATH", "/etc/agent-svc/ops-allowlist.json")
STATE_DIR = _env_path("STATE_DIR", "/var/lib/agent-ops")
LOG_DIR = _env_path("LOG_DIR", "/var/log/agent-ops")
ENV_FILE_ROOT = _env_path("ENV_FILE_ROOT", "/srv/stack/env")
STACK_ROOT = _env_path("STACK_ROOT", "/srv/stack")
DOCKER_BINARY = _env_str("DOCKER_BINARY", "/usr/bin/docker")
LOGGER_BINARY = _env_str("LOGGER_BINARY", "/usr/bin/logger")
PATH_VALUE = _env_str("PATH", "/usr/bin:/bin")
HOME_VALUE = _env_str("HOME", "/var/lib/agent-ops")

MAX_REQUEST_BYTES = 8 * 1024
MAX_ALLOWLIST_PROJECT_KEY = 40
MAX_ENV_FILE_BYTES = 64 * 1024
LOCK_TIMEOUT_S = _env_float("LOCK_TIMEOUT_S", 30.0)
LOCK_RETRY_INTERVAL_S = _env_float("LOCK_RETRY_INTERVAL_S", 0.5)
COMPOSE_WAIT_TIMEOUT_S = 180
COMPOSE_SUBPROCESS_TIMEOUT_S = _env_float("COMPOSE_SUBPROCESS_TIMEOUT_S", 210.0)
RESTART_STABLE_WINDOW_S = _env_float("RESTART_STABLE_WINDOW_S", 20.0)
READY_PROBE_ATTEMPTS = 5
READY_PROBE_INTERVAL_S = _env_float("READY_PROBE_INTERVAL_S", 2.0)
READY_PROBE_TIMEOUT_S = _env_float("READY_PROBE_TIMEOUT_S", 3.0)
DOCKER_INSPECT_TIMEOUT_S = 15.0

RUN_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
PROJECT_KEY_RE = re.compile(r"[a-z0-9-]{1,40}")
REQUEST_HASH_RE = re.compile(r"[0-9a-f]{64}")
_REQUEST_FIELDS = frozenset(
    {
        "v",
        "mode",
        "request_id",
        "run_id",
        "project",
        "kind",
        "key",
        "op",
        "value",
        "request_hash",
    }
)

# --------------------------------------------------------------------------
# Result/exit code vocabulary (spec section 4). `message` is always one of
# these tokens (or an `agent_ops_policy.PolicyError`/`validate_request`
# reason code, itself also a short fixed token) -- never request data.
# --------------------------------------------------------------------------

CODE_APPLIED = "applied"
CODE_ALREADY_APPLIED = "already_applied"
CODE_BAD_REQUEST = "bad_request"
CODE_REFUSED = "refused"
CODE_PRECONDITION = "precondition"
CODE_FAILED_ROLLED_BACK = "failed_rolled_back"
CODE_FAILED_ROLLBACK_FAILED = "failed_rollback_failed"
CODE_BUSY = "busy"

EXIT_CODES = {
    CODE_APPLIED: 0,
    CODE_ALREADY_APPLIED: 0,
    CODE_BAD_REQUEST: 2,
    CODE_REFUSED: 3,
    CODE_PRECONDITION: 4,
    CODE_FAILED_ROLLED_BACK: 5,
    CODE_FAILED_ROLLBACK_FAILED: 6,
    CODE_BUSY: 7,
}

# Every `message` this helper can ever emit. A fixed, closed vocabulary --
# `_write_result` asserts against it so a future edit can never accidentally
# start interpolating request data into what is otherwise just a token.
MESSAGES = frozenset(
    {
        "ok",
        # bad_request (2): the input file itself cannot be trusted enough to
        # even know a request_id.
        "symlink",
        "not_regular_file",
        "wrong_owner",
        "too_large",
        "invalid_json",
        "not_an_object",
        "unknown_fields",
        "missing_fields",
        "bad_v",
        "bad_mode",
        "bad_request_id",
        "bad_run_id",
        "bad_project",
        "bad_kind",
        "bad_key_type",
        "bad_op_type",
        "bad_value_type",
        "bad_request_hash",
        # refused (3): policy/hash/project -- nothing touched.
        "hash_mismatch",
        "cannot_load_trusted_modules",
        "cannot_load_allowlist",
        # agent_ops_policy.validate_request reason codes, reused verbatim
        # ("bad_kind" is already listed above for the request-shape check):
        "bad_key",
        "secret_key",
        "not_allowlisted",
        "bad_op",
        "bad_value",
        "project_denied",
        "repo_mismatch",
        "item_pattern",
        # precondition (4): key/line/image/env problems -- nothing changed.
        "container_missing",
        "container_not_running",
        "images_disagree",
        "image_not_pinned_sha",
        "env_missing",
        "env_symlink",
        "env_not_regular_file",
        "env_too_large",
        "env_not_utf8",
        "key_missing_line",
        "duplicate_key_line",
        "case_variant_key_line",
        "export_key_line",
        "indented_key_line",
        "unsupported_quoting",
        # agent_ops_policy.apply_op PolicyError codes, reused verbatim:
        "bad_format",
        "missing_key",
        "malformed_list",
        "max_items",
        "protected_item",
        "empty_result",
        # failure/rollback (5/6): the value applied to disk and restart ran.
        "compose_failed",
        "container_not_running_after_restart",
        "unhealthy",
        "restart_count_unstable",
        "image_mismatch",
        "env_line_mismatch",
        "ready_probe_failed",
        "rollback_compose_failed",
        "rollback_verify_failed",
        # busy (7):
        "lock_timeout",
        # rollback CLI only:
        "backup_not_found",
        "backup_invalid",
        "line_changed",
    }
)


class PolicyModules:
    """The three trusted modules, loaded by path (see module docstring)."""

    def __init__(self, agent_repos: ModuleType, policy: ModuleType, env_file_lock: ModuleType):
        self.agent_repos = agent_repos
        self.policy = policy
        self.env_file_lock = env_file_lock


class HelperError(Exception):
    """A terminal, already-classified outcome: `code` is one of the CODE_*
    constants, `message` one of MESSAGES."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _bad_request(message: str) -> HelperError:
    return HelperError(CODE_BAD_REQUEST, message)


def _refused(message: str) -> HelperError:
    return HelperError(CODE_REFUSED, message)


def _precondition(message: str) -> HelperError:
    return HelperError(CODE_PRECONDITION, message)


def _busy(message: str) -> HelperError:
    return HelperError(CODE_BUSY, message)


# --------------------------------------------------------------------------
# Trusted module loading -- copy of image_state.py's `_load_agent_repos`,
# generalized to any file name.
# --------------------------------------------------------------------------


def _load_module_by_path(path: Path, *, label: str) -> ModuleType:
    module_name = f"env_apply_{label}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: `agent_ops_policy.py` uses
    # `from __future__ import annotations` (postponed evaluation), so its
    # `@dataclass(frozen=True)` classes need `sys.modules[cls.__module__]`
    # to resolve their (now string) field annotations while the class body
    # itself is still executing -- without this, `dataclasses._is_type`
    # crashes with `AttributeError: 'NoneType' object has no attribute
    # '__dict__'`. `image_state.py::_load_agent_repos` gets away without
    # this only because `agent_repos.py` does not use postponed
    # annotations; a module that does needs the registration.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_trusted_modules(trusted_dir: Path) -> PolicyModules:
    agent_repos = _load_module_by_path(trusted_dir / "agent_repos.py", label="agent_repos")
    policy = _load_module_by_path(
        trusted_dir / "agent_ops_policy.py", label="agent_ops_policy"
    )
    env_file_lock = _load_module_by_path(
        trusted_dir / "env_file_lock.py", label="env_file_lock"
    )
    return PolicyModules(agent_repos, policy, env_file_lock)


# --------------------------------------------------------------------------
# Request file: O_NOFOLLOW, regular, owned by agent-svc's uid, <=8 KB, exact
# field set.
# --------------------------------------------------------------------------


def resolve_agent_svc_uid() -> int:
    override = _test_override("AGENT_SVC_UID")
    if override is not None:
        return int(override)
    return pwd.getpwnam("agent-svc").pw_uid


def read_request_file(path: Path, *, expected_uid: int) -> dict[str, Any]:
    """Read, ownership-check and structurally validate the request file.

    Raises `HelperError(CODE_BAD_REQUEST, ...)` for anything that means the
    file cannot be trusted at all -- including a wrong owner, which is
    checked from the SAME open fd's fstat (never a separate lstat/stat call
    that a rename could race against).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # final component is a symlink
            raise _bad_request("symlink") from exc
        raise _bad_request("not_regular_file") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _bad_request("not_regular_file")
        if st.st_uid != expected_uid:
            raise _bad_request("wrong_owner")
        if st.st_size > MAX_REQUEST_BYTES:
            raise _bad_request("too_large")
        raw = os.read(fd, MAX_REQUEST_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_REQUEST_BYTES:
        raise _bad_request("too_large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _bad_request("invalid_json") from exc
    if not isinstance(data, dict):
        raise _bad_request("not_an_object")
    _validate_request_shape(data)
    return data


def _validate_request_shape(data: dict[str, Any]) -> None:
    fields = set(data)
    if fields - _REQUEST_FIELDS:
        raise _bad_request("unknown_fields")
    if _REQUEST_FIELDS - fields:
        raise _bad_request("missing_fields")
    if data.get("v") != 1:
        raise _bad_request("bad_v")
    if data.get("mode") != "apply":
        raise _bad_request("bad_mode")
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise _bad_request("bad_request_id")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise _bad_request("bad_run_id")
    project = data.get("project")
    if not isinstance(project, str) or not PROJECT_KEY_RE.fullmatch(project):
        raise _bad_request("bad_project")
    kind = data.get("kind")
    if not isinstance(kind, str) or not (1 <= len(kind) <= 32):
        raise _bad_request("bad_kind")
    key = data.get("key")
    if not isinstance(key, str) or not (1 <= len(key) <= 64):
        raise _bad_request("bad_key_type")
    op = data.get("op")
    if not isinstance(op, str) or not (1 <= len(op) <= 16):
        raise _bad_request("bad_op_type")
    value = data.get("value")
    if not isinstance(value, str) or not (1 <= len(value) <= 256):
        raise _bad_request("bad_value_type")
    request_hash = data.get("request_hash")
    if not isinstance(request_hash, str) or not REQUEST_HASH_RE.fullmatch(request_hash):
        raise _bad_request("bad_request_hash")


# --------------------------------------------------------------------------
# Non-blocking, retrying lock on `<env_file>.lock` -- same sibling-lock-file
# convention as `env_file_lock.locked_env_file`, reimplemented with
# LOCK_NB + retry since that function blocks indefinitely (see module
# docstring).
# --------------------------------------------------------------------------


@contextlib.contextmanager
def locked_env_file_nonblocking(
    path: Path, *, timeout_s: float, retry_interval_s: float, sleep: Sleep
):
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _busy("lock_timeout") from None
                sleep(retry_interval_s)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------------------
# Docker inspect.
# --------------------------------------------------------------------------


def docker_inspect(name: str, *, runner: Runner) -> dict[str, Any] | None:
    try:
        completed = runner(
            [
                DOCKER_BINARY,
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .}}",
                "--",
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DOCKER_INSPECT_TIMEOUT_S,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


_SHA40_RE = re.compile(r"[0-9a-f]{40}")


def check_containers_pinned(
    containers: tuple[tuple[str, str], ...], *, runner: Runner
) -> tuple[dict[str, dict[str, Any]], str]:
    """docker-inspect every allowlisted container; require all running with
    image `<repo>:<same 40-hex sha>`. Returns (raw states by name, pinned sha).
    """
    states: dict[str, dict[str, Any]] = {}
    for name, _repo in containers:
        raw = docker_inspect(name, runner=runner)
        if raw is None:
            raise _precondition("container_missing")
        states[name] = raw
    tags: set[str] = set()
    for name, repo in containers:
        raw = states[name]
        state = raw.get("State")
        if not isinstance(state, dict) or not state.get("Running"):
            raise _precondition("container_not_running")
        config = raw.get("Config")
        image = config.get("Image") if isinstance(config, dict) else None
        if not isinstance(image, str) or not image.startswith(f"{repo}:"):
            raise _precondition("images_disagree")
        tag = image[len(repo) + 1 :]
        if not _SHA40_RE.fullmatch(tag):
            raise _precondition("image_not_pinned_sha")
        tags.add(tag)
    if len(tags) != 1:
        raise _precondition("images_disagree")
    return states, next(iter(tags))


def _container_env_has(raw: dict[str, Any], key: str, raw_value: str) -> bool:
    config = raw.get("Config")
    env_list = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(env_list, list):
        return False
    target = f"{key}={raw_value}"
    return any(entry == target for entry in env_list if isinstance(entry, str))


# --------------------------------------------------------------------------
# Env file line parsing (spec section 4 step 3).
# --------------------------------------------------------------------------


@dataclass
class EnvLine:
    quote_style: str  # "none" | "single" | "double"
    raw_value: str  # value text, quotes stripped
    ending: bytes  # b"\n", b"\r\n" or b"" (last line, no trailing newline)


@dataclass
class ParsedEnvFile:
    content: bytes
    st: os.stat_result
    lines: list[bytes]  # each WITH its own line ending (splitlines(keepends=True))
    target_index: int
    target: EnvLine


def _quote_and_extract(rest: bytes) -> tuple[str, str] | None:
    """`rest` is the line's bytes after `KEY=`, with any trailing newline
    already stripped. Returns (quote_style, raw_value) or None if the
    quoting is unsupported (refused as a precondition by the caller)."""
    try:
        text = rest.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'" and "'" not in text[1:-1]:
        return "single", text[1:-1]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"' and '"' not in text[1:-1]:
        if "\\" in text[1:-1]:
            return None
        return "double", text[1:-1]
    if any(ch in text for ch in ("'", '"', "\\")):
        return None
    return "none", text


def read_env_file(path: Path, *, key: str) -> ParsedEnvFile:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _precondition("env_symlink") from exc
        raise _precondition("env_missing") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _precondition("env_not_regular_file")
        if st.st_size > MAX_ENV_FILE_BYTES:
            raise _precondition("env_too_large")
        content = os.read(fd, MAX_ENV_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(content) > MAX_ENV_FILE_BYTES:
        raise _precondition("env_too_large")

    lines = content.splitlines(keepends=True)
    key_bytes = key.encode("ascii")
    exact_re = re.compile(rb"^" + re.escape(key_bytes) + rb"=")
    ci_re = re.compile(rb"^" + re.escape(key_bytes) + rb"=", re.IGNORECASE)
    export_re = re.compile(
        rb"^export[ \t]+" + re.escape(key_bytes) + rb"[ \t]*=", re.IGNORECASE
    )
    indented_re = re.compile(rb"^[ \t]+" + re.escape(key_bytes) + rb"[ \t]*=", re.IGNORECASE)

    target_index: int | None = None
    exact_count = 0
    for index, raw_line in enumerate(lines):
        if export_re.match(raw_line):
            raise _precondition("export_key_line")
        if indented_re.match(raw_line):
            raise _precondition("indented_key_line")
        if exact_re.match(raw_line):
            exact_count += 1
            target_index = index
        elif ci_re.match(raw_line):
            raise _precondition("case_variant_key_line")
    if exact_count == 0:
        raise _precondition("key_missing_line")
    if exact_count > 1:
        raise _precondition("duplicate_key_line")

    assert target_index is not None
    raw_line = lines[target_index]
    if raw_line.endswith(b"\r\n"):
        ending = b"\r\n"
        body = raw_line[:-2]
    elif raw_line.endswith(b"\n"):
        ending = b"\n"
        body = raw_line[:-1]
    else:
        ending = b""
        body = raw_line
    rest = body[len(key_bytes) + 1 :]
    parsed = _quote_and_extract(rest)
    if parsed is None:
        raise _precondition("unsupported_quoting")
    quote_style, raw_value = parsed
    if not (1 <= len(raw_value) <= 256):
        raise _precondition("unsupported_quoting")
    return ParsedEnvFile(
        content=content,
        st=st,
        lines=lines,
        target_index=target_index,
        target=EnvLine(quote_style=quote_style, raw_value=raw_value, ending=ending),
    )


def _render_line(key: str, value: str, quote_style: str, ending: bytes) -> bytes:
    if quote_style == "single":
        body = f"{key}='{value}'"
    elif quote_style == "double":
        body = f'{key}="{value}"'
    else:
        body = f"{key}={value}"
    return body.encode("utf-8") + ending


def write_env_line(
    path: Path,
    parsed: ParsedEnvFile,
    *,
    key: str,
    new_value: str,
) -> None:
    """Atomically replace exactly the target line, preserving every other
    line byte-for-byte and the original file's uid/gid/mode."""
    new_line = _render_line(key, new_value, parsed.target.quote_style, parsed.target.ending)
    new_lines = list(parsed.lines)
    new_lines[parsed.target_index] = new_line
    new_content = b"".join(new_lines)

    directory = path.parent
    tmp_name = f".{path.name}.tmp-{uuid.uuid4().hex}"
    tmp_path = directory / tmp_name
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, new_content)
        os.fchmod(fd, stat.S_IMODE(parsed.st.st_mode))
        with contextlib.suppress(OSError):
            os.fchown(fd, parsed.st.st_uid, parsed.st.st_gid)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, path)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# Compose restart.
# --------------------------------------------------------------------------


def compose_argv(stack: str, services: tuple[str, ...]) -> list[str]:
    return [
        DOCKER_BINARY,
        "compose",
        "-f",
        f"stacks/{stack}.yml",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        str(COMPOSE_WAIT_TIMEOUT_S),
        *services,
    ]


def run_compose(
    stack: str,
    services: tuple[str, ...],
    *,
    image_tag: str,
    runner: Runner,
    log_path: Path,
) -> bool:
    env = {
        "PATH": PATH_VALUE,
        "IMAGE_TAG": image_tag,
        "DOCKER_CONFIG": str(STATE_DIR / "docker-config"),
        "HOME": HOME_VALUE,
    }
    try:
        completed = runner(
            compose_argv(stack, services),
            cwd=str(STACK_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=COMPOSE_SUBPROCESS_TIMEOUT_S,
            text=True,
            check=False,
        )
        ok = completed.returncode == 0
        output = completed.stdout or ""
    except subprocess.TimeoutExpired as exc:
        ok = False
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += "\n[env_apply] compose timed out\n"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, output.encode("utf-8", "replace"))
    finally:
        os.close(fd)
    return ok


# --------------------------------------------------------------------------
# Verify.
# --------------------------------------------------------------------------


def verify_containers(
    containers: tuple[tuple[str, str], ...],
    *,
    key: str,
    expected_value: str,
    pinned_tag: str,
    runner: Runner,
    sleep: Sleep,
) -> str | None:
    """Returns None on success, else a MESSAGES token describing the failure."""
    states: dict[str, dict[str, Any]] = {}
    for name, _repo in containers:
        raw = docker_inspect(name, runner=runner)
        if raw is None:
            return "container_not_running_after_restart"
        states[name] = raw

    for name, _repo in containers:
        state = states[name].get("State")
        if not isinstance(state, dict) or not state.get("Running"):
            return "container_not_running_after_restart"

    for name, _repo in containers:
        state = states[name].get("State")
        health = state.get("Health") if isinstance(state, dict) else None
        if isinstance(health, dict) and health.get("Status") != "healthy":
            return "unhealthy"

    no_healthcheck = [
        name
        for name, _repo in containers
        if not isinstance((states[name].get("State") or {}).get("Health"), dict)
    ]
    if no_healthcheck:
        before = {name: states[name].get("RestartCount") for name in no_healthcheck}
        sleep(RESTART_STABLE_WINDOW_S)
        for name in no_healthcheck:
            raw = docker_inspect(name, runner=runner)
            if raw is None:
                return "container_not_running_after_restart"
            if raw.get("RestartCount") != before[name]:
                return "restart_count_unstable"

    for name, repo in containers:
        raw = docker_inspect(name, runner=runner) if name in no_healthcheck else states[name]
        if raw is None:
            return "container_not_running_after_restart"
        config = raw.get("Config")
        image = config.get("Image") if isinstance(config, dict) else None
        if image != f"{repo}:{pinned_tag}":
            return "image_mismatch"
        if not _container_env_has(raw, key, expected_value):
            return "env_line_mismatch"

    return None


def probe_ready_url(url: str, *, probe: Probe, sleep: Sleep) -> bool:
    for attempt in range(READY_PROBE_ATTEMPTS):
        if attempt > 0:
            sleep(READY_PROBE_INTERVAL_S)
        if probe(url, READY_PROBE_TIMEOUT_S):
            return True
    return False


def default_probe(url: str, timeout_s: float) -> bool:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            body = response.read(4096)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and parsed.get("status") == "ok"


# --------------------------------------------------------------------------
# Audit log + syslog.
# --------------------------------------------------------------------------


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_logger_runner(line: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [LOGGER_BINARY, "-t", "agent-ops", line],
            check=False,
            timeout=5.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def append_audit_log(
    log_dir: Path,
    entry: dict[str, Any],
    *,
    logger_runner: LoggerRunner,
) -> None:
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    audit_path = log_dir / "audit.jsonl"
    fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    logger_runner(line)


# --------------------------------------------------------------------------
# Result file.
# --------------------------------------------------------------------------


def _agent_svc_gid() -> int | None:
    override = _test_override("AGENT_SVC_GID")
    if override is not None:
        return int(override)
    try:
        return pwd.getpwnam("agent-svc").pw_gid
    except KeyError:
        return None


def write_result_file(results_dir: Path, request_id: str, result: dict[str, Any]) -> None:
    path = results_dir / f"{request_id}.json"
    tmp_path = results_dir / f".{request_id}.json.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps(result, sort_keys=True).encode("utf-8"))
        # 0640 root:agent-svc (spec section 4): `os.open`'s own `mode` is
        # subject to the unit's UMask=0077, which would silently drop the
        # group-read bit -- set it explicitly instead of relying on it.
        os.fchmod(fd, 0o640)
        gid = _agent_svc_gid()
        if gid is not None:
            with contextlib.suppress(OSError):
                os.fchown(fd, -1, gid)  # -1: never touch the owner, group only
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


def read_existing_result(results_dir: Path, request_id: str) -> dict[str, Any] | None:
    path = results_dir / f"{request_id}.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Backups (rollback support). Root-only (0700 dir enforced by tmpfiles);
# this is the one place a real value legitimately persists in cleartext.
# --------------------------------------------------------------------------


def write_backup(
    backups_dir: Path,
    project_key: str,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    project_dir = backups_dir / project_key
    project_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = project_dir / f"{request_id}.json"
    tmp_path = project_dir / f".{request_id}.json.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


def find_backup(backups_dir: Path, request_id: str) -> dict[str, Any] | None:
    if not backups_dir.is_dir():
        return None
    for project_dir in sorted(backups_dir.iterdir()):
        candidate = project_dir / f"{request_id}.json"
        if candidate.is_file():
            try:
                with open(candidate, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None
    return None


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


@dataclass
class Deps:
    request_path: Path = field(default_factory=lambda: REQUEST_PATH)
    trusted_dir: Path = field(default_factory=lambda: TRUSTED_DIR)
    allowlist_path: Path = field(default_factory=lambda: ALLOWLIST_PATH)
    state_dir: Path = field(default_factory=lambda: STATE_DIR)
    log_dir: Path = field(default_factory=lambda: LOG_DIR)
    env_file_root: Path = field(default_factory=lambda: ENV_FILE_ROOT)
    runner: Runner = subprocess.run
    sleep: Sleep = time.sleep
    probe: Probe = default_probe
    logger_runner: LoggerRunner = default_logger_runner
    agent_svc_uid: int | None = None


def _finish(
    deps: Deps,
    *,
    code: str,
    message: str,
    request_id: str | None,
    run_id: str | None = None,
    project_key: str | None = None,
    key: str | None = None,
    op: str | None = None,
    value: str | None = None,
    old_raw: str | None = None,
    new_raw: str | None = None,
    restarted: bool = False,
    rolled_back: bool = False,
    image_tag: str | None = None,
    started_at: float | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    assert message in MESSAGES, f"message {message!r} is not in the fixed vocabulary"
    exit_code = EXIT_CODES[code]
    result = {
        "v": 1,
        "request_id": request_id,
        "code": code,
        "exit": exit_code,
        "message": message,
        "rolled_back": rolled_back,
        "restarted": restarted,
        "image_tag": image_tag,
    }
    if persist and request_id is not None:
        results_dir = deps.state_dir / "results"
        with contextlib.suppress(OSError):
            write_result_file(results_dir, request_id, result)
        duration_ms = int((time.monotonic() - started_at) * 1000) if started_at else None
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "request_id": request_id,
            "run_id": run_id,
            "project": project_key,
            "key": key,
            "op": op,
            "value_sha256": _sha256_hex(value) if value is not None else None,
            "before_sha256": _sha256_hex(old_raw) if old_raw is not None else None,
            "after_sha256": _sha256_hex(new_raw) if new_raw is not None else None,
            "code": code,
            "exit": exit_code,
            "restarted": restarted,
            "rolled_back": rolled_back,
            "image_tag": image_tag,
            "duration_ms": duration_ms,
        }
        with contextlib.suppress(OSError):
            append_audit_log(deps.log_dir, entry, logger_runner=deps.logger_runner)
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def cmd_apply(deps: Deps) -> tuple[dict[str, Any], int]:
    started_at = time.monotonic()
    try:
        expected_uid = (
            deps.agent_svc_uid if deps.agent_svc_uid is not None else resolve_agent_svc_uid()
        )
        request = read_request_file(deps.request_path, expected_uid=expected_uid)
    except HelperError as exc:
        result = _finish(
            deps, code=exc.code, message=exc.message, request_id=None, persist=False
        )
        return result, EXIT_CODES[exc.code]

    request_id = request["request_id"]
    run_id = request["run_id"]
    project_key = request["project"]
    kind = request["kind"]
    key = request["key"]
    op = request["op"]
    value = request["value"]
    claimed_hash = request["request_hash"]

    results_dir = deps.state_dir / "results"
    existing = read_existing_result(results_dir, request_id)
    if existing is not None and existing.get("code") in (CODE_APPLIED, CODE_ALREADY_APPLIED):
        print(json.dumps(existing, sort_keys=True), flush=True)
        return existing, existing.get("exit", 0)

    def fail(exc: HelperError, **extra: Any) -> tuple[dict[str, Any], int]:
        result = _finish(
            deps,
            code=exc.code,
            message=exc.message,
            request_id=request_id,
            run_id=run_id,
            project_key=project_key,
            key=key,
            op=op,
            value=value,
            started_at=started_at,
            **extra,
        )
        return result, EXIT_CODES[exc.code]

    try:
        modules = load_trusted_modules(deps.trusted_dir)
    except (OSError, RuntimeError, SyntaxError):
        return fail(_refused("cannot_load_trusted_modules"))

    policy = modules.policy
    try:
        allowlist = policy.load_allowlist(
            deps.allowlist_path,
            modules.agent_repos.REPOSITORIES,
            require_root_owned=not TEST_MODE,
        )
    except policy.PolicyError:
        return fail(_refused("cannot_load_allowlist"))

    project = allowlist.projects.get(project_key)
    repo_full_name = project.repo_full_name if project is not None else ""
    ok, reason_code = policy.validate_request(
        {"kind": kind, "key": key, "op": op, "value": value},
        allowlist,
        project_key=project_key,
        repo_full_name=repo_full_name,
    )
    if not ok:
        return fail(_refused(reason_code))

    assert project is not None
    recomputed_hash = policy.request_hash(
        run_id=run_id, project_key=project_key, kind=kind, key=key, op=op, value=value
    )
    if recomputed_hash != claimed_hash:
        return fail(_refused("hash_mismatch"))

    key_policy = project.keys[key]

    try:
        container_states, pinned_tag = check_containers_pinned(
            project.containers, runner=deps.runner
        )
    except HelperError as exc:
        return fail(exc, image_tag=None)

    env_path = deps.env_file_root / f"{project.stack}.env"
    log_path = deps.log_dir / f"compose-{request_id}.log"

    try:
        with locked_env_file_nonblocking(
            env_path,
            timeout_s=LOCK_TIMEOUT_S,
            retry_interval_s=LOCK_RETRY_INTERVAL_S,
            sleep=deps.sleep,
        ):
            try:
                parsed = read_env_file(env_path, key=key)
            except HelperError as exc:
                return fail(exc, image_tag=pinned_tag)

            old_raw = parsed.target.raw_value
            try:
                new_raw, changed = policy.apply_op(
                    key_policy.format, old_raw, op, value, key_policy
                )
            except policy.PolicyError as exc:
                return fail(_precondition(exc.args[0]), old_raw=old_raw, image_tag=pinned_tag)

            already_live = all(
                _container_env_has(container_states[name], key, new_raw)
                for name, _repo in project.containers
            )
            if not changed and already_live:
                result = _finish(
                    deps,
                    code=CODE_ALREADY_APPLIED,
                    message="ok",
                    request_id=request_id,
                    run_id=run_id,
                    project_key=project_key,
                    key=key,
                    op=op,
                    value=value,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    restarted=False,
                    rolled_back=False,
                    image_tag=pinned_tag,
                    started_at=started_at,
                )
                return result, 0

            backup_payload = {
                "v": 1,
                "request_id": request_id,
                "run_id": run_id,
                "project_key": project_key,
                "stack": project.stack,
                "key": key,
                "quote_style": parsed.target.quote_style,
                "old_raw": old_raw,
                "new_raw": new_raw,
                "pinned_image_tag": pinned_tag,
                "services": list(project.services),
                "containers": [list(pair) for pair in project.containers],
                "ts": datetime.now(UTC).isoformat(),
            }
            write_backup(deps.state_dir / "backups", project_key, request_id, backup_payload)

            write_env_line(env_path, parsed, key=key, new_value=new_raw)

            compose_ok = run_compose(
                project.stack,
                project.services,
                image_tag=pinned_tag,
                runner=deps.runner,
                log_path=log_path,
            )

            failure_message: str | None = None if compose_ok else "compose_failed"
            if compose_ok:
                failure_message = verify_containers(
                    project.containers,
                    key=key,
                    expected_value=new_raw,
                    pinned_tag=pinned_tag,
                    runner=deps.runner,
                    sleep=deps.sleep,
                )
            if (
                failure_message is None
                and project.ready_url is not None
                and not probe_ready_url(project.ready_url, probe=deps.probe, sleep=deps.sleep)
            ):
                failure_message = "ready_probe_failed"

            if failure_message is None:
                result = _finish(
                    deps,
                    code=CODE_APPLIED,
                    message="ok",
                    request_id=request_id,
                    run_id=run_id,
                    project_key=project_key,
                    key=key,
                    op=op,
                    value=value,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    restarted=True,
                    rolled_back=False,
                    image_tag=pinned_tag,
                    started_at=started_at,
                )
                return result, 0

            # --- rollback ---
            reparsed = read_env_file(env_path, key=key)
            write_env_line(env_path, reparsed, key=key, new_value=old_raw)
            rollback_compose_ok = run_compose(
                project.stack,
                project.services,
                image_tag=pinned_tag,
                runner=deps.runner,
                log_path=log_path,
            )
            rollback_verify_failure: str | None = (
                None if rollback_compose_ok else "rollback_compose_failed"
            )
            if rollback_compose_ok:
                rollback_verify_failure = verify_containers(
                    project.containers,
                    key=key,
                    expected_value=old_raw,
                    pinned_tag=pinned_tag,
                    runner=deps.runner,
                    sleep=deps.sleep,
                )
                if rollback_verify_failure is not None:
                    rollback_verify_failure = "rollback_verify_failed"

            if rollback_verify_failure is None:
                result = _finish(
                    deps,
                    code=CODE_FAILED_ROLLED_BACK,
                    message=failure_message,
                    request_id=request_id,
                    run_id=run_id,
                    project_key=project_key,
                    key=key,
                    op=op,
                    value=value,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    restarted=True,
                    rolled_back=True,
                    image_tag=pinned_tag,
                    started_at=started_at,
                )
                return result, EXIT_CODES[CODE_FAILED_ROLLED_BACK]

            result = _finish(
                deps,
                code=CODE_FAILED_ROLLBACK_FAILED,
                message=failure_message,
                request_id=request_id,
                run_id=run_id,
                project_key=project_key,
                key=key,
                op=op,
                value=value,
                old_raw=old_raw,
                new_raw=new_raw,
                restarted=True,
                rolled_back=False,
                image_tag=pinned_tag,
                started_at=started_at,
            )
            return result, EXIT_CODES[CODE_FAILED_ROLLBACK_FAILED]
    except HelperError as exc:
        # `_busy` raised by locked_env_file_nonblocking: never persisted,
        # never audited -- purely transient, agent-svc retries.
        result = _finish(
            deps, code=exc.code, message=exc.message, request_id=request_id, persist=False
        )
        return result, EXIT_CODES[exc.code]


# --------------------------------------------------------------------------
# rollback CLI (operator only, never in sudoers).
# --------------------------------------------------------------------------


def cmd_rollback(deps: Deps, request_id: str) -> tuple[dict[str, Any], int]:
    if not REQUEST_ID_RE.fullmatch(request_id):
        result = _finish(
            deps,
            code=CODE_BAD_REQUEST,
            message="bad_request_id",
            request_id=None,
            persist=False,
        )
        return result, EXIT_CODES[CODE_BAD_REQUEST]

    backups_dir = deps.state_dir / "backups"
    backup = find_backup(backups_dir, request_id)
    if backup is None:
        result = _finish(
            deps,
            code=CODE_BAD_REQUEST,
            message="backup_not_found",
            request_id=request_id,
            persist=False,
        )
        return result, EXIT_CODES[CODE_BAD_REQUEST]

    try:
        project_key = backup["project_key"]
        stack = backup["stack"]
        key = backup["key"]
        quote_style = backup["quote_style"]
        old_raw = backup["old_raw"]
        new_raw = backup["new_raw"]
        pinned_tag = backup["pinned_image_tag"]
        services = tuple(backup["services"])
        containers = tuple(tuple(pair) for pair in backup["containers"])
    except (KeyError, TypeError):
        result = _finish(
            deps,
            code=CODE_BAD_REQUEST,
            message="backup_invalid",
            request_id=request_id,
            persist=False,
        )
        return result, EXIT_CODES[CODE_BAD_REQUEST]

    env_path = deps.env_file_root / f"{stack}.env"
    log_path = deps.log_dir / f"compose-rollback-{request_id}.log"

    def fail(exc: HelperError) -> tuple[dict[str, Any], int]:
        result = _finish(
            deps,
            code=exc.code,
            message=exc.message,
            request_id=request_id,
            project_key=project_key,
            key=key,
            old_raw=old_raw,
            new_raw=new_raw,
            image_tag=pinned_tag,
        )
        return result, EXIT_CODES[exc.code]

    try:
        with locked_env_file_nonblocking(
            env_path,
            timeout_s=LOCK_TIMEOUT_S,
            retry_interval_s=LOCK_RETRY_INTERVAL_S,
            sleep=deps.sleep,
        ):
            try:
                parsed = read_env_file(env_path, key=key)
            except HelperError as exc:
                return fail(exc)

            if parsed.target.raw_value != new_raw or parsed.target.quote_style != quote_style:
                return fail(_refused("line_changed"))

            write_env_line(env_path, parsed, key=key, new_value=old_raw)

            compose_ok = run_compose(
                stack, services, image_tag=pinned_tag, runner=deps.runner, log_path=log_path
            )
            failure = None if compose_ok else "rollback_compose_failed"
            if compose_ok:
                failure = verify_containers(
                    containers,
                    key=key,
                    expected_value=old_raw,
                    pinned_tag=pinned_tag,
                    runner=deps.runner,
                    sleep=deps.sleep,
                )
                if failure is not None:
                    failure = "rollback_verify_failed"

            if failure is None:
                result = _finish(
                    deps,
                    code=CODE_APPLIED,
                    message="ok",
                    request_id=request_id,
                    project_key=project_key,
                    key=key,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    restarted=True,
                    rolled_back=True,
                    image_tag=pinned_tag,
                )
                return result, 0

            result = _finish(
                deps,
                code=CODE_FAILED_ROLLBACK_FAILED,
                message=failure,
                request_id=request_id,
                project_key=project_key,
                key=key,
                old_raw=old_raw,
                new_raw=new_raw,
                restarted=True,
                rolled_back=False,
                image_tag=pinned_tag,
            )
            return result, EXIT_CODES[CODE_FAILED_ROLLBACK_FAILED]
    except HelperError as exc:
        result = _finish(
            deps, code=exc.code, message=exc.message, request_id=request_id, persist=False
        )
        return result, EXIT_CODES[exc.code]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str], *, deps: Deps | None = None) -> int:
    deps = deps if deps is not None else Deps()
    if len(argv) < 2 or argv[1] not in ("apply", "rollback"):
        print(json.dumps({"reason": "usage: env_apply.py <apply|rollback --request-id UUID>"}))
        return 2
    if argv[1] == "apply":
        if len(argv) != 2:
            print(json.dumps({"reason": "apply takes no arguments"}))
            return 2
        _result, exit_code = cmd_apply(deps)
        return exit_code

    # rollback
    request_id = None
    rest = argv[2:]
    index = 0
    while index < len(rest):
        if rest[index] == "--request-id" and index + 1 < len(rest):
            request_id = rest[index + 1]
            index += 2
        else:
            print(json.dumps({"reason": "usage: env_apply.py rollback --request-id UUID"}))
            return 2
    if request_id is None:
        print(json.dumps({"reason": "usage: env_apply.py rollback --request-id UUID"}))
        return 2
    _result, exit_code = cmd_rollback(deps, request_id)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
