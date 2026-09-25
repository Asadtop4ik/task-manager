"""Copy only the enabled QA flag and scoped read token into the host monitor env."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

ENV_FILE = Path("/etc/task-manager/external-monitor.env")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def _task_api_environment(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    result = run(
        ["docker", "inspect", "task-api"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    records = json.loads(result.stdout)
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("task-api container inspection failed")
    entries = (records[0].get("Config") or {}).get("Env") or []
    values: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, str) and "=" in entry:
            name, value = entry.split("=", 1)
            values[name] = value
    return values


def _replace_settings(contents: str, settings: dict[str, str]) -> str:
    remaining = set(settings)
    lines: list[str] = []
    for line in contents.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in settings:
            if key in remaining:
                lines.append(f"{key}={settings[key]}")
                remaining.remove(key)
            continue
        lines.append(line)
    lines.extend(f"{key}={settings[key]}" for key in sorted(remaining))
    return "\n".join(lines) + "\n"


def sync_qa_environment(
    *,
    env_file: Path = ENV_FILE,
    task_api_env: dict[str, str] | None = None,
) -> None:
    source = task_api_env if task_api_env is not None else _task_api_environment()
    enabled = source.get("AGENT_QA_ENABLED", "").lower() == "true"
    settings = {
        "AGENT_QA_ENABLED": "true" if enabled else "false",
        "GITHUB_AGENT_QA_TOKEN": "",
    }
    if enabled:
        token = source.get("GITHUB_AGENT_QA_TOKEN", "")
        if not token or not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("enabled QA requires a valid repository-scoped read token")
        settings["GITHUB_AGENT_QA_TOKEN"] = token
    if not env_file.is_file():
        raise FileNotFoundError("external monitor environment file is missing")

    metadata = env_file.stat()
    updated = _replace_settings(env_file.read_text(encoding="utf-8"), settings)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{env_file.name}.", dir=env_file.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
        os.fchmod(fd, metadata.st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_file)
        directory_fd = os.open(env_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    sync_qa_environment()
    print("external monitor QA configuration synced")
