"""Startup recovery: reconcile the journal against `/status` before leasing resumes.

This is intentionally a skeleton: it decides which journal entries are done
(and can be dropped) versus which still need work, but the actual resume
logic (re-attaching to a workspace, re-running preflight, ...) belongs to the
lane handlers landing in later work packages. `AgentRunOut` (the `/status`
payload) does not currently expose the run's live lease id, so the "lease not
ours" check below only fires when that field happens to be present; otherwise
recovery falls back to the terminal-status check alone.
"""

from __future__ import annotations

from typing import Any

from .api import TaskManagerApi
from .http import HttpError
from .journal import Journal, JournalEntry
from .log import Logger

# Statuses after which no further agent-svc action on this run is expected.
TERMINAL_STATUSES = frozenset({"merged", "deployed", "failed", "cancelled"})


def recover(journal: Journal, api: TaskManagerApi, logger: Logger) -> list[JournalEntry]:
    """Clean up finished/foreign journal entries; return the rest for resume."""
    resume: list[JournalEntry] = []
    for entry in journal.list():
        try:
            status_payload = api.status(entry.run_id)
        except HttpError as exc:
            logger.error(exc, event="recovery_status_failed", run_id=entry.run_id)
            continue
        if not _clean_or_resume(entry, status_payload, journal, logger):
            continue
        resume.append(entry)
    return resume


def _clean_or_resume(
    entry: JournalEntry, status_payload: Any, journal: Journal, logger: Logger
) -> bool:
    if not isinstance(status_payload, dict):
        logger.event(
            "recovery_status_invalid", level="error", run_id=entry.run_id, stage=entry.stage
        )
        return False

    run_status = status_payload.get("status")
    terminal = run_status in TERMINAL_STATUSES

    current_lease = status_payload.get("lease_id")
    ours = not isinstance(current_lease, str) or current_lease == entry.lease_id

    if terminal or not ours:
        journal.remove(entry.run_id)
        logger.event(
            "recovery_cleaned",
            lane="recovery",
            run_id=entry.run_id,
            stage=entry.stage,
            detail=f"status={run_status!r} ours={ours}",
        )
        return False
    return True
