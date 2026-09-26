"""Copy selected Task Manager secrets into agent-svc's credential files.

Run as root during ops/install_agent_svc.sh (step e). Reads
/srv/stack/env/task-manager.env, writes one file per mapped secret under
/etc/agent-svc/credentials/<name> (0600, root-owned) for systemd's
LoadCredential= to hand to the agent-svc service, and generates AGENT_SVC_TOKEN
the first time it is missing. Never prints a secret value.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from collections.abc import Callable
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
GENERATED_ENV_NAME = "AGENT_SVC_TOKEN"
GENERATED_CREDENTIAL_NAME = "agent_svc_token"

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _read_env_values(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


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


def _append_generated_token(env_file: Path, token: str) -> None:
    metadata = env_file.stat()
    contents = env_file.read_text(encoding="utf-8")
    if contents and not contents.endswith("\n"):
        contents += "\n"
    contents += f"{GENERATED_ENV_NAME}={token}\n"
    _atomic_write(
        env_file,
        contents,
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
    """
    if not task_manager_env_file.is_file():
        raise FileNotFoundError("task-manager environment file is missing")

    values = _read_env_values(task_manager_env_file)
    for env_name, credential_name in MAPPING.items():
        _write_credential(credentials_dir, credential_name, values.get(env_name, ""))

    token = values.get(GENERATED_ENV_NAME, "")
    outcome = "exists"
    if not token:
        token = token_factory()
        _append_generated_token(task_manager_env_file, token)
        outcome = "created"
    _write_credential(credentials_dir, GENERATED_CREDENTIAL_NAME, token)
    return outcome


if __name__ == "__main__":
    if sync_agent_svc_credentials() == "created":
        print("AGENT_SVC_TOKEN created; recreate task-api to load it")
    else:
        print("exists")
