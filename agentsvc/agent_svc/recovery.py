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

import shutil
from pathlib import Path
from typing import Any

from .api import TaskManagerApi
from .codex import CodexRunner
from .journal import Journal, JournalEntry
from .log import Logger

# Statuses after which no further agent-svc action on this run is expected.
TERMINAL_STATUSES = frozenset({"merged", "deployed", "failed", "cancelled"})


def recover(
    journal: Journal,
    api: TaskManagerApi,
    logger: Logger,
    *,
    codex: CodexRunner | None = None,
    state_dir: str | None = None,
) -> list[JournalEntry]:
    """Clean up finished/foreign journal entries; return the rest for resume.

    `codex`/`state_dir` (optional -- omitting either just skips this side
    effect) remove any sandbox workspace or publish checkout a PREVIOUS
    agent-svc process left behind for every entry this pass classifies,
    whether it turns out to be terminal/foreign (about to be dropped) or
    live (about to be resumed): `RunScaffold.__enter__` does the identical
    cleanup again before a resumed run's own `codex.prepare`, so this is not
    the only guard, but a crashed run's workspace should not sit there
    untouched for however long it takes for that run to be resumed or
    re-leased.
    """
    resume: list[JournalEntry] = []
    for entry in journal.list():
        try:
            status_payload = api.status(entry.run_id)
        except Exception as exc:
            # `api.status` can fail in more ways than `HttpError` (a timeout,
            # a malformed JSON body, a dropped connection, ...); one bad
            # entry must log and move on, never abort startup recovery, and
            # never touch its workspace without knowing its real status.
            logger.error(exc, event="recovery_status_failed", run_id=entry.run_id)
            continue
        if _clean_or_resume(entry, status_payload, journal, logger):
            resume.append(entry)
        _cleanup_run_leftovers(entry.run_id, codex=codex, state_dir=state_dir, logger=logger)
    return resume


def _cleanup_run_leftovers(
    run_id: str,
    *,
    codex: CodexRunner | None,
    state_dir: str | None,
    logger: Logger,
) -> None:
    if codex is not None:
        try:
            codex.cleanup({"run_id": run_id})
        except Exception as exc:
            logger.error(exc, event="recovery_codex_cleanup_failed", run_id=run_id)
    if state_dir is not None:
        publish_dir = Path(state_dir) / "publish" / run_id
        shutil.rmtree(publish_dir, ignore_errors=True)


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
