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
            # Removed on exit, and codex.cleanup was called on enter (to
            # clear any leftover from a previous crashed run) and on exit.
            self.assertIsNone(ctx.journal.read(work.run_id))
            self.assertEqual(len(ctx.codex.cleanup_calls), 2)  # type: ignore[attr-defined]

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

    def test_enter_removes_a_leftover_publish_dir_before_prepare_would_run(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            leftover = Path(ctx.settings.state_dir) / "publish" / work.run_id
            leftover.mkdir(parents=True)
            (leftover / "stale.txt").write_text("from a crashed run\n", encoding="utf-8")

            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=1000.0):
                # Gone by the time `__enter__` returns -- before any
                # prepare/exec/publish call for this fresh attempt.
                self.assertFalse(leftover.exists())

    def test_enter_cleans_up_codex_before_any_other_work(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            api = FakeApi()
            ctx = build_test_context(Path(tmp), api=api)
            work = _implement_work()
            cancel = threading.Event()
            with RunScaffold(ctx, work, cancel, heartbeat_interval_s=1000.0):
                self.assertEqual(len(ctx.codex.cleanup_calls), 1)  # type: ignore[attr-defined]
                self.assertEqual(len(ctx.codex.prepare_calls), 0)  # type: ignore[attr-defined]

    def test_deliver_succeeds_on_first_try_without_sleeping(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            ctx = build_test_context(Path(tmp), api=FakeApi())
            work = _implement_work()
            cancel = threading.Event()
            calls: list[int] = []
            with RunScaffold(
                ctx, work, cancel, heartbeat_interval_s=1000.0, delivery_backoff_s=(5.0, 5.0)
            ) as run:
                run.deliver(lambda: calls.append(1))
            self.assertEqual(calls, [1])
            # Journal removed: the delivery succeeded.
            self.assertIsNone(ctx.journal.read(work.run_id))

    def test_deliver_retries_a_transient_failure_then_succeeds(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            ctx = build_test_context(Path(tmp), api=FakeApi())
            work = _implement_work()
            cancel = threading.Event()
            attempts = {"count": 0}

            def flaky() -> None:
                attempts["count"] += 1
                if attempts["count"] < 2:
                    raise RuntimeError("network blip")

            with RunScaffold(
                ctx, work, cancel, heartbeat_interval_s=1000.0, delivery_backoff_s=(0.01, 0.01)
            ) as run:
                run.deliver(flaky)
            self.assertEqual(attempts["count"], 2)
            self.assertIsNone(ctx.journal.read(work.run_id))

    def test_deliver_keeps_the_journal_entry_after_exhausting_every_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            ctx = build_test_context(Path(tmp), api=FakeApi())
            work = _implement_work()
            cancel = threading.Event()
            attempts = {"count": 0}

            def always_fails() -> None:
                attempts["count"] += 1
                raise RuntimeError("backend unreachable")

            with RunScaffold(
                ctx, work, cancel, heartbeat_interval_s=1000.0, delivery_backoff_s=(0.01, 0.01)
            ) as run:
                run.deliver(always_fails)
            self.assertEqual(attempts["count"], 3)  # 1 initial + 2 retries
            # Kept, not removed: a future recover() pass gets another chance.
            self.assertIsNotNone(ctx.journal.read(work.run_id))

    def test_deliver_lease_lost_cancels_immediately_without_retrying(self) -> None:
        with TemporaryDirectory() as tmp:
            from agent_svc.runctx import RunScaffold

            ctx = build_test_context(Path(tmp), api=FakeApi())
            work = _implement_work()
            cancel = threading.Event()
            attempts = {"count": 0}

            def lease_lost() -> None:
                attempts["count"] += 1
                raise LeaseLost("cancelled")

            with RunScaffold(
                ctx, work, cancel, heartbeat_interval_s=1000.0, delivery_backoff_s=(5.0, 5.0)
            ) as run:
                run.deliver(lease_lost)
                self.assertTrue(cancel.is_set())
            self.assertEqual(attempts["count"], 1)  # never retried
            # LeaseLost is not a delivery failure -- the API already decided;
            # the journal is still removed normally.
            self.assertIsNone(ctx.journal.read(work.run_id))

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
