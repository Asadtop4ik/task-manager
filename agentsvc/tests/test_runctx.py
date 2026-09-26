from __future__ import annotations

import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.api import LeaseLost, Work

from .support import FakeApi, build_test_context


def _implement_work(run_id: str = "11111111-1111-1111-1111-111111111111") -> Work:
    return Work(
        run_id=run_id,
        kind="implement",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=1,
        task_revision="rev",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        title="t",
        description="d",
        image_count=0,
        complexity="simple",
        relevant_files=(),
        branch="codex/task-1-" + run_id,
        pr_url=None,
        pr_number=None,
        head_sha=None,
        action_id=None,
        instruction=None,
        expected_head_sha=None,
    )


class RunScaffoldTests(unittest.TestCase):
    def test_enter_creates_run_dir_and_journal_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=1000.0) as run:
                self.assertTrue(run.run_dir.is_dir())
                entry = ctx.journal.read(work.run_id)
                assert entry is not None
                self.assertEqual(entry.stage, "leased")
            # Removed on exit, and codex.cleanup was called.
            self.assertIsNone(ctx.journal.read(work.run_id))
            self.assertEqual(len(ctx.codex.cleanup_calls), 1)  # type: ignore[attr-defined]

    def test_stage_updates_journal_and_reports_to_api(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=1000.0) as run:
                run.stage("workspace_ready", base_sha="a" * 40, branch="codex/task-1-x")
            self.assertEqual(len(api.stages), 1)
            run_id, lease_id, stage, error = api.stages[0]
            self.assertEqual(
                (run_id, lease_id, stage, error),
                (work.run_id, "lease-1", "workspace_ready", None),
            )

    def test_stage_failure_is_logged_and_never_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            class BoomApi(FakeApi):
                def stage(self, *a, **k):  # type: ignore[override]
                    raise RuntimeError("network down")

            ctx = build_test_context(Path(tmp), api=BoomApi())
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=1000.0) as run:
                run.stage("workspace_ready")  # must not raise
            self.assertFalse(cancel.is_set())

    def test_heartbeat_lease_lost_sets_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            api.queue_heartbeat_effects(LeaseLost("cancelled"))
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=0.05) as run:
                self.assertTrue(cancel.wait(timeout=2.0))
                # Once cancelled, stage() must not call the API again.
                run.stage("codex_started")
            self.assertEqual(api.stages, [])

    def test_heartbeat_transient_failure_does_not_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            api.queue_heartbeat_effects(RuntimeError("timeout"))
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=0.05) as run:
                import time

                time.sleep(0.3)
                self.assertFalse(run.cancel.is_set())


if __name__ == "__main__":
    unittest.main()
