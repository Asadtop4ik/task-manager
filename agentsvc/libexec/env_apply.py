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
verification wait, the readiness prober, the syslog call, the wall clock)
are plain keyword parameters on ``main``/``cmd_apply``/``cmd_rollback``,
following ``image_state.py``'s ``runner=subprocess.run`` shape.

Never prints, logs, or persists any config VALUE anywhere except the
root-only (0700) backup (a JSON file plus a full copy of the env file as it
stood before the change) -- the one place a value legitimately persists in
cleartext, exactly as sensitive as the env file itself, and never read by
agent-svc. Stdout, the result file (0640 root:agent-svc), and the audit log
carry only sha256 hashes of values and `message`/`rollback_message` fields
drawn from the fixed vocabulary in `MESSAGES` below -- never request data
interpolated in.

Safety model for a failure ANYWHERE after the env file has been rewritten
(compose failing to even start, an unexpected exception, a concurrent hand
edit racing the lock): every such path goes through `_rollback_and_finish`,
which (1) never silently swallows the event -- it always ends in exactly one
accurately classified, persisted result; (2) never overwrites a line that
does not exactly equal what THIS request wrote (value, quote style, and line
ending) -- a concurrent edit is left alone rather than clobbered; and
(3) never restarts a stack against a stale, backup-recorded image tag -- the
running tag is re-inspected fresh, inside the lock, immediately before every
`docker compose` invocation, forward or rollback.
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
Clock = Callable[[], float]

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
RATE_LIMIT_WINDOW_S = 3600.0
RATE_LIMIT_MAX_PER_HOUR = 5

RUN_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
REQUEST_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
PROJECT_KEY_RE = re.compile(r"[a-z0-9-]{1,40}")
REQUEST_HASH_RE = re.compile(r"[0-9a-f]{64}")
# `agent_ops_policy.apply_op` calls `int(value)` for json/csv int-list ops --
# this is checked BEFORE that call so a misconfigured allowlist (an
# over-permissive `item_re`) can never reach an uncaught ValueError. Mirrors
# `agent_ops_policy._CSV_INT_RE` exactly (not imported: that module is only
# ever loaded by path at runtime, see `load_trusted_modules`).
_STRICT_INT_RE = re.compile(r"-?[0-9]+")
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
# Result/exit code vocabulary (spec section 4). `message`/`rollback_message`
# are always drawn from `MESSAGES` (or an `agent_ops_policy.PolicyError`/
# `validate_request` reason code, itself also a short fixed token) -- never
# request data.
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

# Every `message`/`rollback_message` this helper can ever emit. A fixed,
# closed vocabulary -- `_finish` asserts against it so a future edit can
# never accidentally start interpolating request data into what is
# otherwise just a token.
MESSAGES = frozenset(
    {
        "ok",
        # bad_request (2): the input file itself cannot be trusted enough to
        # even know a request_id.
        "symlink",
        "not_regular_file",
        "wrong_owner",
        "group_or_world_writable",
        "ops_dir_symlink",
        "ops_dir_writable",
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
        "backup_not_found",
        "backup_invalid",
        # refused (3): policy/hash/project -- nothing touched.
        "hash_mismatch",
        "cannot_load_trusted_modules",
        "cannot_load_allowlist",
        "stack_denied",
        "rate_limited",
        "needs_operator",
        "line_changed",
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
        "services_containers_mismatch",
        "bad_list_item_int",
        "env_missing",
        "env_symlink",
        "env_not_regular_file",
        "env_hardlinked",
        "env_wrong_owner",
        "env_too_large",
        "env_short_read",
        "env_not_utf8",
        "env_nul_byte",
        "env_bad_line_ending",
        "env_control_char_in_value",
        "unsupported_old_value",
        "key_missing_line",
        "duplicate_key_line",
        "case_variant_key_line",
        "export_key_line",
        "indented_key_line",
        "malformed_key_line",
        "unsupported_quoting",
        "env_write_failed",
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
        "unexpected_error",
        "rollback_compose_failed",
        "rollback_verify_failed",
        "reparse_failed",
        "write_failed",
        "lock_symlink",
        # busy (7):
        "lock_timeout",
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


class _ForwardFailure(Exception):
    """Internal control-flow only: a classified (not exceptional) forward
    failure, raised so the exact same `except BaseException` handler around
    the write-through-verify sequence in `cmd_apply` also catches a genuinely
    unexpected exception -- both end up going through the same rollback
    path, never propagating past it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _WriteVerifyError(Exception):
    """Internal: the read-back verification after writing the temp file did
    not match what was intended -- `write_env_line` refuses to replace the
    real file when this happens."""


def _bad_request(message: str) -> HelperError:
    return HelperError(CODE_BAD_REQUEST, message)


def _refused(message: str) -> HelperError:
    return HelperError(CODE_REFUSED, message)


def _precondition(message: str) -> HelperError:
    return HelperError(CODE_PRECONDITION, message)


def _busy(message: str) -> HelperError:
    return HelperError(CODE_BUSY, message)


def _write_all(fd: int, data: bytes) -> None:
    """`os.write` may perform a short write even for a regular file (POSIX
    permits it); loop until every byte is written rather than assuming one
    call suffices."""
    view = memoryview(data)
    written = 0
    total = len(view)
    while written < total:
        written += os.write(fd, view[written:])


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
# Request file: O_NOFOLLOW (+O_NONBLOCK, so a FIFO planted at this path can
# never hang the open() call indefinitely -- it is still refused right after
# as not-a-regular-file), regular, owned by agent-svc's uid, not
# group/world-writable, <=8 KB, exact field set. The containing directory
# (`/run/agent-svc/ops`) is checked too: not a symlink, not group/world-
# writable.
# --------------------------------------------------------------------------


def resolve_agent_svc_uid() -> int:
    override = _test_override("AGENT_SVC_UID")
    if override is not None:
        return int(override)
    return pwd.getpwnam("agent-svc").pw_uid


def _resolve_deploy_uid() -> int | None:
    """The `deploy` service user's uid, or `None` if that user does not
    exist on this host. Looked up by name (never a hardcoded uid, e.g.
    1000 -- whatever a given host's `useradd`/image build actually
    assigned) each time `Deps`'s default is built; a missing `deploy` user
    is not an error here (unlike `resolve_agent_svc_uid`, whose identity is
    this very process's own) -- it just means `env_owner_uids` falls back
    to root-only."""
    override = _test_override("DEPLOY_UID")
    if override is not None:
        return int(override) if override else None
    try:
        return pwd.getpwnam("deploy").pw_uid
    except KeyError:
        return None


def _default_env_owner_uids() -> frozenset[int]:
    """Production env files are owned by root (uid 0, the historical
    default) or by the `deploy` service user, mode 600 (spec: docs/
    AGENT_SVC.md's rollout notes) -- accept both. If `deploy` does not
    exist on this host, accept only uid 0: a lookup failure must never
    silently widen the accepted owner set."""
    deploy_uid = _resolve_deploy_uid()
    return frozenset({0}) if deploy_uid is None else frozenset({0, deploy_uid})


def _check_ops_dir(path: Path) -> None:
    try:
        st = os.lstat(path.parent)
    except OSError as exc:
        raise _bad_request("not_regular_file") from exc
    if stat.S_ISLNK(st.st_mode):
        raise _bad_request("ops_dir_symlink")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _bad_request("ops_dir_writable")


def read_request_file(path: Path, *, expected_uid: int) -> dict[str, Any]:
    """Read, ownership-check and structurally validate the request file.

    Raises `HelperError(CODE_BAD_REQUEST, ...)` for anything that means the
    file cannot be trusted at all -- including a wrong owner, which is
    checked from the SAME open fd's fstat (never a separate lstat/stat call
    that a rename could race against).
    """
    _check_ops_dir(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _bad_request("group_or_world_writable")
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
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _precondition("lock_symlink") from exc
        raise _precondition("env_missing") from exc
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
# Docker inspect. Never raises: any subprocess-level problem (missing
# binary, timeout, permission) is reported the same way as "no such
# container" -- callers treat `None` as "cannot confirm this container's
# state" and refuse/fail accordingly rather than propagating a traceback.
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
    except (OSError, subprocess.SubprocessError):
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

    Called MULTIPLE times per request -- once before even trying to acquire
    the env-file lock (fail fast), and again fresh, inside the lock,
    immediately before every `docker compose` invocation (forward and
    rollback) -- so the tag actually used is always the one observed right
    before it is used, never a value sampled earlier or read back from a
    backup file (a deploy can happen at any point agent-ops-apply is not
    itself holding the lock).
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


def _values_semantically_equal(fmt: str, a: str, b: str) -> bool:
    """Compare two raw env values for the SAME meaning, not the same bytes:
    `[1,2]` and `[1, 2]` (or `1,2` and `1, 2` for csv) both parse to the
    identical int list. Falls back to exact string equality (and for
    "scalar", stays exact-string-only -- there is no canonical form to
    parse toward)."""
    if a == b:
        return True
    if fmt == "json_int_list":
        try:
            parsed_a = json.loads(a)
            parsed_b = json.loads(b)
        except json.JSONDecodeError:
            return False
        return parsed_a == parsed_b
    if fmt == "csv_int_list":
        try:
            return _csv_int_list(a) == _csv_int_list(b)
        except ValueError:
            return False
    return False


def _csv_int_list(raw: str) -> list[int]:
    stripped = raw.strip()
    if not stripped:
        return []
    return [int(token.strip()) for token in stripped.split(",")]


def _container_env_has(raw: dict[str, Any], key: str, fmt: str, expected_value: str) -> bool:
    config = raw.get("Config")
    env_list = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(env_list, list):
        return False
    prefix = f"{key}="
    for entry in env_list:
        if isinstance(entry, str) and entry.startswith(prefix):
            return _values_semantically_equal(fmt, entry[len(prefix) :], expected_value)
    return False


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


# Detects any dotenv-ish key declaration at the start of a (already
# whitespace-stripped) line: optional `export`, an identifier, then `=` or
# `:`, with arbitrary spacing around the separator -- deliberately MORE
# permissive than the one exact canonical form this helper ever accepts, so
# it can be used to find every OTHER line a real dotenv/pydantic-settings
# parser might also read as declaring the same (case-insensitive) key.
_KEY_LINE_RE = re.compile(r"^(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]")


def _line_key_match(line_text: str) -> re.Match[str] | None:
    return _KEY_LINE_RE.match(line_text.strip())


def _classify_noncanonical_line(line_text: str, key: str) -> str:
    """`line_text` (the RAW line, with its terminator, NOT pre-stripped) is
    known to plausibly declare `key` but is not the one exact canonical form
    (`KEY=...` at column 0, exact case, `=` immediately after the key, no
    `export`). Returns the single MESSAGES token best describing why."""
    if line_text != line_text.lstrip():
        return "indented_key_line"
    match = _line_key_match(line_text)
    assert match is not None
    if match.group("export"):
        return "export_key_line"
    if match.group("key") != key:
        return "case_variant_key_line"
    return "malformed_key_line"  # exact case, no export/indent -- `:` sep or spacing


def _quote_and_extract(rest: str) -> tuple[str, str] | None:
    """`rest` is the line's text after `KEY=`, with any trailing newline
    already stripped. Returns (quote_style, raw_value) or None if the
    quoting is unsupported (refused as a precondition by the caller)."""
    text = rest
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'" and "'" not in text[1:-1]:
        return "single", text[1:-1]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"' and '"' not in text[1:-1]:
        if "\\" in text[1:-1]:
            return None
        return "double", text[1:-1]
    if any(ch in text for ch in ("'", '"', "\\")):
        return None
    return "none", text


def read_env_file(
    path: Path, *, key: str, allowed_owner_uids: frozenset[int] = frozenset({0})
) -> ParsedEnvFile:
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
        # A hard-linked env file could be edited "atomically" through the
        # other link name without our rename-based replace ever touching
        # it, or vice versa -- refuse rather than risk operating on
        # something with more than one path to it.
        if st.st_nlink != 1:
            raise _precondition("env_hardlinked")
        if st.st_uid not in allowed_owner_uids:
            raise _precondition("env_wrong_owner")
        if st.st_size > MAX_ENV_FILE_BYTES:
            raise _precondition("env_too_large")
        content = os.read(fd, MAX_ENV_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(content) > MAX_ENV_FILE_BYTES:
        raise _precondition("env_too_large")
    if len(content) != st.st_size:
        raise _precondition("env_short_read")

    if b"\x00" in content:
        raise _precondition("env_nul_byte")
    # A lone `\r` (not part of `\r\n`) is a genuine hazard, not just an
    # oddity: `bytes.splitlines(keepends=True)` treats a bare `\r` as its
    # OWN line boundary, so the byte AFTER it silently becomes the start of
    # the "next line" with no separator preserved -- rewriting the target
    # line then glues it directly onto whatever followed (verified: this
    # merged a `BOT_TOKEN=...` line into the rewritten value and then
    # destroyed it on rollback). Refuse outright instead.
    if b"\r" in content.replace(b"\r\n", b""):
        raise _precondition("env_bad_line_ending")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _precondition("env_not_utf8") from exc

    lines = content.splitlines(keepends=True)
    key_bytes = key.encode("ascii")
    canonical_prefix = key_bytes + b"="

    canonical_indices: list[int] = []
    plausible_indices: list[int] = []
    for index, raw_line in enumerate(lines):
        if raw_line.startswith(canonical_prefix):
            canonical_indices.append(index)
            plausible_indices.append(index)
            continue
        text = raw_line.decode("utf-8")  # whole file already proved decodable above
        match = _line_key_match(text)
        if match is not None and match.group("key").casefold() == key.casefold():
            plausible_indices.append(index)

    if not plausible_indices:
        raise _precondition("key_missing_line")
    if not canonical_indices:
        defect_line = lines[plausible_indices[0]].decode("utf-8")
        raise _precondition(_classify_noncanonical_line(defect_line, key))
    if len(plausible_indices) > 1:
        raise _precondition("duplicate_key_line")

    target_index = canonical_indices[0]
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
    rest = body[len(key_bytes) + 1 :].decode("utf-8")
    parsed = _quote_and_extract(rest)
    if parsed is None:
        raise _precondition("unsupported_quoting")
    quote_style, raw_value = parsed
    if not (1 <= len(raw_value) <= 256):
        raise _precondition("unsupported_quoting")
    if any(ord(ch) < 0x20 for ch in raw_value):
        raise _precondition("env_control_char_in_value")
    if "$" in raw_value or " #" in raw_value or raw_value != raw_value.rstrip():
        raise _precondition("unsupported_old_value")
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


def _render_new_content(parsed: ParsedEnvFile, *, key: str, new_value: str) -> bytes:
    new_line = _render_line(key, new_value, parsed.target.quote_style, parsed.target.ending)
    new_lines = list(parsed.lines)
    new_lines[parsed.target_index] = new_line
    return b"".join(new_lines)


def write_env_line(
    path: Path,
    parsed: ParsedEnvFile,
    *,
    key: str,
    new_value: str,
) -> None:
    """Atomically replace exactly the target line, preserving every other
    line byte-for-byte and the original file's uid/gid/mode.

    All-or-nothing: any exception here (`OSError`, `_WriteVerifyError`)
    means `path` is GUARANTEED unchanged -- the temp file is written,
    fsynced, read back and sha256-compared BEFORE the atomic `os.replace`,
    and `os.replace` is the only step that can affect `path` at all.
    """
    new_content = _render_new_content(parsed, key=key, new_value=new_value)
    expected_sha256 = hashlib.sha256(new_content).hexdigest()

    directory = path.parent
    tmp_path = directory / f".{path.name}.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, new_content)
        os.fchmod(fd, stat.S_IMODE(parsed.st.st_mode))
        # Ownership must end up matching the original exactly. A failure
        # here aborts (no `contextlib.suppress`): silently replacing the
        # file with the WRONG owner/group would be worse than refusing.
        os.fchown(fd, parsed.st.st_uid, parsed.st.st_gid)
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        readback = bytearray()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            readback += chunk
        if hashlib.sha256(bytes(readback)).hexdigest() != expected_sha256:
            raise _WriteVerifyError("temp file content did not verify after write")
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
        "--no-build",
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
    """Never raises: a missing docker binary, a subprocess timeout, or a
    problem writing the log file are all reported as a plain `False`
    (compose failed) so the caller's rollback logic always runs rather than
    an uncaught exception skipping it."""
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
    except (OSError, subprocess.SubprocessError) as exc:
        ok = False
        output = f"[env_apply] failed to run compose: {type(exc).__name__}\n"
    with contextlib.suppress(OSError):
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _write_all(fd, output.encode("utf-8", "replace"))
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
    fmt: str,
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
            state = raw.get("State")
            if not isinstance(state, dict) or not state.get("Running"):
                # A container that exited during the stability window
                # (restart policy "no") must never be reported as verified
                # just because its RestartCount happens not to have moved.
                return "container_not_running_after_restart"
            if raw.get("RestartCount") != before[name]:
                return "restart_count_unstable"
            states[name] = raw  # refresh: the image/env check below must
            # see the post-window state, not the pre-sleep snapshot.

    for name, repo in containers:
        raw = states[name]
        config = raw.get("Config")
        image = config.get("Image") if isinstance(config, dict) else None
        if image != f"{repo}:{pinned_tag}":
            return "image_mismatch"
        if not _container_env_has(raw, key, fmt, expected_value):
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
        _write_all(fd, (line + "\n").encode("utf-8"))
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


def write_result_file(results_dir: Path, file_stem: str, result: dict[str, Any]) -> None:
    path = results_dir / f"{file_stem}.json"
    tmp_path = results_dir / f".{file_stem}.json.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, json.dumps(result, sort_keys=True).encode("utf-8"))
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
# this is the one place a real value legitimately persists in cleartext --
# both as JSON metadata AND a full copy of the env file as it stood
# immediately before this request's change, so hand recovery never depends
# on this module's own line-splice logic being correct.
# --------------------------------------------------------------------------


def write_backup(
    backups_dir: Path,
    project_key: str,
    request_id: str,
    payload: dict[str, Any],
    *,
    original_content: bytes,
) -> None:
    project_dir = backups_dir / project_key
    project_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    env_copy_path = project_dir / f"{request_id}.env.bak"
    tmp_env_copy = project_dir / f".{request_id}.env.bak.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_env_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, original_content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_env_copy, env_copy_path)

    path = project_dir / f"{request_id}.json"
    tmp_path = project_dir / f".{request_id}.json.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, json.dumps(payload, sort_keys=True).encode("utf-8"))
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
# Rate limit: <=N applies per project per rolling hour. Best-effort/fail
# open -- a corrupt or unwritable counter file must never block a
# legitimate, already owner-approved apply; it only ever adds friction
# against a compromised/buggy agent-svc hammering the same project.
# --------------------------------------------------------------------------


def check_and_record_rate_limit(
    state_dir: Path,
    project_key: str,
    *,
    now: float,
    max_per_hour: int = RATE_LIMIT_MAX_PER_HOUR,
) -> bool:
    """Returns True (and records this attempt) if under budget; False (and
    does NOT record) if the project's rolling-hour budget is already spent."""
    path = state_dir / "ratelimit.json"
    data: dict[str, list[float]] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            for project, timestamps in loaded.items():
                if isinstance(project, str) and isinstance(timestamps, list):
                    data[project] = [t for t in timestamps if isinstance(t, (int, float))]
    except (OSError, json.JSONDecodeError):
        data = {}

    window_start = now - RATE_LIMIT_WINDOW_S
    recent = [t for t in data.get(project_key, []) if t > window_start]
    if len(recent) >= max_per_hour:
        return False
    recent.append(now)
    data[project_key] = recent
    # Prune every project's stale entries opportunistically so the file
    # never grows without bound.
    data = {k: [t for t in v if t > window_start] for k, v in data.items() if v}

    tmp_path = state_dir / f".ratelimit.json.tmp-{uuid.uuid4().hex}"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, json.dumps(data).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
    return True


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
    now: Clock = time.time
    agent_svc_uid: int | None = None
    env_owner_uids: frozenset[int] = field(default_factory=_default_env_owner_uids)
    rate_limit_max_per_hour: int = RATE_LIMIT_MAX_PER_HOUR


def _finish(
    deps: Deps,
    *,
    code: str,
    message: str,
    request_id: str | None,
    result_key: str | None = None,
    run_id: str | None = None,
    project_key: str | None = None,
    key: str | None = None,
    op: str | None = None,
    value: str | None = None,
    old_raw: str | None = None,
    new_raw: str | None = None,
    restarted: bool = False,
    rolled_back: bool = False,
    env_restored: bool = True,
    rollback_message: str | None = None,
    image_tag: str | None = None,
    request_hash: str | None = None,
    started_at: float | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    assert message in MESSAGES, f"message {message!r} is not in the fixed vocabulary"
    assert (
        rollback_message is None or rollback_message in MESSAGES
    ), f"rollback_message {rollback_message!r} is not in the fixed vocabulary"
    exit_code = EXIT_CODES[code]
    result = {
        "v": 1,
        "request_id": request_id,
        "request_hash": request_hash,
        "code": code,
        "exit": exit_code,
        "message": message,
        "rollback_message": rollback_message,
        "rolled_back": rolled_back,
        "restarted": restarted,
        "env_restored": env_restored,
        "image_tag": image_tag,
    }
    if persist and request_id is not None:
        results_dir = deps.state_dir / "results"
        file_stem = result_key if result_key is not None else request_id
        with contextlib.suppress(OSError):
            write_result_file(results_dir, file_stem, result)
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
            "env_restored": env_restored,
            "rollback_message": rollback_message,
            "image_tag": image_tag,
            "duration_ms": duration_ms,
        }
        with contextlib.suppress(OSError):
            append_audit_log(deps.log_dir, entry, logger_runner=deps.logger_runner)
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def _attempt_forward(
    deps: Deps,
    *,
    project: Any,
    key: str,
    fmt: str,
    new_raw: str,
    pinned_tag: str,
    log_path: Path,
) -> str | None:
    """Restart the stack and verify it. Never raises (every dependency it
    calls -- `run_compose`, `verify_containers`, `probe_ready_url` -- is
    itself exception-safe); returns None on success or a MESSAGES token."""
    compose_ok = run_compose(
        project.stack,
        project.services,
        image_tag=pinned_tag,
        runner=deps.runner,
        log_path=log_path,
    )
    if not compose_ok:
        return "compose_failed"
    failure = verify_containers(
        project.containers,
        key=key,
        fmt=fmt,
        expected_value=new_raw,
        pinned_tag=pinned_tag,
        runner=deps.runner,
        sleep=deps.sleep,
    )
    if failure is not None:
        return failure
    if project.ready_url is not None and not probe_ready_url(
        project.ready_url, probe=deps.probe, sleep=deps.sleep
    ):
        return "ready_probe_failed"
    return None


def _rollback_and_finish(
    deps: Deps,
    *,
    project: Any,
    key: str,
    op: str,
    value: str,
    old_raw: str,
    new_raw: str,
    quote_style: str,
    ending: bytes,
    env_path: Path,
    log_path: Path,
    forward_message: str,
    request_id: str,
    run_id: str,
    project_key: str,
    started_at: float,
    request_hash: str,
) -> tuple[dict[str, Any], int]:
    """The env file already holds `new_raw` on disk when this is called
    (from either a classified forward failure or a genuinely unexpected
    exception -- see `cmd_apply`). Restores `old_raw` ONLY if the file
    still holds exactly what this request wrote; re-inspects the running
    image tag fresh (never the backup's, which can be stale by the time a
    rollback runs); always ends in exactly one accurately classified,
    persisted result -- this function itself never raises past its own
    boundary, even on a bug inside it."""

    def done(
        *,
        code: str,
        rollback_message: str | None,
        env_restored: bool,
        rolled_back: bool,
        image_tag: str | None,
    ) -> tuple[dict[str, Any], int]:
        result = _finish(
            deps,
            code=code,
            message=forward_message,
            request_id=request_id,
            run_id=run_id,
            project_key=project_key,
            key=key,
            op=op,
            value=value,
            old_raw=old_raw,
            new_raw=new_raw,
            restarted=True,
            rolled_back=rolled_back,
            env_restored=env_restored,
            rollback_message=rollback_message,
            image_tag=image_tag,
            request_hash=request_hash,
            started_at=started_at,
        )
        return result, EXIT_CODES[code]

    try:
        try:
            reparsed = read_env_file(env_path, key=key, allowed_owner_uids=deps.env_owner_uids)
        except (HelperError, OSError):
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="reparse_failed",
                env_restored=False,
                rolled_back=False,
                image_tag=None,
            )

        if (
            reparsed.target.raw_value != new_raw
            or reparsed.target.quote_style != quote_style
            or reparsed.target.ending != ending
        ):
            # Someone/something else changed the line since we wrote it
            # (a concurrent hand edit racing our advisory lock, most
            # plausibly) -- never blindly stomp on that.
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="line_changed",
                env_restored=False,
                rolled_back=False,
                image_tag=None,
            )

        try:
            write_env_line(env_path, reparsed, key=key, new_value=old_raw)
        except (OSError, _WriteVerifyError):
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="write_failed",
                env_restored=False,
                rolled_back=False,
                image_tag=None,
            )

        # The file holds old_raw again regardless of what happens below.
        try:
            _, rollback_tag = check_containers_pinned(project.containers, runner=deps.runner)
        except HelperError:
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="rollback_compose_failed",
                env_restored=True,
                rolled_back=False,
                image_tag=None,
            )

        rollback_compose_ok = run_compose(
            project.stack,
            project.services,
            image_tag=rollback_tag,
            runner=deps.runner,
            log_path=log_path,
        )
        if not rollback_compose_ok:
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="rollback_compose_failed",
                env_restored=True,
                rolled_back=False,
                image_tag=rollback_tag,
            )

        rollback_failure = verify_containers(
            project.containers,
            key=key,
            fmt=project.keys[key].format,
            expected_value=old_raw,
            pinned_tag=rollback_tag,
            runner=deps.runner,
            sleep=deps.sleep,
        )
        if (
            rollback_failure is None
            and project.ready_url is not None
            and not probe_ready_url(project.ready_url, probe=deps.probe, sleep=deps.sleep)
        ):
            rollback_failure = "ready_probe_failed"

        if rollback_failure is not None:
            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                rollback_message="rollback_verify_failed",
                env_restored=True,
                rolled_back=False,
                image_tag=rollback_tag,
            )

        return done(
            code=CODE_FAILED_ROLLED_BACK,
            rollback_message=None,
            env_restored=True,
            rolled_back=True,
            image_tag=rollback_tag,
        )
    except Exception:
        # Final backstop: whatever this was, the safest accurate statement
        # is "we do not know the env file's current state for certain" --
        # never let a bug here escape as a bare traceback (which could, in
        # the worst case, embed env text in a local variable repr).
        return done(
            code=CODE_FAILED_ROLLBACK_FAILED,
            rollback_message="write_failed",
            env_restored=False,
            rolled_back=False,
            image_tag=None,
        )


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

    # Defense in depth beyond `agent_ops_policy`'s own HARD_DENIED_PROJECTS
    # check (keyed on `project_key`): refuse independently if the STACK
    # itself names a hard-denied project, so a misconfigured allowlist entry
    # filed under some OTHER project_key can never end up pointed at
    # /srv/stack/env/task-manager.env (or agent-qa's) regardless.
    if project.stack in policy.HARD_DENIED_PROJECTS:
        return fail(_refused("stack_denied"))

    # Every compose service this request would recreate must correspond to
    # a container this helper actually verifies afterward -- otherwise a
    # service could be restarted with nobody ever checking what it ended up
    # running.
    container_names = {name for name, _repo in project.containers}
    if not set(project.services) <= container_names:
        return fail(_precondition("services_containers_mismatch"))

    recomputed_hash = policy.request_hash(
        run_id=run_id, project_key=project_key, kind=kind, key=key, op=op, value=value
    )

    # Idempotency cache: re-emit ONLY a terminal applied/already_applied
    # result recorded for THIS exact effect (hash-bound) -- a replayed or
    # forged request that reuses a request_id with a different value/hash
    # must never short-circuit into someone else's cached success. A
    # request_id whose last attempt ended failed_rollback_failed is never
    # retried automatically at all, regardless of hash: it needs an
    # operator's `rollback` CLI run first.
    results_dir = deps.state_dir / "results"
    existing = read_existing_result(results_dir, request_id)
    if existing is not None:
        if (
            existing.get("code")
            in (
                CODE_APPLIED,
                CODE_ALREADY_APPLIED,
            )
            and existing.get("request_hash") == recomputed_hash
        ):
            print(json.dumps(existing, sort_keys=True), flush=True)
            return existing, existing.get("exit", 0)
        if existing.get("code") == CODE_FAILED_ROLLBACK_FAILED:
            # Deliberately `persist=False`: writing a fresh "refused" result
            # under the SAME request_id would overwrite (destroy) the
            # original failed_rollback_failed record this whole gate exists
            # to protect -- exactly the class of bug fixed for the
            # rollback CLI's own result file (see cmd_rollback's
            # `result_key`). Nothing changed, so there is nothing new to
            # persist; the original result is the one that matters.
            result = _finish(
                deps,
                code=CODE_REFUSED,
                message="needs_operator",
                request_id=request_id,
                run_id=run_id,
                project_key=project_key,
                key=key,
                op=op,
                value=value,
                started_at=started_at,
                persist=False,
            )
            return result, EXIT_CODES[CODE_REFUSED]

    if recomputed_hash != claimed_hash:
        return fail(_refused("hash_mismatch"))

    key_policy = project.keys[key]

    # A misconfigured allowlist (an over-permissive `item_re` on a
    # json/csv_int_list key) must never reach `int(value)` inside
    # `agent_ops_policy.apply_op` with something that cannot parse.
    if key_policy.format in ("json_int_list", "csv_int_list") and not _STRICT_INT_RE.fullmatch(
        value
    ):
        return fail(_precondition("bad_list_item_int"))

    try:
        check_containers_pinned(project.containers, runner=deps.runner)
    except HelperError as exc:
        return fail(exc, image_tag=None)

    if not check_and_record_rate_limit(
        deps.state_dir, project_key, now=deps.now(), max_per_hour=deps.rate_limit_max_per_hour
    ):
        return fail(_refused("rate_limited"), image_tag=None)

    env_path = deps.env_file_root / f"{project.stack}.env"
    log_path = deps.log_dir / f"compose-{request_id}.log"

    try:
        with locked_env_file_nonblocking(
            env_path,
            timeout_s=LOCK_TIMEOUT_S,
            retry_interval_s=LOCK_RETRY_INTERVAL_S,
            sleep=deps.sleep,
        ):
            # Re-inspect INSIDE the lock, immediately before touching
            # anything: the pre-lock check above only gates whether it was
            # worth acquiring the lock at all. A deploy could have landed
            # in between; THIS tag is the one actually used below.
            try:
                container_states, pinned_tag = check_containers_pinned(
                    project.containers, runner=deps.runner
                )
            except HelperError as exc:
                return fail(exc, image_tag=None)

            try:
                parsed = read_env_file(
                    env_path, key=key, allowed_owner_uids=deps.env_owner_uids
                )
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
                _container_env_has(container_states[name], key, key_policy.format, new_raw)
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
                    env_restored=True,
                    image_tag=pinned_tag,
                    request_hash=recomputed_hash,
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
                "key_format": key_policy.format,
                "quote_style": parsed.target.quote_style,
                "old_raw": old_raw,
                "new_raw": new_raw,
                "pinned_image_tag": pinned_tag,
                "services": list(project.services),
                "containers": [list(pair) for pair in project.containers],
                "ready_url": project.ready_url,
                "file_sha256_before": hashlib.sha256(parsed.content).hexdigest(),
                "file_sha256_after": hashlib.sha256(
                    _render_new_content(parsed, key=key, new_value=new_raw)
                ).hexdigest(),
                "ts": datetime.now(UTC).isoformat(),
            }

            try:
                write_backup(
                    deps.state_dir / "backups",
                    project_key,
                    request_id,
                    backup_payload,
                    original_content=parsed.content,
                )
                write_env_line(env_path, parsed, key=key, new_value=new_raw)
            except (OSError, _WriteVerifyError):
                # All-or-nothing: the ORIGINAL env file is guaranteed
                # untouched (see write_env_line's own docstring).
                return fail(
                    _precondition("env_write_failed"), old_raw=old_raw, image_tag=pinned_tag
                )

            # From here on the env file HAS new_raw on disk. EVERY path
            # below -- an expected classified failure (`_ForwardFailure`)
            # or a genuinely unexpected exception -- goes through the same
            # `_rollback_and_finish`, which always ends in exactly one
            # accurately classified, persisted result. Nothing may
            # propagate past this point uncaught.
            try:
                forward_message = _attempt_forward(
                    deps,
                    project=project,
                    key=key,
                    fmt=key_policy.format,
                    new_raw=new_raw,
                    pinned_tag=pinned_tag,
                    log_path=log_path,
                )
                if forward_message is None:
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
                        env_restored=True,
                        image_tag=pinned_tag,
                        request_hash=recomputed_hash,
                        started_at=started_at,
                    )
                    return result, 0
                raise _ForwardFailure(forward_message)
            except BaseException as exc:
                if isinstance(exc, (SystemExit, KeyboardInterrupt, GeneratorExit)):
                    raise
                message = (
                    exc.message if isinstance(exc, _ForwardFailure) else "unexpected_error"
                )
                return _rollback_and_finish(
                    deps,
                    project=project,
                    key=key,
                    op=op,
                    value=value,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    quote_style=parsed.target.quote_style,
                    ending=parsed.target.ending,
                    env_path=env_path,
                    log_path=log_path,
                    forward_message=message,
                    request_id=request_id,
                    run_id=run_id,
                    project_key=project_key,
                    started_at=started_at,
                    request_hash=recomputed_hash,
                )
    except HelperError as exc:
        # Reached only by something raised during lock ACQUISITION itself
        # (busy from contention, or the lock file's own path being a
        # symlink) -- every HelperError raised once inside the lock is
        # handled locally and returns before this point. `busy` is
        # transient and must never be cached/audited (agent-svc simply
        # retries); anything else here is a real, persistable outcome.
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
            persist=exc.code != CODE_BUSY,
        )
        return result, EXIT_CODES[exc.code]


# --------------------------------------------------------------------------
# rollback CLI (operator only, never in sudoers).
# --------------------------------------------------------------------------


def cmd_rollback(deps: Deps, request_id: str) -> tuple[dict[str, Any], int]:
    started_at = time.monotonic()

    def done(
        *,
        code: str,
        message: str,
        persist: bool = True,
        **extra: Any,
    ) -> tuple[dict[str, Any], int]:
        # Deliberately NEVER writes results/<request_id>.json (that file
        # belongs to `cmd_apply`'s own outcome for this request_id, and
        # must never be silently overwritten by a later, possibly refused,
        # manual rollback attempt) -- a distinct `<request_id>.rollback.json`
        # instead.
        result = _finish(
            deps,
            code=code,
            message=message,
            request_id=request_id,
            result_key=f"{request_id}.rollback",
            persist=persist,
            started_at=started_at,
            **extra,
        )
        return result, EXIT_CODES[code]

    if not REQUEST_ID_RE.fullmatch(request_id):
        return done(code=CODE_BAD_REQUEST, message="bad_request_id", persist=False)

    backup = find_backup(deps.state_dir / "backups", request_id)
    if backup is None:
        return done(code=CODE_BAD_REQUEST, message="backup_not_found", persist=False)

    try:
        project_key = backup["project_key"]
        stack = backup["stack"]
        key = backup["key"]
        fmt = backup["key_format"]
        quote_style = backup["quote_style"]
        old_raw = backup["old_raw"]
        new_raw = backup["new_raw"]
        services = tuple(backup["services"])
        containers = tuple(tuple(pair) for pair in backup["containers"])
        ready_url = backup.get("ready_url")
        if not all(
            isinstance(v, str)
            for v in (project_key, stack, key, fmt, quote_style, old_raw, new_raw)
        ):
            raise TypeError("non-string backup field")
        if ready_url is not None and not isinstance(ready_url, str):
            raise TypeError("bad ready_url")
    except (KeyError, TypeError):
        return done(code=CODE_BAD_REQUEST, message="backup_invalid", persist=False)

    env_path = deps.env_file_root / f"{stack}.env"
    log_path = deps.log_dir / f"compose-rollback-{request_id}.log"

    try:
        with locked_env_file_nonblocking(
            env_path,
            timeout_s=LOCK_TIMEOUT_S,
            retry_interval_s=LOCK_RETRY_INTERVAL_S,
            sleep=deps.sleep,
        ):
            try:
                parsed = read_env_file(
                    env_path, key=key, allowed_owner_uids=deps.env_owner_uids
                )
            except HelperError as exc:
                return done(
                    code=exc.code, message=exc.message, project_key=project_key, key=key
                )

            already_old = (
                parsed.target.raw_value == old_raw and parsed.target.quote_style == quote_style
            )
            still_forward = (
                parsed.target.raw_value == new_raw and parsed.target.quote_style == quote_style
            )
            if not (already_old or still_forward):
                # Neither the value this request wrote NOR the value it
                # would restore -- something else changed the line since;
                # never guess which direction to reconcile it.
                return done(
                    code=CODE_REFUSED,
                    message="line_changed",
                    project_key=project_key,
                    key=key,
                    old_raw=old_raw,
                    new_raw=new_raw,
                )

            if still_forward:
                try:
                    write_env_line(env_path, parsed, key=key, new_value=old_raw)
                except (OSError, _WriteVerifyError):
                    return done(
                        code=CODE_FAILED_ROLLBACK_FAILED,
                        message="write_failed",
                        project_key=project_key,
                        key=key,
                        old_raw=old_raw,
                        new_raw=new_raw,
                        env_restored=False,
                    )
            # else: the file already holds old_raw (e.g. cmd_apply's own
            # automatic rollback already restored it, or a previous manual
            # rollback did) -- nothing to write, just make sure the running
            # containers actually reflect it.

            try:
                _, current_tag = check_containers_pinned(containers, runner=deps.runner)
            except HelperError:
                return done(
                    code=CODE_FAILED_ROLLBACK_FAILED,
                    message="rollback_compose_failed",
                    project_key=project_key,
                    key=key,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    env_restored=True,
                )

            compose_ok = run_compose(
                stack, services, image_tag=current_tag, runner=deps.runner, log_path=log_path
            )
            failure = None if compose_ok else "rollback_compose_failed"
            if compose_ok:
                failure = verify_containers(
                    containers,
                    key=key,
                    fmt=fmt,
                    expected_value=old_raw,
                    pinned_tag=current_tag,
                    runner=deps.runner,
                    sleep=deps.sleep,
                )
                if failure is not None:
                    failure = "rollback_verify_failed"
            if (
                failure is None
                and ready_url is not None
                and not probe_ready_url(ready_url, probe=deps.probe, sleep=deps.sleep)
            ):
                failure = "rollback_verify_failed"

            if failure is None:
                return done(
                    code=CODE_APPLIED,
                    message="ok",
                    project_key=project_key,
                    key=key,
                    old_raw=old_raw,
                    new_raw=new_raw,
                    restarted=True,
                    rolled_back=True,
                    env_restored=True,
                    image_tag=current_tag,
                )

            return done(
                code=CODE_FAILED_ROLLBACK_FAILED,
                message=failure,
                project_key=project_key,
                key=key,
                old_raw=old_raw,
                new_raw=new_raw,
                restarted=True,
                rolled_back=False,
                env_restored=True,
                image_tag=current_tag,
            )
    except HelperError as exc:
        # `busy` (lock contention) is never persisted/cached; a lock-file
        # symlink is a real, persistable outcome for this request_id.
        return done(
            code=exc.code,
            message=exc.message,
            project_key=project_key,
            key=key,
            persist=exc.code != CODE_BUSY,
        )


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
