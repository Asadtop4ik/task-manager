"""Tests for `agent_svc/codex.py` (`CodexRunner`).

Two layers:

- Pure unit tests against a hand-rolled `FakePopen` (no real process), for
  `CodexRunner`'s own orchestration logic: heartbeat/cancel/hard-wall timeout
  handling, stderr bounding, event forwarding. These can use very small
  timeouts and stay fast.
- End-to-end tests that spawn the real `libexec/codex_child.py` (in test
  mode, `command_prefix=[]`) against `fake_codex.py`, reusing the harness
  from `test_codex_child.py` (work_root/mirrors_dir/codex homes, env
  overrides). These prove the two modules actually agree on the wire
  protocol, not just that each one's mocks are self-consistent.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
AGENTSVC_DIR = TESTS_DIR.parent
if str(AGENTSVC_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTSVC_DIR))

from agent_svc.codex import CodexChildError, CodexResult, CodexRunner  # noqa: E402
from tests.test_codex_child import (  # noqa: E402
    LIBEXEC_DIR,
    PYTHON_BIN,
    ChildProcessTestCase,
    new_run_id,
)

# ---------------------------------------------------------------------------
# prepare / package / cleanup (subprocess.run-shaped fake runner)
# ---------------------------------------------------------------------------


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout


class SimpleCallTests(unittest.TestCase):
    def test_argv_uses_default_sudo_prefix(self) -> None:
        runner = CodexRunner()
        argv = runner._argv("prepare")
        self.assertEqual(
            argv,
            [
                "/usr/bin/sudo",
                "-n",
                "-u",
                "agent-codex",
                "--",
                "/usr/bin/python3",
                "/opt/agent-svc/libexec/codex_child.py",
                "prepare",
            ],
        )

    def test_argv_with_test_prefix(self) -> None:
        runner = CodexRunner(
            command_prefix=[], python_bin="/usr/bin/python3.12", libexec_dir="/scratch/libexec"
        )
        self.assertEqual(
            runner._argv("exec"),
            ["/usr/bin/python3.12", "/scratch/libexec/codex_child.py", "exec"],
        )

    def test_prepare_success(self) -> None:
        calls = []

        def fake_runner(argv, **kwargs):
            calls.append((argv, kwargs.get("input")))
            return FakeCompletedProcess(0, b'{"ok": true, "head": "abc123"}')

        runner = CodexRunner(
            command_prefix=[], python_bin="python3", libexec_dir="/x", runner=fake_runner
        )
        result = runner.prepare({"run_id": "r1"})
        self.assertEqual(result, {"ok": True, "head": "abc123"})
        argv, payload = calls[0]
        self.assertEqual(argv, ["python3", "/x/codex_child.py", "prepare"])
        self.assertEqual(json.loads(payload), {"run_id": "r1"})

    def test_prepare_failure_raises_with_reason(self) -> None:
        def fake_runner(argv, **kwargs):
            return FakeCompletedProcess(3, b'{"reason": "invalid run_id"}')

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        with self.assertRaises(CodexChildError) as ctx:
            runner.prepare({"run_id": "bad"})
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertEqual(ctx.exception.reason, "invalid run_id")

    def test_package_rejects_non_dict_output(self) -> None:
        def fake_runner(argv, **kwargs):
            return FakeCompletedProcess(0, b"not json")

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        with self.assertRaises(CodexChildError):
            runner.package({"run_id": "r1"})

    def test_cleanup_success(self) -> None:
        def fake_runner(argv, **kwargs):
            return FakeCompletedProcess(0, b'{"ok": true}')

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        self.assertEqual(runner.cleanup({"run_id": "r1"}), {"ok": True})

    def test_simple_call_timeout_raises(self) -> None:
        def fake_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        with self.assertRaises(CodexChildError) as ctx:
            runner.package({"run_id": "r1"}, timeout_s=1)
        self.assertEqual(ctx.exception.exit_code, -1)

    def test_failure_without_reason_uses_generic_message(self) -> None:
        def fake_runner(argv, **kwargs):
            return FakeCompletedProcess(1, b"")

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        with self.assertRaises(CodexChildError) as ctx:
            runner.cleanup({"run_id": "r1"})
        self.assertIn("cleanup", ctx.exception.reason)


# ---------------------------------------------------------------------------
# run_exec against a hand-rolled fake Popen
# ---------------------------------------------------------------------------


class _FakeStream:
    """Iterable line source mimicking a Popen text-mode pipe."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()

    def push(self, line: str) -> None:
        self._queue.put(line)

    def close(self) -> None:
        self._queue.put(None)

    def __iter__(self) -> _FakeStream:
        return self

    def __next__(self) -> str:
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakePopen:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exited = threading.Event()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        # A killed process's pipes see EOF; a merely-terminated one might not
        # (it can ignore SIGTERM), which is exactly the distinction the
        # SIGTERM-then-SIGKILL tests below rely on.
        self.stdout.close()
        self.stderr.close()
        self._exited.set()

    def finish(self, returncode: int = 0) -> None:
        """Test helper: simulate the process exiting on its own."""
        self.returncode = returncode
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if not self._exited.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.returncode if self.returncode is not None else 0


def make_frame(**overrides) -> dict:
    frame = {
        "type": "agent_svc.result",
        "exit_code": 0,
        "timed_out": False,
        "idle_killed": False,
        "final_message": "done",
        "thread_id": "t1",
        "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        "stderr_tail": [],
    }
    frame.update(overrides)
    return frame


class FakePopenRunExecTests(unittest.TestCase):
    def test_events_forwarded_and_result_parsed(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        fake.stdout.push(json.dumps({"type": "item.completed", "index": 1}))
        fake.stdout.push(json.dumps(make_frame(final_message="all good")))
        fake.finish(0)

        runner = CodexRunner(popen=lambda *a, **k: fake)
        events: list[dict] = []
        result = runner.run_exec({"timeout_s": 10}, on_event=events.append)

        self.assertEqual(
            [event["type"] for event in events], ["thread.started", "item.completed"]
        )
        self.assertIsInstance(result, CodexResult)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.final_message, "all good")
        self.assertEqual(
            result.usage, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}
        )
        self.assertIsNotNone(result.frame)
        self.assertEqual(
            fake.stdin.written, [json.dumps({"timeout_s": 10}, ensure_ascii=False)]
        )
        self.assertTrue(fake.stdin.closed)

    def test_heartbeat_false_triggers_cancel_and_sigterm(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        # No frame ever arrives: the fake codex process is "hung".

        runner = CodexRunner(popen=lambda *a, **k: fake, sigkill_grace_s=0.3)
        calls: list[int] = []

        def heartbeat() -> bool:
            calls.append(1)
            return len(calls) < 2

        result = runner.run_exec(
            {"timeout_s": 30},
            on_event=lambda _e: None,
            heartbeat=heartbeat,
            heartbeat_interval_s=0.05,
        )

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.frame)
        self.assertGreaterEqual(len(calls), 2)

    def test_cancel_event_triggers_cancel(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        cancel = threading.Event()
        cancel.set()

        runner = CodexRunner(popen=lambda *a, **k: fake, sigkill_grace_s=0.2)
        result = runner.run_exec({"timeout_s": 30}, on_event=lambda _e: None, cancel=cancel)

        self.assertTrue(fake.terminated)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)

    def test_hard_wall_timeout_kills_and_reports_timed_out(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))

        runner = CodexRunner(
            popen=lambda *a, **k: fake, hard_wall_safety_s=0.15, sigkill_grace_s=0.15
        )
        start = time.monotonic()
        result = runner.run_exec({"timeout_s": 0.1}, on_event=lambda _e: None)
        elapsed = time.monotonic() - start

        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertIsNone(result.frame)
        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)
        self.assertLess(elapsed, 5)

    def test_run_exec_rejects_missing_or_bad_timeout(self) -> None:
        def must_not_be_called(*args, **kwargs):
            raise AssertionError("popen should not be called when timeout_s is invalid")

        runner = CodexRunner(popen=must_not_be_called)
        with self.assertRaises(ValueError):
            runner.run_exec({}, on_event=lambda _e: None)
        with self.assertRaises(ValueError):
            runner.run_exec({"timeout_s": 0}, on_event=lambda _e: None)
        with self.assertRaises(ValueError):
            runner.run_exec({"timeout_s": True}, on_event=lambda _e: None)

    def test_stderr_tail_is_bounded(self) -> None:
        fake = FakePopen()
        for index in range(50):
            # The marker goes first: truncation to 500 chars keeps the
            # prefix, so a marker at the end of a >500-char line would be
            # the part that gets cut off instead of proving anything.
            fake.stderr.push(f"line-{index}-" + ("x" * 600))
        fake.stdout.push(json.dumps(make_frame(stderr_tail=[])))
        fake.finish(0)

        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec({"timeout_s": 10}, on_event=lambda _e: None)

        self.assertEqual(len(result.stderr_tail), 40)
        for line in result.stderr_tail:
            self.assertLessEqual(len(line), 500)
        # The bounded deque keeps the most recent lines.
        self.assertTrue(result.stderr_tail[-1].startswith("line-49-"))
        self.assertTrue(result.stderr_tail[0].startswith("line-10-"))

    def test_non_dict_and_unparseable_lines_are_ignored(self) -> None:
        fake = FakePopen()
        fake.stdout.push("not json at all")
        fake.stdout.push(json.dumps([1, 2, 3]))
        fake.stdout.push(json.dumps(make_frame()))
        fake.finish(0)

        events: list[dict] = []
        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec({"timeout_s": 10}, on_event=events.append)

        self.assertEqual(events, [])
        self.assertIsNotNone(result.frame)


# ---------------------------------------------------------------------------
# End-to-end: real codex_child.py (test mode) driving fake_codex.py
# ---------------------------------------------------------------------------


class EndToEndFakeCodexTests(ChildProcessTestCase):
    def make_runner(self, **kwargs) -> CodexRunner:
        return CodexRunner(
            command_prefix=[],
            python_bin=PYTHON_BIN,
            libexec_dir=str(LIBEXEC_DIR),
            sigkill_grace_s=2.0,
            **kwargs,
        )

    def write_control(self, run_id: str, control: dict) -> None:
        tmp_dir = self.work_root / run_id / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "fake_codex_control.json").write_text(json.dumps(control), encoding="utf-8")

    def test_events_forwarded_and_result_parsed(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id,
            {
                "scenario": "normal",
                "thread_id": "e2e-1",
                "final_message": "hi there",
                "tokens": 40,
            },
        )
        request = self.base_exec_request(run_id, timeout_s=15, idle_timeout_s=10)

        events: list[dict] = []
        with patch.dict("os.environ", self.child_env(), clear=True):
            result = self.make_runner().run_exec(request, on_event=events.append)

        self.assertIn("thread.started", [event["type"] for event in events])
        self.assertIn("turn.completed", [event["type"] for event in events])
        self.assertFalse(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.final_message, "hi there")
        self.assertEqual(
            result.usage, {"input_tokens": 40, "cached_input_tokens": 0, "output_tokens": 10}
        )

    def test_heartbeat_false_cancels(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id, {"scenario": "hang", "sleep_s": 60, "thread_id": "e2e-hang"}
        )
        request = self.base_exec_request(run_id, timeout_s=30, idle_timeout_s=30)

        calls: list[int] = []

        def heartbeat() -> bool:
            calls.append(1)
            return len(calls) < 2

        with patch.dict("os.environ", self.child_env(), clear=True):
            start = time.monotonic()
            result = self.make_runner().run_exec(
                request,
                on_event=lambda _e: None,
                heartbeat=heartbeat,
                heartbeat_interval_s=0.2,
            )
            elapsed = time.monotonic() - start

        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertLess(elapsed, 20)

    def test_timeout_reported_from_child(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id, {"scenario": "hang", "sleep_s": 60, "thread_id": "e2e-timeout"}
        )
        request = self.base_exec_request(run_id, timeout_s=1, idle_timeout_s=30)

        with patch.dict("os.environ", self.child_env(), clear=True):
            start = time.monotonic()
            result = self.make_runner().run_exec(request, on_event=lambda _e: None)
            elapsed = time.monotonic() - start

        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertLess(elapsed, 20)


if __name__ == "__main__":
    unittest.main()
