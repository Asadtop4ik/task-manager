"""Lane loops: lease, dispatch by kind, and isolate one record's failure.

Every loop shares `LoopRunner`: a `tick()` that never raises out of
`run_forever`, and per-record isolation with exponential backoff (30s
doubling to 10 minutes, reset on success) so one bad run never starves the
others or stops the loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from threading import Event

from .api import InvalidResponse as ApiInvalidResponse
from .api import LeaseLost, TaskManagerApi, Work
from .log import Logger

_INITIAL_BACKOFF_S = 30.0
_MAX_BACKOFF_S = 600.0


class LoopRunner:
    def __init__(self, *, logger: Logger, poll_s: float) -> None:
        self._logger = logger
        self._poll_s = poll_s
        self._backoff: dict[str, float] = {}
        self._ready_at: dict[str, float] = {}

    @property
    def name(self) -> str:
        return type(self).__name__

    def tick(self) -> None:
        raise NotImplementedError

    def run_forever(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # a lane thread must never die
                self._logger.error(exc, event="lane_tick_failed", lane=self.name)
            stop.wait(self._poll_s)

    def in_backoff(self, record_id: str) -> bool:
        """True while `record_id` is still cooling down after a recent failure."""
        return time.monotonic() < self._ready_at.get(record_id, 0.0)

    def isolate(self, record_id: str, fn: Callable[[], None]) -> bool:
        """Run `fn`, skipping it while `record_id` is in backoff. True on success."""
        now = time.monotonic()
        if now < self._ready_at.get(record_id, 0.0):
            return False
        try:
            fn()
        except Exception as exc:
            self._logger.error(exc, event="record_failed", lane=self.name, run_id=record_id)
            previous = self._backoff.get(record_id)
            next_backoff = (
                _INITIAL_BACKOFF_S if previous is None else min(previous * 2, _MAX_BACKOFF_S)
            )
            self._backoff[record_id] = next_backoff
            self._ready_at[record_id] = now + next_backoff
            return False
        self._backoff.pop(record_id, None)
        self._ready_at.pop(record_id, None)
        return True


class CodeLane(LoopRunner):
    """Leases `lane=code` work and dispatches it to a handler by `kind`."""

    def __init__(
        self,
        *,
        api: TaskManagerApi,
        handlers: Mapping[str, Callable[[Work], None]],
        logger: Logger,
        poll_s: float,
        enabled: bool = False,
    ) -> None:
        super().__init__(logger=logger, poll_s=poll_s)
        self._api = api
        self._handlers = dict(handlers)
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "code"

    def tick(self) -> None:
        if not self._enabled:
            return
        try:
            work = self._api.lease("code")
        except LeaseLost as exc:
            self._logger.event(
                "lease_lost", level="warning", lane=self.name, detail=exc.detail
            )
            return
        except ApiInvalidResponse as exc:
            self._logger.error(
                exc,
                event="lease_invalid",
                lane=self.name,
                run_id=getattr(exc, "run_id", None),
            )
            return
        if work is None:
            return
        if self.in_backoff(work.run_id):
            # The backend just granted us a real, exclusive lease on this
            # run (it already ticked `attempts`/`_mark_running`); silently
            # calling `isolate()` here would swallow it with no log line at
            # all, and the lease would just expire unused. Make the skip
            # loud instead: an operator needs to see this, not lose the run.
            self._logger.event(
                "leased_record_in_backoff",
                level="warning",
                lane=self.name,
                run_id=work.run_id,
                task_id=work.task_id,
                stage="leased",
                detail=(
                    "this run recently failed and is still cooling down; not "
                    "re-invoking its handler now, the lease will expire on its own"
                ),
            )
            return
        handler = self._handlers.get(work.kind, self._default_handler)
        self.isolate(work.run_id, lambda: handler(work))

    def _default_handler(self, work: Work) -> None:
        detail = f"no handler registered for kind={work.kind!r}"
        self._logger.event(
            "stage_not_implemented",
            level="error",
            lane=self.name,
            run_id=work.run_id,
            task_id=work.task_id,
            stage="leased",
            detail=detail,
        )
        try:
            self._api.stage(work.run_id, work.lease_id, "leased", error=f"agent-svc: {detail}")
        except LeaseLost as exc:
            self._logger.event(
                "stage_report_failed",
                level="warning",
                lane=self.name,
                run_id=work.run_id,
                detail=exc.detail,
            )


class ChatLane(LoopRunner):
    """Phase-3 placeholder: disabled until chat leasing/dispatch is implemented."""

    def __init__(
        self, *, api: TaskManagerApi, logger: Logger, poll_s: float, enabled: bool = False
    ) -> None:
        super().__init__(logger=logger, poll_s=poll_s)
        self._api = api
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "chat"

    def tick(self) -> None:
        if not self._enabled:
            return
        # Phase 3: chat lane leasing/dispatch is not implemented yet.
        return


class WatchLoop(LoopRunner):
    """Runs a list of independent checks, each isolated from the others."""

    def __init__(
        self,
        *,
        checks: Sequence[Callable[[], None]],
        logger: Logger,
        poll_s: float,
        enabled: bool = True,
    ) -> None:
        super().__init__(logger=logger, poll_s=poll_s)
        self._checks = list(checks)
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "watch"

    def tick(self) -> None:
        if not self._enabled:
            return
        for index, check in enumerate(self._checks):
            self.isolate(f"check-{index}", check)
