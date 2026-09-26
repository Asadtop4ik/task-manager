"""Copy selected Task Manager secrets into agent-svc's credential files.

Run as root during ops/install_agent_svc.sh (step e). Reads
/srv/stack/env/task-manager.env, writes one file per mapped secret under
/etc/agent-svc/credentials/<name> (0600, root-owned) for systemd's
LoadCredential= to hand to the agent-svc service, and generates AGENT_SVC_TOKEN
the first time it is missing. Never prints a secret value.

GITHUB_AGENT_QA_TOKEN is optional: its credential file is always written (like
every other mapped secret, since this systemd version has no "ignore if
missing" form of LoadCredential=), but it may be empty — an empty
github_qa_token means QA is disabled, see docs/AGENT_SVC.md. Every other
mapped key is required; a missing or empty one is a hard failure that names
the key, never a value.

The env file's own formatting is trusted only for the exact `KEY=value` shape:
no `export`, no surrounding whitespace, no quoting, no inline `#` comment on a
line that assigns one of the names this script reads. Anything else on one of
those lines is treated as a parse error rather than guessed at, because a
silently mis-parsed token (e.g. with a trailing comment folded into the value)
would otherwise ship a broken credential without any visible error.

Callers that also rewrite /srv/stack/env/task-manager.env (update_public_agent_token.py
today) should take the same `<env file>.lock` flock before reading or writing
it, so a concurrent run of either script can't interleave a read with the
other's write.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

TASK_MANAGER_ENV_FILE = Path("/srv/stack/env/task-manager.env")
CREDENTIALS_DIR = Path("/etc/agent-svc/credentials")

# Task Manager env var name -> agent-svc credential file name.
MAPPING: dict[str, str] = {
    "AGENT_CALLBACK_TOKEN": "callback_token",
    "INTAKE_WORKER_TOKEN": "intake_worker_token",
    "GITHUB_AGENT_TOKEN": "github_agent_token",
    "GITHUB_PUBLIC_AGENT_TOKEN": "github_public_agent_token",
    "GITHUB_AGENT_QA_TOKEN": "github_qa_token",
}
OPTIONAL_ENV_NAMES: frozenset[str] = frozenset({"GITHUB_AGENT_QA_TOKEN"})
REQUIRED_ENV_NAMES: frozenset[str] = frozenset(MAPPING) - OPTIONAL_ENV_NAMES

GENERATED_ENV_NAME = "AGENT_SVC_TOKEN"
GENERATED_CREDENTIAL_NAME = "agent_svc_token"

# Names this script actually consumes; formatting is strictly enforced only
# for these. Everything else in the env file is left alone, untouched.
_KNOWN_NAMES: frozenset[str] = frozenset(MAPPING) | {GENERATED_ENV_NAME}

# Loosely detects "this line assigns one of our names, in some shell-ish way"
# (with or without `export`, with or without spacing) so a line like that can
# be validated strictly, instead of silently falling through unmatched.
_LOOSE_ASSIGNMENT = re.compile(r"^[ \t]*(export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
# The only shape this script accepts: KEY=value, nothing before or after.
_STRICT_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class MalformedSecretLineError(ValueError):
    """A line assigning a name this script reads isn't plain KEY=value."""

    def __init__(self, key: str, env_file: Path):
        super().__init__(
            f"{key} in {env_file} is not a plain KEY=value line "
            "(no 'export', surrounding spaces, quotes, or inline '#' comments allowed)"
        )
        self.key = key


class MissingRequiredSecretError(ValueError):
    """A required secret is missing or empty. Never carries the value."""

    def __init__(self, key: str, env_file: Path):
        super().__init__(f"{key} is missing or empty in {env_file}")
        self.key = key


def _value_looks_wrapped_or_commented(value: str) -> bool:
    if value != value.strip():
        return True
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        return True
    return "#" in value


def _parse_env_values(contents: str, env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        loose = _LOOSE_ASSIGNMENT.match(raw_line)
        if not loose:
            continue
        has_export, key = loose.group(1), loose.group(2)
        strict = _STRICT_ASSIGNMENT.match(raw_line)
        is_clean = (
            strict is not None
            and strict.group(1) == key
            and has_export is None
            and not _value_looks_wrapped_or_commented(strict.group(2))
        )
        if key in _KNOWN_NAMES:
            if not is_clean:
                raise MalformedSecretLineError(key, env_file)
            values[key] = strict.group(2)
        elif is_clean:
            values[key] = strict.group(2)
    return values


@contextmanager
def _locked(env_file: Path) -> Iterator[None]:
    # Locks a fixed sibling file rather than env_file itself: env_file is
    # replaced via atomic rename (a new inode each time it changes), and
    # flock()ing a path that gets renamed away from under you does not
    # serialize against the next writer. A lock file that is never replaced
    # avoids that hazard.
    lock_path = env_file.with_name(env_file.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(
    path: Path,
    content: str,
    mode: int,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        if uid is not None and gid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _write_credential(directory: Path, name: str, value: str) -> None:
    # No explicit chown: this script only ever runs as root (via sudo, see
    # ops/install_agent_svc.sh step e), so files it creates are already
    # root-owned. Leaving ownership alone keeps this testable as a normal user.
    _atomic_write(directory / name, value, 0o600)


def _append_generated_token(env_file: Path, contents: str, token: str) -> None:
    metadata = env_file.stat()
    updated = contents
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += f"{GENERATED_ENV_NAME}={token}\n"
    _atomic_write(
        env_file,
        updated,
        mode=metadata.st_mode & 0o777,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
    )


def sync_agent_svc_credentials(
    *,
    task_manager_env_file: Path = TASK_MANAGER_ENV_FILE,
    credentials_dir: Path = CREDENTIALS_DIR,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
) -> str:
    """Copy mapped secrets and ensure AGENT_SVC_TOKEN exists.

    Returns "created" if AGENT_SVC_TOKEN was just generated, "exists" otherwise.
    Idempotent: re-running with unchanged inputs rewrites the same values.

    Raises MissingRequiredSecretError or MalformedSecretLineError (naming only
    the offending key, never a value) instead of writing a wrong or partial
    credential file.
    """
    if not task_manager_env_file.is_file():
        raise FileNotFoundError("task-manager environment file is missing")

    with _locked(task_manager_env_file):
        contents = task_manager_env_file.read_text(encoding="utf-8")
        values = _parse_env_values(contents, task_manager_env_file)

        for env_name in REQUIRED_ENV_NAMES:
            if not values.get(env_name):
                raise MissingRequiredSecretError(env_name, task_manager_env_file)

        for env_name, credential_name in MAPPING.items():
            # Optional secrets still get a file (systemd's LoadCredential= on this
            # host has no "missing is fine" form); an empty value there means the
            # corresponding feature (QA) is disabled, never a missing credential.
            _write_credential(credentials_dir, credential_name, values.get(env_name, ""))

        token = values.get(GENERATED_ENV_NAME, "")
        outcome = "exists"
        if not token:
            token = token_factory()
            _append_generated_token(task_manager_env_file, contents, token)
            outcome = "created"
        _write_credential(credentials_dir, GENERATED_CREDENTIAL_NAME, token)
        return outcome


if __name__ == "__main__":
    if sync_agent_svc_credentials() == "created":
        print("AGENT_SVC_TOKEN created; recreate task-api to load it")
    else:
        print("exists")
