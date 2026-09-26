"""Common run scaffolding shared by every code-lane handler.

`RunScaffold` owns everything that is the same for `implement`, `correction`,
and (eventually) `review`: the per-run work directory, a journal entry kept
current for crash recovery, a background heartbeat that turns a lost lease
into cooperative cancellation, a `stage(...)` helper that reports progress
without ever letting a network hiccup fail the run, and `codex.cleanup` in
`finally`.

Once the lease is lost (`LeaseLost` from `api.heartbeat`, or from `stage`
itself), `cancel` is set and every later `stage(...)` call becomes a no-op:
the Task Manager API already owns the outcome at that point, so the handler
must stop without sending any further callback.
"""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from . import repos
from .api import LeaseLost, Work
from .context import ServiceContext
from .journal import JournalEntry

HEARTBEAT_INTERVAL_S = 30.0
_JOIN_TIMEOUT_S = 5.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunScaffold:
    """`with RunScaffold(ctx, work, cancel) as run:` for the lifetime of one lease."""

    def __init__(
        self,
        ctx: ServiceContext,
        work: Work,
        cancel: threading.Event,
        *,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._ctx = ctx
        self._work = work
        self.cancel = cancel
        self._heartbeat_interval_s = heartbeat_interval_s
        self.run_dir: Path = repos.make_run_dir(ctx.settings.work_root, work.run_id)
        self._started_at = _now_iso()
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> RunScaffold:
        self._write_journal(stage="leased", base_sha=None, branch=self._work.branch)
        # A previous agent-svc process may have crashed between leasing this
        # run and its own cleanup (or `recovery.py` skipped it, or this is a
        # re-lease of a run that timed out mid-implement). `codex.prepare`
        # refuses outright if `wt/` already exists, so clearing any leftover
        # sandbox workspace and publish checkout BEFORE it ever runs is what
        # makes re-leasing the same run_id idempotent rather than stuck.
        self._cleanup_leftovers()
        # Registered so a service-wide shutdown (main.run's SIGTERM/SIGINT
        # handler) can cooperatively cancel this run too, not just stop
        # leasing new ones.
        self._ctx.cancel_registry.register(self.cancel)
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"heartbeat-{self._work.run_id}", daemon=True
        )
        self._heartbeat_thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=_JOIN_TIMEOUT_S)
        self._ctx.cancel_registry.unregister(self.cancel)
        self._cleanup_leftovers()
        # Downloaded task attachments are sensitive and only ever needed for
        # the duration of this run.
        shutil.rmtree(self.run_dir / "images", ignore_errors=True)
        self._ctx.journal.remove(self._work.run_id)

    def _cleanup_leftovers(self) -> None:
        try:
            self._ctx.codex.cleanup({"run_id": self._work.run_id})
        except Exception as exc:  # cleanup must never mask the real outcome
            self._ctx.logger.error(exc, event="codex_cleanup_failed", run_id=self._work.run_id)
        publish_dir = Path(self._ctx.settings.state_dir) / "publish" / self._work.run_id
        shutil.rmtree(publish_dir, ignore_errors=True)

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(self._heartbeat_interval_s):
            try:
                self._ctx.api.heartbeat(self._work.run_id, self._work.lease_id)
            except LeaseLost as exc:
                self._ctx.logger.event(
                    "lease_lost", level="warning", run_id=self._work.run_id, detail=exc.detail
                )
                self.cancel.set()
                return
            except Exception as exc:
                self._ctx.logger.error(exc, event="heartbeat_failed", run_id=self._work.run_id)

    def stage(
        self,
        stage: str,
        *,
        error: str | None = None,
        base_sha: str | None = None,
        branch: str | None = None,
    ) -> None:
        self._write_journal(stage=stage, base_sha=base_sha, branch=branch)
        if self.cancel.is_set():
            return
        try:
            self._ctx.api.stage(self._work.run_id, self._work.lease_id, stage, error=error)
        except LeaseLost:
            self.cancel.set()
        except Exception as exc:
            self._ctx.logger.error(
                exc, event="stage_report_failed", run_id=self._work.run_id, stage=stage
            )

    def _write_journal(self, *, stage: str, base_sha: str | None, branch: str | None) -> None:
        entry = JournalEntry(
            run_id=self._work.run_id,
            kind=self._work.kind,
            lease_id=self._work.lease_id,
            stage=stage,
            base_sha=base_sha,
            branch=branch,
            started_at=self._started_at,
            updated_at=_now_iso(),
        )
        try:
            self._ctx.journal.write(entry)
        except OSError as exc:
            self._ctx.logger.error(exc, event="journal_write_failed", run_id=self._work.run_id)
