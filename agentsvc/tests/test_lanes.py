from __future__ import annotations

import io
import json
import unittest
from datetime import UTC, datetime
from threading import Event
from unittest.mock import patch

from agent_svc.api import DiscussionLease, IntakeLease, InvalidWork, LeaseLost, Work
from agent_svc.lanes import ChatLane, CodeLane, LoopRunner, WatchLoop
from agent_svc.log import Logger, Redactor


def _logger() -> Logger:
    return Logger(Redactor([]), stream=io.StringIO())


def _work(run_id: str = "run-1", kind: str = "implement") -> Work:
    return Work(
        run_id=run_id,
        kind=kind,  # type: ignore[arg-type]
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=1,
        task_revision="rev",
        repo_full_name="Owner/repo",
        base_branch="main",
        mode="pr",
        title="t",
        description="d",
        image_count=0,
        complexity="simple",
        relevant_files=(),
        branch=None,
        pr_url=None,
        pr_number=None,
        head_sha=None,
        action_id=None,
        instruction=None,
        expected_head_sha=None,
    )


class CountingLoop(LoopRunner):
    def __init__(self, *, fail_times: int = 0) -> None:
        super().__init__(logger=_logger(), poll_s=0.0)
        self.ticks = 0
        self._fail_times = fail_times

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks <= self._fail_times:
            raise RuntimeError("boom")


class LoopRunnerTests(unittest.TestCase):
    def test_run_forever_stops_when_event_is_set(self) -> None:
        loop = CountingLoop()
        stop = Event()

        def fake_wait(_timeout: float) -> bool:
            if loop.ticks >= 3:
                stop.set()
            return False

        stop.wait = fake_wait  # type: ignore[method-assign]
        loop.run_forever(stop)
        self.assertEqual(loop.ticks, 3)

    def test_tick_exception_never_escapes_run_forever(self) -> None:
        loop = CountingLoop(fail_times=2)
        stop = Event()

        def fake_wait(_timeout: float) -> bool:
            if loop.ticks >= 3:
                stop.set()
            return False

        stop.wait = fake_wait  # type: ignore[method-assign]
        loop.run_forever(stop)  # must not raise despite the first two ticks failing
        self.assertEqual(loop.ticks, 3)

    def test_isolate_backs_off_and_resets_on_success(self) -> None:
        loop = CountingLoop()
        calls = []
        clock = [0.0]
        with patch("agent_svc.lanes.time.monotonic", side_effect=lambda: clock[0]):

            def fail() -> None:
                raise RuntimeError("nope")

            self.assertFalse(loop.isolate("rec-1", fail))
            self.assertFalse(loop.isolate("rec-1", fail))  # still in backoff, skipped
            self.assertEqual(len(calls), 0)

            clock[0] = 30.1  # first backoff (30s) has elapsed
            self.assertFalse(loop.isolate("rec-1", fail))  # fails again -> backoff doubles

            clock[0] = 30.1 + 59.9  # second backoff (60s) not yet elapsed
            self.assertFalse(loop.isolate("rec-1", fail))

            clock[0] = 30.1 + 60.1
            self.assertTrue(loop.isolate("rec-1", lambda: calls.append(1)))
            self.assertEqual(calls, [1])
            self.assertNotIn("rec-1", loop._backoff)

    def test_one_failing_record_does_not_block_another(self) -> None:
        loop = CountingLoop()
        good_calls = []
        loop.isolate("bad", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        loop.isolate("good", lambda: good_calls.append(1))
        self.assertEqual(good_calls, [1])


class FakeApi:
    def __init__(self, *, leases: list[object] | None = None) -> None:
        self._leases = list(leases or [])
        self.staged: list[tuple[str, str, str, str | None]] = []

    def lease(self, lane: str) -> Work | None:
        result = self._leases.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def stage(
        self, run_id: str, lease_id: str, stage: str, *, error: str | None = None
    ) -> None:
        self.staged.append((run_id, lease_id, stage, error))


class CodeLaneTests(unittest.TestCase):
    def test_disabled_lane_never_calls_lease(self) -> None:
        api = FakeApi(leases=[])
        lane = CodeLane(api=api, handlers={}, logger=_logger(), poll_s=0.0, enabled=False)
        lane.tick()  # would raise IndexError if lease() were called
        self.assertEqual(api.staged, [])

    def test_no_work_is_a_no_op(self) -> None:
        api = FakeApi(leases=[None])
        lane = CodeLane(api=api, handlers={}, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()
        self.assertEqual(api.staged, [])

    def test_registered_handler_is_invoked_with_the_work(self) -> None:
        received = []
        api = FakeApi(leases=[_work()])
        lane = CodeLane(
            api=api,
            handlers={"implement": lambda work: received.append(work)},
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
        )
        lane.tick()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].run_id, "run-1")

    def test_unhandled_kind_uses_default_handler_and_reports_stage_error(self) -> None:
        api = FakeApi(leases=[_work(kind="review")])
        lane = CodeLane(api=api, handlers={}, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()
        self.assertEqual(len(api.staged), 1)
        run_id, lease_id, stage, error = api.staged[0]
        self.assertEqual((run_id, lease_id, stage), ("run-1", "lease-1", "leased"))
        assert error is not None
        self.assertIn("no handler registered", error)

    def test_lease_lost_is_logged_and_swallowed(self) -> None:
        api = FakeApi(leases=[LeaseLost("cancelled")])
        lane = CodeLane(api=api, handlers={}, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()  # must not raise

    def test_invalid_lease_payload_is_logged_and_swallowed(self) -> None:
        api = FakeApi(leases=[InvalidWork("bad payload", run_id="run-9")])
        lane = CodeLane(api=api, handlers={}, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()  # must not raise

    def test_one_failing_handler_does_not_stop_the_next_tick(self) -> None:
        api = FakeApi(leases=[_work(run_id="run-1"), _work(run_id="run-2")])

        def boom(_work: Work) -> None:
            raise RuntimeError("handler exploded")

        processed = []
        lane = CodeLane(
            api=api,
            handlers={
                "implement": lambda work: (
                    boom(work) if work.run_id == "run-1" else processed.append(work.run_id)
                )
            },
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
        )
        lane.tick()
        lane.tick()
        self.assertEqual(processed, ["run-2"])

    def test_leased_record_still_in_backoff_is_never_silently_skipped(self) -> None:
        # A run whose handler recently failed is still leasable again (its
        # earlier lease naturally expired); if we just called `isolate()`
        # blindly, this would vanish with zero log output.
        work = _work(run_id="run-1")
        api = FakeApi(leases=[work])
        stream = io.StringIO()
        logger = Logger(Redactor([]), stream=stream)
        calls: list[Work] = []
        lane = CodeLane(
            api=api,
            handlers={"implement": lambda w: calls.append(w)},
            logger=logger,
            poll_s=0.0,
            enabled=True,
        )
        # Seed backoff for this exact run_id, as a prior failed attempt would.
        lane.isolate("run-1", lambda: (_ for _ in ()).throw(RuntimeError("prior failure")))
        self.assertTrue(lane.in_backoff("run-1"))
        stream.truncate(0)
        stream.seek(0)

        lane.tick()

        self.assertEqual(calls, [])  # handler must not run while still backing off
        events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
        self.assertIn("leased_record_in_backoff", events)

    def test_leased_record_runs_normally_once_backoff_clears(self) -> None:
        work = _work(run_id="run-1")
        api = FakeApi(leases=[work])
        calls: list[Work] = []
        lane = CodeLane(
            api=api,
            handlers={"implement": lambda w: calls.append(w)},
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
        )
        self.assertFalse(lane.in_backoff("run-1"))
        lane.tick()
        self.assertEqual([w.run_id for w in calls], ["run-1"])


def _intake_lease(intake_id: int = 1) -> IntakeLease:
    return IntakeLease(
        intake_id=intake_id,
        revision=1,
        lease_id="lease-1",
        text="Task",
        answer_text=None,
        mode="pr",
        images=(),
        repo_full_name="Owner/repo",
        base_branch="main",
        analysis_rounds=0,
    )


def _discussion_lease(discussion_id: int = 1) -> DiscussionLease:
    return DiscussionLease(
        discussion_id=discussion_id,
        revision=1,
        lease_id="lease-1",
        repo_full_name="Owner/repo",
        base_branch="main",
        project_key="repo",
        diagnostics_enabled=False,
        thread_id=None,
        text="Salom",
        images=(),
    )


class FakeChatApi:
    def __init__(
        self,
        *,
        intake_leases: list[object] | None = None,
        discussion_leases: list[object] | None = None,
    ) -> None:
        self._intake_leases = list(intake_leases or [])
        self._discussion_leases = list(discussion_leases or [])
        self.intake_lease_calls = 0
        self.discussion_lease_calls = 0

    def lease_intake(self) -> IntakeLease | None:
        self.intake_lease_calls += 1
        result = self._intake_leases.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def lease_discussion(self) -> DiscussionLease | None:
        self.discussion_lease_calls += 1
        result = self._discussion_leases.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ChatLaneTests(unittest.TestCase):
    def test_disabled_by_default_and_never_calls_the_api(self) -> None:
        api = FakeChatApi()
        lane = ChatLane(api=api, logger=_logger(), poll_s=0.0)
        lane.tick()  # would raise IndexError if it leased anything

    def test_disabled_lane_ignores_handlers_too(self) -> None:
        calls: list[object] = []
        api = FakeChatApi()
        lane = ChatLane(
            api=api,
            logger=_logger(),
            poll_s=0.0,
            enabled=False,
            handle_intake=lambda lease: calls.append(lease),
        )
        lane.tick()
        self.assertEqual(calls, [])

    def test_no_work_of_either_kind_is_a_no_op(self) -> None:
        api = FakeChatApi(intake_leases=[None], discussion_leases=[None])
        lane = ChatLane(api=api, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()
        self.assertEqual(api.intake_lease_calls, 1)
        self.assertEqual(api.discussion_lease_calls, 1)

    def test_intake_work_dispatches_to_its_handler_and_skips_discussion(self) -> None:
        lease = _intake_lease()
        received: list[IntakeLease] = []
        api = FakeChatApi(intake_leases=[lease])
        lane = ChatLane(
            api=api,
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
            handle_intake=lambda work: received.append(work),
        )
        lane.tick()
        self.assertEqual(received, [lease])
        self.assertEqual(api.discussion_lease_calls, 0)  # intake found -> no discussion poll

    def test_discussion_is_only_polled_when_intake_is_idle(self) -> None:
        lease = _discussion_lease()
        received: list[DiscussionLease] = []
        api = FakeChatApi(intake_leases=[None], discussion_leases=[lease])
        lane = ChatLane(
            api=api,
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
            handle_discussion=lambda work: received.append(work),
        )
        lane.tick()
        self.assertEqual(received, [lease])

    def test_an_intake_lease_fetch_failure_skips_discussion_this_tick(self) -> None:
        api = FakeChatApi(intake_leases=[RuntimeError("network blip")], discussion_leases=[])
        lane = ChatLane(api=api, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()  # must not raise, and must not try discussion (empty queue would IndexError)
        self.assertEqual(api.discussion_lease_calls, 0)

    def test_one_failing_intake_handler_does_not_block_a_later_one(self) -> None:
        api = FakeChatApi(intake_leases=[_intake_lease(1), _intake_lease(2)])

        def boom(work: IntakeLease) -> None:
            raise RuntimeError("handler exploded")

        processed: list[int] = []
        lane = ChatLane(
            api=api,
            logger=_logger(),
            poll_s=0.0,
            enabled=True,
            handle_intake=lambda work: (
                boom(work) if work.intake_id == 1 else processed.append(work.intake_id)
            ),
        )
        lane.tick()
        lane.tick()
        self.assertEqual(processed, [2])

    def test_no_handler_registered_is_logged_and_does_not_raise(self) -> None:
        api = FakeChatApi(intake_leases=[_intake_lease()])
        lane = ChatLane(api=api, logger=_logger(), poll_s=0.0, enabled=True)
        lane.tick()  # no handle_intake configured -> must not raise


class WatchLoopTests(unittest.TestCase):
    def test_each_check_runs_even_if_an_earlier_one_fails(self) -> None:
        calls = []

        def failing() -> None:
            raise RuntimeError("check failed")

        loop = WatchLoop(
            checks=[failing, lambda: calls.append("second")], logger=_logger(), poll_s=0.0
        )
        loop.tick()
        self.assertEqual(calls, ["second"])

    def test_disabled_watch_loop_runs_no_checks(self) -> None:
        calls = []
        loop = WatchLoop(
            checks=[lambda: calls.append(1)], logger=_logger(), poll_s=0.0, enabled=False
        )
        loop.tick()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
