"""Atomic on-disk run journal: `<runs_dir>/<run_id>.json`.

Every write goes to a temp file in the same directory, then `os.replace`, so a
crash mid-write never leaves a half-written journal entry. A file that fails
to parse is moved aside into `<runs_dir>/corrupt/` and logged rather than
raised, so one bad entry never blocks startup recovery for the rest.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .log import Logger

RUN_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
_FIELDS = (
    "run_id",
    "kind",
    "lease_id",
    "stage",
    "base_sha",
    "branch",
    "started_at",
    "updated_at",
)
_ENTRY_MODE = 0o600


@dataclass(frozen=True)
class JournalEntry:
    run_id: str
    kind: str
    lease_id: str
    stage: str
    base_sha: str | None
    branch: str | None
    started_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _entry_from_dict(payload: Any) -> JournalEntry:
    if not isinstance(payload, dict):
        raise ValueError("journal entry is not an object")
    missing = [field for field in _FIELDS if field not in payload]
    if missing:
        raise ValueError(f"journal entry is missing field(s): {', '.join(missing)}")

    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("journal entry has an invalid run_id")
    kind = payload["kind"]
    if not isinstance(kind, str) or not kind:
        raise ValueError("journal entry has an invalid kind")
    lease_id = payload["lease_id"]
    if not isinstance(lease_id, str) or not lease_id:
        raise ValueError("journal entry has an invalid lease_id")
    stage = payload["stage"]
    if not isinstance(stage, str) or not stage:
        raise ValueError("journal entry has an invalid stage")
    base_sha = payload["base_sha"]
    if base_sha is not None and (
        not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", base_sha)
    ):
        raise ValueError("journal entry has an invalid base_sha")
    branch = payload["branch"]
    if branch is not None and not isinstance(branch, str):
        raise ValueError("journal entry has an invalid branch")
    started_at = payload["started_at"]
    updated_at = payload["updated_at"]
    if not isinstance(started_at, str) or not started_at:
        raise ValueError("journal entry has an invalid started_at")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("journal entry has an invalid updated_at")

    return JournalEntry(
        run_id=run_id,
        kind=kind,
        lease_id=lease_id,
        stage=stage,
        base_sha=base_sha,
        branch=branch,
        started_at=started_at,
        updated_at=updated_at,
    )


class Journal:
    def __init__(self, runs_dir: str | Path, *, logger: Logger | None = None) -> None:
        self._dir = Path(runs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._logger = logger

    def _path(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self._dir / f"{run_id}.json"

    def write(self, entry: JournalEntry) -> None:
        path = self._path(entry.run_id)
        data = json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".tmp-", suffix=".json")
        try:
            try:
                os.write(fd, data)
                os.fchmod(fd, _ENTRY_MODE)
            finally:
                os.close(fd)
            os.replace(tmp_name, path)
        except BaseException:
            # Whatever failed (write, fchmod, or the final replace), never
            # leave a stray `.tmp-*.json` behind for `list()` to trip over.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def read(self, run_id: str) -> JournalEntry | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            return _entry_from_dict(json.loads(raw))
        except (OSError, ValueError, TypeError) as exc:
            self._quarantine(path, exc)
            return None

    def list(self) -> list[JournalEntry]:
        entries: list[JournalEntry] = []
        for path in sorted(self._dir.glob("*.json")):
            # `Path.glob` (unlike the stdlib `glob` module) matches dotfiles,
            # so a leftover `.tmp-*.json` from an interrupted `write()` would
            # otherwise reach `read()` and blow up on an invalid run_id.
            if not RUN_ID_RE.fullmatch(path.stem):
                continue
            entry = self.read(path.stem)
            if entry is not None:
                entries.append(entry)
        return entries

    def remove(self, run_id: str) -> None:
        path = self._path(run_id)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _quarantine(self, path: Path, exc: Exception) -> None:
        if self._logger is not None:
            self._logger.error(exc, event="journal_corrupt", run_id=path.stem)
        corrupt_dir = self._dir / "corrupt"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        target = corrupt_dir / f"{path.stem}-{int(time.time())}.json"
        with contextlib.suppress(OSError):
            os.replace(path, target)
