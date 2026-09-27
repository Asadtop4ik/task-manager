"""Shared per-job scaffolding for the chat lane (intake, `/suhbat` discussion).

Unlike the code lane's `RunScaffold`, no heartbeat or journal integration is
needed here: an intake or discussion job finishes well inside the Task
Manager lease window (5 minutes) on its own timeout budget (180s / 150s),
and the backend already sweeps a lease that outlives that budget on its own
(see `lease_intake`/`lease_discussion`'s stale-lease handling in
`agent-intakes.py`/`project_discussions.py`) -- agent-svc crash recovery for
these two kinds is the backend's job, not agent-svc's.

What IS shared with the code lane: a fresh, agent-svc-local `run_id` (these
jobs have no Task-Manager-side run_id of their own -- the backend identifies
them by intake/discussion id instead), the sandboxed run directory, a
`cancel` Event registered with the service-wide `CancelRegistry` so a
shutdown interrupts an in-flight chat job instead of leaving it running
until its own timeout, and guaranteed cleanup of the sandbox workspace and
any downloaded images once the job is done.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType

from . import repos
from .context import ServiceContext

# A hard ceiling on the whole job, independent of and tighter than the Task
# Manager lease (5 minutes): every step (image download, mirror fetch,
# `prepare`, the Codex call itself) clamps its own timeout to whatever is
# left of this budget, so a slow step can never eat so much of the lease
# that a later step has no time left to even try -- and the job always
# finishes (one way or another) with margin before the lease itself would
# expire and the backend re-leases it out from under us.
JOB_DEADLINE_S = 270.0
# Never hand a callee a zero or negative timeout (which some APIs treat as
# "wait forever" and others reject outright); this is small enough that a
# genuinely exhausted budget still fails fast.
_MIN_STEP_TIMEOUT_S = 1.0


class ChatRun:
    """`with ChatRun(ctx) as run:` for the lifetime of one intake/discussion job."""

    def __init__(self, ctx: ServiceContext) -> None:
        self._ctx = ctx
        self.run_id = str(uuid.uuid4())
        self.cancel = threading.Event()
        self._deadline = time.monotonic() + JOB_DEADLINE_S
        self.run_dir: Path = repos.make_run_dir(ctx.settings.work_root, self.run_id)

    def remaining_s(self) -> float:
        """Seconds left in this job's own deadline, floored at `_MIN_STEP_TIMEOUT_S`."""
        return max(self._deadline - time.monotonic(), _MIN_STEP_TIMEOUT_S)

    def __enter__(self) -> ChatRun:
        self._ctx.cancel_registry.register(self.cancel)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._ctx.cancel_registry.unregister(self.cancel)
        try:
            self._ctx.codex.cleanup({"run_id": self.run_id})
        except Exception as inner_exc:  # cleanup must never mask the real outcome
            self._ctx.logger.error(inner_exc, event="codex_cleanup_failed", run_id=self.run_id)
        # Downloaded intake/discussion attachments are sensitive and only
        # ever needed for the duration of this job.
        shutil.rmtree(self.run_dir / "images", ignore_errors=True)
