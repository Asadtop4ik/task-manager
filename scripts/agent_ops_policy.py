"""Shared policy module for agent "ops requests" (owner-approved env changes).

Design: `agent-svc-notes/ops-requests-spec.md` sections 2-3, work package WP-D0.
This is the single source of truth for what an "env_set" request is allowed to
touch and how its value is applied to an env-file line; every other component
that needs those answers calls into this module rather than reimplementing
them:

* the trailer parser (`agentsvc/agent_svc/ops_requests.py`, WP-C) calls
  `validate_request` to turn Codex's proposals into `allowed`/`denied`;
* the backend (`backend/app/services/agent_ops.py`, WP-A) calls
  `request_hash` to compute the same hash stored on the row and re-checked at
  approval time;
* the root oneshot helper (`agentsvc/libexec/env_apply.py`, WP-D) calls
  `load_allowlist`, `validate_request` (to independently re-check what it was
  told, never trusting the caller) and `apply_op` (to compute the new env-file
  line) before it ever touches `/srv/stack/env/*.env`.

Import contract (spec section 4): this file is loaded two ways -- as an
ordinary module from `scripts/` (`python3 -m unittest discover -s scripts`,
or `from agent_ops_policy import ...` once `scripts/` is on `sys.path`), and
by file path from `/opt/agent-svc/trusted/agent_ops_policy.py`, the same
pattern `agentsvc/libexec/image_state.py::_load_agent_repos` uses for
`agent_repos.py`. Both loaders expect a single flat file, so this module:

* uses no relative imports and imports nothing else from this repository;
* takes the trusted project catalog as a parameter (`repositories`) rather
  than importing `backend/app/services/agent_repos.py` itself -- every
  caller here already loads that catalog its own way (by path in production,
  by `sys.path` insert in a checkout) and just hands it in. Anything with
  `.project_key` (str), `.full_name` (str) and `.images`
  (`Sequence[tuple[str, str]]`) works; `agent_repos.REPOSITORIES` satisfies
  this directly.

Nothing in this module ever prints, logs, or returns a *value* on its own
initiative -- `KeyPolicy`/`ProjectPolicy`/`Allowlist` carry only names, ops
and descriptions (safe to drop into a Codex prompt), and the one place a
value flows through (`apply_op`) hands it straight back to the caller instead
of persisting or emitting it anywhere.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol


class PolicyError(ValueError):
    """A policy violation. ``args[0]`` is a short, stable, machine-readable code."""


# --------------------------------------------------------------------------
# Regexes (spec section 2). Always used with `.fullmatch(...)`, never `.match`
# or `.search` with a `^...$`-anchored pattern: in non-MULTILINE mode `$`
# matches immediately before a trailing "\n" as well as at the true end of
# string, so `re.match(r"^\d+$", "123\n")` (or the equivalent `.match`/`$`
# combination) *passes* a value with a trailing newline appended -- exactly
# the "5339875840\n" trap this module must reject. `fullmatch` has no such
# hole: the whole string, trailing newline included, must satisfy the
# pattern. Compiled with `re.ASCII` so a lookalike non-ASCII digit or letter
# (e.g. Arabic-Indic "٥", U+0665) can never satisfy a class that was only
# ever meant to admit ASCII (this matters most for allowlist-authored
# `item_re`/`value_re` patterns that might use `\d`/`\w` shorthand instead of
# an explicit `[0-9]` range -- see `_compile_pattern` below).
# --------------------------------------------------------------------------
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}", re.ASCII)
VALUE_RE = re.compile(r"[A-Za-z0-9_.,:@/+-]{1,256}", re.ASCII)
# Substring denylist on the key name, written with leading/trailing ".*" so it
# can be used with `.fullmatch(...)` like every other pattern here (a plain
# `.search(...)` would work too, but a stray control character after the
# matched substring would then slip through unnoticed instead of failing the
# same way every other check in this module fails).
SECRET_KEY_RE = re.compile(
    r".*(?:SECRET|TOKEN|PASSW|PWD|KEY|DSN|DATABASE|REDIS|URL|URI|HOST|ENDPOINT|"
    r"WEBHOOK|PRIVATE|CREDENTIAL|AUTH|COOKIE|SALT|JWT|API).*",
    re.ASCII,
)

MAX_REQUESTS = 3
MAX_VALUE_LEN = 256
HARD_DENIED_PROJECTS = frozenset({"task-manager", "agent-qa"})
FORMATS = ("json_int_list", "csv_int_list", "scalar")
OPS = ("replace", "list_add", "list_remove")

_LIST_FORMATS = ("json_int_list", "csv_int_list")
_LIST_OPS = frozenset({"list_add", "list_remove"})

_PROJECT_KEY_RE = re.compile(r"[a-z0-9-]{1,40}", re.ASCII)
_STACK_RE = re.compile(r"[a-z0-9-]{1,40}", re.ASCII)
_SERVICE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}", re.ASCII)
_READY_URL_RE = re.compile(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}/[A-Za-z0-9_./-]*", re.ASCII)
_CSV_INT_RE = re.compile(r"-?[0-9]+", re.ASCII)

_MAX_ALLOWLIST_BYTES = 256 * 1024
_MAX_DESCRIPTION_LEN = 200
_MAX_REASON_LEN = 300

_TOP_LEVEL_FIELDS = frozenset({"version", "projects"})
_PROJECT_FIELDS = frozenset(
    {
        "repo_full_name",
        "stack",
        "env_file",
        "services",
        "containers",
        "ready_url",
        "keys",
    }
)
_KEY_COMMON_FIELDS = frozenset({"format", "ops", "description"})
_KEY_LIST_FIELDS = _KEY_COMMON_FIELDS | {"item_re", "max_items", "protected_items"}
_KEY_SCALAR_FIELDS = _KEY_COMMON_FIELDS | {"value_re"}


class RepositoryLike(Protocol):
    """The slice of `agent_repos.AgentRepository` this module actually needs."""

    project_key: str
    full_name: str
    images: tuple[tuple[str, str], ...]


# --------------------------------------------------------------------------
# Data model. Frozen and holding only prompt-safe data: key names, the ops
# allowed on them, and their human descriptions -- never a value, current or
# requested. `compose_implement_prompt` (WP-C) can serialize `keys` straight
# into the Codex prompt without a values allowlist of its own to maintain.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPolicy:
    """One allowlisted key's policy.

    ``item_re``/``max_items``/``protected_items`` are set (and ``value_re``
    is ``None``) for ``format in ("json_int_list", "csv_int_list")``;
    ``value_re`` is set (and the other three left at their defaults) for
    ``format == "scalar"``.
    """

    format: str
    ops: tuple[str, ...]
    description: str
    item_re: re.Pattern[str] | None = None
    max_items: int | None = None
    protected_items: frozenset[int] = frozenset()
    value_re: re.Pattern[str] | None = None


@dataclass(frozen=True)
class ProjectPolicy:
    project_key: str
    repo_full_name: str
    stack: str
    env_file: str
    services: tuple[str, ...]
    containers: tuple[tuple[str, str], ...]
    ready_url: str | None
    keys: Mapping[str, KeyPolicy]


@dataclass(frozen=True)
class Allowlist:
    version: int
    projects: Mapping[str, ProjectPolicy]


# --------------------------------------------------------------------------
# Loading / parsing.
# --------------------------------------------------------------------------

_OPEN_ERROR_CODES = {
    errno.ELOOP: "symlink",
    errno.ENOENT: "not_found",
    errno.EACCES: "not_accessible",
    errno.EPERM: "not_accessible",
}


def _open_error_code(exc: OSError) -> str:
    if exc.errno is None:
        return "open_failed"
    return _OPEN_ERROR_CODES.get(exc.errno, "open_failed")


def load_allowlist(
    path: str | Path,
    repositories: Iterable[RepositoryLike],
    *,
    require_root_owned: bool = True,
) -> Allowlist:
    """Read, permission-check, and parse the root-owned ops-allowlist file.

    Refuses (``PolicyError``) a path whose final component is a symlink
    (``os.open`` with ``O_NOFOLLOW``, checked on the file actually opened --
    no separate ``lstat`` that a rename could race against), a non-regular
    file, a file not owned by uid 0, or a file writable by group or other.

    ``require_root_owned`` gates only the uid-0 check, and only via this
    explicit parameter -- never an environment variable, unlike the
    ``..._TEST_MODE`` pattern `agentsvc/libexec/codex_child.py` uses for its
    own test overrides, precisely so a stray environment variable can never
    weaken this check in production. Callers under test (which do not run as
    root) pass ``require_root_owned=False``; the group/world-writable check
    stays active either way, so a test can still exercise *that* refusal
    without needing to be root.
    """
    resolved = Path(path)
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise PolicyError(_open_error_code(exc)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PolicyError("not_regular_file")
        if require_root_owned and st.st_uid != 0:
            raise PolicyError("not_root_owned")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PolicyError("writable_by_group_or_other")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_ALLOWLIST_BYTES:
                raise PolicyError("too_large")
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError("invalid_json") from exc
    return parse_allowlist(data, repositories)


def parse_allowlist(data: dict, repositories: Iterable[RepositoryLike]) -> Allowlist:
    """Validate an already-parsed allowlist document. Pure; no I/O.

    Unknown fields reject the whole file at every level (top level, each
    project, each key) rather than being silently ignored -- a field a
    future version might give meaning to must not be quietly accepted (and
    misread as absent) by this one.
    """
    if not isinstance(data, dict):
        raise PolicyError("invalid_type")
    unknown = set(data) - _TOP_LEVEL_FIELDS
    if unknown:
        raise PolicyError("unknown_field")
    if data.get("version") != 1:
        raise PolicyError("bad_version")
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, dict):
        raise PolicyError("bad_projects")

    catalog: dict[str, RepositoryLike] = {}
    for repository in repositories:
        catalog[repository.project_key] = repository

    projects: dict[str, ProjectPolicy] = {}
    for project_key, raw_project in raw_projects.items():
        if not isinstance(project_key, str) or not _PROJECT_KEY_RE.fullmatch(project_key):
            raise PolicyError("bad_project_key")
        if project_key in HARD_DENIED_PROJECTS:
            raise PolicyError("project_denied")
        if not isinstance(raw_project, dict):
            raise PolicyError("bad_project")
        projects[project_key] = _parse_project(project_key, raw_project, catalog)

    return Allowlist(version=1, projects=MappingProxyType(projects))


def _parse_project(
    project_key: str, raw: dict, catalog: Mapping[str, RepositoryLike]
) -> ProjectPolicy:
    unknown = set(raw) - _PROJECT_FIELDS
    if unknown:
        raise PolicyError("unknown_field")
    missing = _PROJECT_FIELDS - set(raw)
    if missing:
        raise PolicyError("missing_field")

    repo_full_name = raw["repo_full_name"]
    stack = raw["stack"]
    env_file = raw["env_file"]
    raw_services = raw["services"]
    raw_containers = raw["containers"]
    ready_url = raw["ready_url"]
    raw_keys = raw["keys"]

    if not isinstance(repo_full_name, str):
        raise PolicyError("bad_repo_full_name")
    repository = catalog.get(project_key)
    if repository is None or repository.full_name != repo_full_name:
        raise PolicyError("repo_mismatch")

    if not isinstance(stack, str) or not _STACK_RE.fullmatch(stack):
        raise PolicyError("bad_stack")
    if env_file != f"/srv/stack/env/{stack}.env":
        raise PolicyError("bad_env_file")

    services = _parse_services(raw_services)
    containers = _parse_containers(raw_containers, repository)

    if ready_url is not None and (
        not isinstance(ready_url, str) or not _READY_URL_RE.fullmatch(ready_url)
    ):
        raise PolicyError("bad_ready_url")

    if not isinstance(raw_keys, dict) or not raw_keys:
        raise PolicyError("bad_keys")
    keys: dict[str, KeyPolicy] = {}
    for key_name, raw_key in raw_keys.items():
        if not isinstance(key_name, str) or not KEY_RE.fullmatch(key_name):
            raise PolicyError("bad_key")
        if SECRET_KEY_RE.fullmatch(key_name):
            raise PolicyError("secret_key")
        if not isinstance(raw_key, dict):
            raise PolicyError("bad_key_policy")
        keys[key_name] = _parse_key_policy(raw_key)

    return ProjectPolicy(
        project_key=project_key,
        repo_full_name=repo_full_name,
        stack=stack,
        env_file=env_file,
        services=services,
        containers=containers,
        ready_url=ready_url,
        keys=MappingProxyType(keys),
    )


def _parse_services(raw_services: object) -> tuple[str, ...]:
    if not isinstance(raw_services, list) or not (1 <= len(raw_services) <= 4):
        raise PolicyError("bad_services")
    seen: set[str] = set()
    services: list[str] = []
    for service in raw_services:
        if not isinstance(service, str) or not _SERVICE_NAME_RE.fullmatch(service):
            raise PolicyError("bad_services")
        if service in seen:
            raise PolicyError("bad_services")
        seen.add(service)
        services.append(service)
    return tuple(services)


def _parse_containers(
    raw_containers: object, repository: RepositoryLike | None
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw_containers, list) or not raw_containers:
        raise PolicyError("bad_containers")
    catalog_images = set(repository.images) if repository is not None else set()
    seen: set[tuple[str, str]] = set()
    containers: list[tuple[str, str]] = []
    for entry in raw_containers:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(item, str) for item in entry)
        ):
            raise PolicyError("bad_containers")
        pair = (entry[0], entry[1])
        if pair not in catalog_images:
            raise PolicyError("container_not_in_catalog")
        if pair in seen:
            raise PolicyError("bad_containers")
        seen.add(pair)
        containers.append(pair)
    return tuple(containers)


def _compile_pattern(source: object, bad_code: str) -> re.Pattern[str]:
    if not isinstance(source, str) or not source:
        raise PolicyError(bad_code)
    try:
        # re.ASCII: an allowlist-authored pattern that (unwisely) used `\d`
        # or `\w` instead of an explicit `[0-9]`/`[a-z...]` range must still
        # never admit a non-ASCII lookalike digit/letter.
        return re.compile(source, re.ASCII)
    except re.error as exc:
        raise PolicyError(bad_code) from exc


def _parse_key_policy(raw: dict) -> KeyPolicy:
    fmt = raw.get("format")
    if fmt not in FORMATS:
        raise PolicyError("bad_format")

    allowed_fields = _KEY_LIST_FIELDS if fmt in _LIST_FORMATS else _KEY_SCALAR_FIELDS
    unknown = set(raw) - allowed_fields
    if unknown:
        raise PolicyError("unknown_field")

    if fmt in _LIST_FORMATS:
        required = {"format", "ops", "item_re", "max_items", "description"}
        missing = required - set(raw)
        if missing:
            raise PolicyError("missing_field")
        item_re = _compile_pattern(raw["item_re"], "bad_item_re")
        max_items = raw["max_items"]
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not (1 <= max_items <= 200)
        ):
            raise PolicyError("bad_max_items")
        protected_items = _parse_protected_items(raw.get("protected_items", []), item_re)
        value_re = None
        ops = _parse_ops(raw.get("ops"), _LIST_OPS)
    else:
        required = {"format", "ops", "value_re", "description"}
        missing = required - set(raw)
        if missing:
            raise PolicyError("missing_field")
        value_re = _compile_pattern(raw["value_re"], "bad_value_re")
        item_re = None
        max_items = None
        protected_items = frozenset()
        ops = _parse_ops(raw.get("ops"), frozenset({"replace"}))
        if ops != ("replace",):
            raise PolicyError("bad_ops")

    description = raw["description"]
    if (
        not isinstance(description, str)
        or not (1 <= len(description) <= _MAX_DESCRIPTION_LEN)
        or not description.isprintable()
    ):
        raise PolicyError("bad_description")

    return KeyPolicy(
        format=fmt,
        ops=ops,
        description=description,
        item_re=item_re,
        max_items=max_items,
        protected_items=protected_items,
        value_re=value_re,
    )


def _parse_ops(raw_ops: object, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(raw_ops, list) or not raw_ops:
        raise PolicyError("bad_ops")
    if not all(isinstance(op, str) for op in raw_ops):
        raise PolicyError("bad_ops")
    if len(set(raw_ops)) != len(raw_ops):
        raise PolicyError("bad_ops")
    if not set(raw_ops) <= allowed:
        raise PolicyError("bad_ops")
    return tuple(raw_ops)


def _parse_protected_items(raw: object, item_re: re.Pattern[str]) -> frozenset[int]:
    if not isinstance(raw, list):
        raise PolicyError("bad_protected_items")
    protected: set[int] = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise PolicyError("bad_protected_items")
        if not item_re.fullmatch(str(item)):
            raise PolicyError("bad_protected_items")
        protected.add(item)
    return frozenset(protected)


# --------------------------------------------------------------------------
# Request validation. Used both by the trailer parser (deciding
# allowed/denied for a Codex proposal) and, independently, by the root
# helper (which never trusts what agent-svc told it and re-derives this
# itself from the request hash it re-checks).
# --------------------------------------------------------------------------


def validate_request(
    req: Mapping[str, object],
    allowlist: Allowlist,
    *,
    project_key: str,
    repo_full_name: str,
) -> tuple[bool, str]:
    """Check one already-parsed ops-request object against the allowlist.

    ``req`` carries the trailer-line shape: ``kind``, ``key``, ``op``,
    ``value``, and (per the trailer schema) ``reason``. ``reason`` is
    accepted -- callers may pass all five keys, as the trailer parser does
    -- but never inspected here: it is display-only text with its own
    sanitizer (``sanitize_reason``), not a policy input, so its presence,
    absence, or content never changes the result.

    Returns ``(True, "ok")`` or ``(False, reason_code)`` where
    ``reason_code`` is one of: ``"bad_kind"``, ``"bad_key"``,
    ``"secret_key"``, ``"not_allowlisted"``, ``"bad_op"``, ``"bad_value"``,
    ``"project_denied"``, ``"repo_mismatch"``, ``"item_pattern"``.
    """
    if project_key in HARD_DENIED_PROJECTS:
        return False, "project_denied"

    project = allowlist.projects.get(project_key)
    if project is None:
        return False, "not_allowlisted"
    if project.repo_full_name != repo_full_name:
        return False, "repo_mismatch"

    if req.get("kind") != "env_set":
        return False, "bad_kind"

    key = req.get("key")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return False, "bad_key"
    if SECRET_KEY_RE.fullmatch(key):
        return False, "secret_key"

    key_policy = project.keys.get(key)
    if key_policy is None:
        return False, "not_allowlisted"

    op = req.get("op")
    if not isinstance(op, str) or op not in OPS or op not in key_policy.ops:
        return False, "bad_op"

    value = req.get("value")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_VALUE_LEN
        or not VALUE_RE.fullmatch(value)
        or "://" in value
    ):
        return False, "bad_value"

    if key_policy.format == "scalar":
        if key_policy.value_re is None or not key_policy.value_re.fullmatch(value):
            return False, "bad_value"
    else:
        if key_policy.item_re is None or not key_policy.item_re.fullmatch(value):
            return False, "item_pattern"

    return True, "ok"


# --------------------------------------------------------------------------
# Applying an op to the current raw env-file value. Pure string/int
# manipulation -- no file I/O; the root helper owns reading/locking/writing
# the env file and calls this only to compute the replacement line text.
# --------------------------------------------------------------------------


def apply_op(
    fmt: str, old_raw: str | None, op: str, value: str, key_policy: KeyPolicy
) -> tuple[str, bool]:
    """Compute the new raw env value for one op, and whether it changed.

    ``old_raw``/the returned new value are the env value text with any
    surrounding quoting already stripped/to be re-added by the caller.

    Raises ``PolicyError`` (code in parentheses) when: ``fmt`` isn't one of
    `FORMATS` or disagrees with ``key_policy.format`` (``"bad_format"``);
    ``op`` isn't valid for ``fmt`` (``"bad_op"``); ``value`` is missing, too
    long, or fails ``key_policy.value_re``/``item_re`` (``"bad_value"`` for
    scalar, ``"item_pattern"`` for a list item); a list op is attempted with
    no current value (``"missing_key"`` -- a list add/remove has nothing to
    add to or remove from); ``old_raw`` cannot be parsed as a list of plain
    ints, or is not a JSON list, or contains a bool/float/duplicate
    (``"malformed_list"``); a ``list_add`` would exceed
    ``key_policy.max_items`` (``"max_items"``); a ``list_remove`` targets a
    protected item -- refused regardless of whether it is currently present,
    since "protected" means the removal itself is refused, not merely that
    it has no effect (``"protected_item"``); or a ``list_remove`` would
    empty the list (``"empty_result"`` -- at least one item must remain).
    """
    if fmt not in FORMATS or fmt != key_policy.format:
        raise PolicyError("bad_format")
    if op not in OPS:
        raise PolicyError("bad_op")
    if not isinstance(value, str) or not value or len(value) > MAX_VALUE_LEN:
        raise PolicyError("bad_value")

    if fmt == "scalar":
        if op != "replace":
            raise PolicyError("bad_op")
        if key_policy.value_re is None or not key_policy.value_re.fullmatch(value):
            raise PolicyError("bad_value")
        changed = old_raw is None or old_raw != value
        return value, changed

    if op not in _LIST_OPS:
        raise PolicyError("bad_op")
    if key_policy.item_re is None or not key_policy.item_re.fullmatch(value):
        raise PolicyError("item_pattern")
    if old_raw is None:
        raise PolicyError("missing_key")

    item = int(value)
    numbers = _parse_int_list(fmt, old_raw)

    if op == "list_add":
        if item in numbers:
            return _render_int_list(fmt, numbers), False
        if key_policy.max_items is not None and len(numbers) + 1 > key_policy.max_items:
            raise PolicyError("max_items")
        numbers.append(item)
        return _render_int_list(fmt, numbers), True

    # list_remove
    if item in key_policy.protected_items:
        raise PolicyError("protected_item")
    if item not in numbers:
        return _render_int_list(fmt, numbers), False
    remaining = [number for number in numbers if number != item]
    if not remaining:
        raise PolicyError("empty_result")
    return _render_int_list(fmt, remaining), True


def _parse_int_list(fmt: str, raw: str) -> list[int]:
    if fmt == "json_int_list":
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolicyError("malformed_list") from exc
        if not isinstance(parsed, list):
            raise PolicyError("malformed_list")
        numbers: list[int] = []
        for item in parsed:
            if isinstance(item, bool) or not isinstance(item, int):
                raise PolicyError("malformed_list")
            numbers.append(item)
    else:
        stripped = raw.strip()
        tokens = [token.strip() for token in stripped.split(",")] if stripped else []
        numbers = []
        for token in tokens:
            if not _CSV_INT_RE.fullmatch(token):
                raise PolicyError("malformed_list")
            numbers.append(int(token))
    if len(set(numbers)) != len(numbers):
        raise PolicyError("malformed_list")
    return numbers


def _render_int_list(fmt: str, numbers: list[int]) -> str:
    if fmt == "json_int_list":
        return json.dumps(numbers, separators=(",", ":"))
    return ",".join(str(number) for number in numbers)


# --------------------------------------------------------------------------
# Request hashing and reason sanitization.
# --------------------------------------------------------------------------


def request_hash(
    *, run_id: str, project_key: str, kind: str, key: str, op: str, value: str
) -> str:
    """The sha256 hex digest that binds one request to its exact effect.

    Canonical JSON: ``{"v":1,"run_id":...,"project_key":...,"kind":...,
    "key":...,"op":...,"value":...}`` with ``sort_keys=True``,
    ``separators=(",",":")``, ``ensure_ascii=True``. Recomputed by the
    backend (stored on the row), the owner's approval action id, and the
    root helper (refuses on mismatch) -- all three must derive the exact
    same bytes from the same inputs.
    """
    payload = {
        "v": 1,
        "run_id": run_id,
        "project_key": project_key,
        "kind": kind,
        "key": key,
        "op": op,
        "value": value,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


_STRIP_CATEGORIES = frozenset({"Cc", "Cf"})
# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR are category Zl/Zp, not
# Cc/Cf, so the general category filter below does not catch them on its
# own; they are exactly as capable of breaking a single-line card/log
# display, so they are stripped explicitly.
_EXTRA_STRIP_CHARS = frozenset({" ", " "})


def sanitize_reason(text: str) -> str:
    """Strip control characters and cap the result to 300 characters.

    Removes every character in Unicode general category Cc (control) or Cf
    (format, e.g. zero-width joiners) plus U+2028/U+2029, then truncates.
    Display-only text; this is never used in a policy decision.
    """
    kept = [
        ch
        for ch in text
        if ch not in _EXTRA_STRIP_CHARS and unicodedata.category(ch) not in _STRIP_CATEGORIES
    ]
    return "".join(kept)[:_MAX_REASON_LEN]
