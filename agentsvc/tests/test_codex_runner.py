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
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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
                "-I",
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
            ["/usr/bin/python3.12", "-I", "/scratch/libexec/codex_child.py", "exec"],
        )

    def test_argv_always_includes_isolated_mode_flag(self) -> None:
        # `-I`: no PYTHONPATH/user site/.pth files, matching the exact
        # command sudoers pins in production, regardless of prefix/python_bin.
        runner = CodexRunner(command_prefix=[], python_bin="python3", libexec_dir="/x")
        self.assertIn("-I", runner._argv("cleanup"))

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
        self.assertEqual(argv, ["python3", "-I", "/x/codex_child.py", "prepare"])
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

    def test_preflight_success(self) -> None:
        # `preflight` now goes through `_call_with_grace` (Popen +
        # `communicate`, for the graceful-timeout behavior below), not the
        # `subprocess.run`-shaped `_simple_call` -- hence a `FakePopen`, not
        # a `fake_runner`.
        fake = FakePopen()
        fake.stdout.push(
            '{"ok": true, "patch_b64": "", "changed_paths": [], "preflight_result": "ok"}'
        )
        fake.finish(0)

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", popen=lambda *a, **k: fake)
        result = runner.preflight({"run_id": "r1"})
        self.assertEqual(result["preflight_result"], "ok")

    def test_preflight_failure_carries_the_trusted_failure_text_in_body(self) -> None:
        fake = FakePopen()
        fake.stdout.push(
            '{"reason": "trusted preflight failed", '
            '"preflight_failure": "Ruff tekshiruvi xato berdi"}'
        )
        fake.finish(3)

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", popen=lambda *a, **k: fake)
        with self.assertRaises(CodexChildError) as ctx:
            runner.preflight({"run_id": "r1"})
        self.assertEqual(ctx.exception.reason, "trusted preflight failed")
        assert ctx.exception.body is not None
        self.assertEqual(ctx.exception.body["preflight_failure"], "Ruff tekshiruvi xato berdi")

    def test_preflight_timeout_sigterms_then_waits_grace_before_sigkill(self) -> None:
        # A hung/SIGTERM-ignoring child must see SIGTERM (relayed through
        # `sudo`) and a full grace period BEFORE SIGKILL -- never SIGKILL
        # first, matching `run_exec`'s own termination pattern (see
        # `_call_with_grace`'s docstring).
        fake = FakePopen()  # never `finish()`es on its own: simulates a hang

        runner = CodexRunner(
            command_prefix=[],
            libexec_dir="/x",
            popen=lambda *a, **k: fake,
            sigkill_grace_s=0.05,
        )
        with self.assertRaises(CodexChildError) as ctx:
            runner.preflight({"run_id": "r1"}, timeout_s=0.05)
        self.assertEqual(ctx.exception.exit_code, -1)
        self.assertIn("timed out", ctx.exception.reason)
        self.assertTrue(fake.terminated, "SIGTERM was never sent on timeout")
        self.assertTrue(
            fake.killed, "a child still alive after the grace period must be killed"
        )

    def test_child_error_body_is_none_without_a_parseable_json_body(self) -> None:
        def fake_runner(argv, **kwargs):
            return FakeCompletedProcess(1, b"not json at all")

        runner = CodexRunner(command_prefix=[], libexec_dir="/x", runner=fake_runner)
        with self.assertRaises(CodexChildError) as ctx:
            runner.cleanup({"run_id": "r1"})
        self.assertIsNone(ctx.exception.body)

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
    """Binary readline()-based line source, mimicking a Popen binary-mode
    pipe (matching `CodexRunner.run_exec`'s real Popen, which no longer
    passes `text=True` -- see item 14: readline(limit) + explicit decode)."""

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()

    def push(self, line: str) -> None:
        data = line if line.endswith("\n") else line + "\n"
        self._queue.put(data.encode("utf-8"))

    def close(self) -> None:
        self._queue.put(None)

    def readline(self, limit: int = -1) -> bytes:
        item = self._queue.get()
        return b"" if item is None else item


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
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
        """Test helper: simulate the process exiting on its own -- which, in
        a real process, also closes its stdout/stderr, producing EOF for the
        reader threads. `run_exec` now reads all the way to EOF rather than
        stopping at the first `agent_svc.result` line (it wants the LAST one
        seen), so a fake that never signals EOF would hang it until the hard
        wall timeout instead of returning promptly."""
        self.returncode = returncode
        self.stdout.close()
        self.stderr.close()
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if not self._exited.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.returncode if self.returncode is not None else 0

    def poll(self) -> int | None:
        return self.returncode

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        """Mimics `subprocess.Popen.communicate` closely enough for
        `_call_with_grace`'s tests: write+close stdin, block for exit (like
        `wait`), then drain the now-EOF'd fake stdout/stderr streams."""
        if input is not None:
            self.stdin.write(input)
        self.stdin.close()
        if self.returncode is None and not self._exited.wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

        def _drain(stream: _FakeStream) -> bytes:
            data = b""
            while True:
                chunk = stream.readline()
                if chunk == b"":
                    return data
                data += chunk

        return _drain(self.stdout), _drain(self.stderr)


class _RespondsToSigtermPopen(FakePopen):
    """Simulates a process that exits promptly on SIGTERM (the common case
    for `discussion`'s app-server child): plain `FakePopen.terminate()` only
    sets a flag, never actually ending the process, which would leave a
    cancel test unable to tell "SIGTERM sent" apart from "the full outer
    timeout simply elapsed"."""

    def terminate(self) -> None:
        super().terminate()
        self.finish(-15)


class RunDiscussionCancelTests(unittest.TestCase):
    def test_cancel_ends_the_call_well_before_the_full_timeout(self) -> None:
        fake = _RespondsToSigtermPopen()
        cancel = threading.Event()
        runner = CodexRunner(command_prefix=[], libexec_dir="/x", popen=lambda *a, **k: fake)

        def _cancel_soon() -> None:
            time.sleep(0.05)
            cancel.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(CodexChildError) as ctx:
            runner.run_discussion({"run_id": "r1"}, timeout_s=30, cancel=cancel)
        elapsed = time.monotonic() - started

        self.assertTrue(fake.terminated, "the cancel watcher never SIGTERMed the child")
        self.assertLess(elapsed, 5.0)  # far under the 30s outer timeout_s
        self.assertEqual(ctx.exception.exit_code, -15)

    def test_no_cancel_event_means_no_watcher_and_normal_success(self) -> None:
        fake = FakePopen()
        fake.stdout.push('{"ok": true, "thread_id": "t1", "response": "hi"}')
        fake.finish(0)
        runner = CodexRunner(command_prefix=[], libexec_dir="/x", popen=lambda *a, **k: fake)

        result = runner.run_discussion({"run_id": "r1"}, timeout_s=5)

        self.assertEqual(result, {"ok": True, "thread_id": "t1", "response": "hi"})
        self.assertFalse(fake.terminated)

    def test_run_discussion_uses_the_configured_sudo_group(self) -> None:
        captured: list[list[str]] = []

        def popen(argv, **kwargs):
            captured.append(list(argv))
            fake = FakePopen()
            fake.stdout.push('{"ok": true, "thread_id": "t1", "response": "hi"}')
            fake.finish(0)
            return fake

        runner = CodexRunner(
            command_prefix=["/usr/bin/sudo", "-n", "-u", "agent-codex", "--"],
            libexec_dir="/x",
            popen=popen,
            discussion_sudo_group="task-diag-client",
        )
        runner.run_discussion({"run_id": "r1"}, timeout_s=5)
        argv = captured[0]
        self.assertIn("-g", argv)
        self.assertEqual(argv[argv.index("-g") + 1], "task-diag-client")
        self.assertLess(argv.index("-g"), argv.index("--"))

    def test_other_subcommands_never_get_the_sudo_group(self) -> None:
        captured: list[list[str]] = []

        def fake_runner(argv, **kwargs):
            captured.append(list(argv))
            return FakeCompletedProcess(0, b'{"ok": true, "head": "' + b"a" * 40 + b'"}')

        runner = CodexRunner(
            command_prefix=["/usr/bin/sudo", "-n", "-u", "agent-codex", "--"],
            libexec_dir="/x",
            runner=fake_runner,
        )
        runner.prepare({"run_id": "r1"})
        self.assertNotIn("-g", captured[0])


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
            fake.stdin.written,
            [json.dumps({"timeout_s": 10}, ensure_ascii=False).encode("utf-8")],
        )
        self.assertTrue(fake.stdin.closed)

    def test_heartbeat_three_consecutive_failures_triggers_cancel(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        # No frame ever arrives: the fake codex process is "hung".

        runner = CodexRunner(popen=lambda *a, **k: fake, sigkill_grace_s=0.3)
        calls: list[int] = []

        def heartbeat() -> bool:
            calls.append(1)
            return False

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
        self.assertGreaterEqual(len(calls), 3)

    def test_heartbeat_transient_failure_alone_does_not_cancel(self) -> None:
        from agent_svc.codex import HEARTBEAT_FAILURE_THRESHOLD

        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        fake.stdout.push(json.dumps(make_frame(final_message="recovered")))
        fake.finish(0)

        calls: list[int] = []

        def heartbeat() -> bool:
            calls.append(1)
            # One fewer than the threshold, then healthy again: must never
            # cancel a run over an isolated blip.
            return not len(calls) < HEARTBEAT_FAILURE_THRESHOLD

        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec(
            {"timeout_s": 10},
            on_event=lambda _e: None,
            heartbeat=heartbeat,
            heartbeat_interval_s=0.05,
        )

        self.assertFalse(result.cancelled)
        self.assertEqual(result.final_message, "recovered")

    def test_lease_lost_cancels_immediately_bypassing_the_counter(self) -> None:
        from agent_svc.codex import LeaseLost

        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))

        def heartbeat() -> bool:
            raise LeaseLost("lease stolen")

        runner = CodexRunner(popen=lambda *a, **k: fake, sigkill_grace_s=0.3)
        result = runner.run_exec(
            {"timeout_s": 30},
            on_event=lambda _e: None,
            heartbeat=heartbeat,
            heartbeat_interval_s=0.05,
        )

        self.assertTrue(result.cancelled)
        self.assertTrue(fake.terminated)

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

    def test_reads_to_eof_and_uses_the_last_frame(self) -> None:
        # Defense in depth on top of the child's own anti-spoofing filter:
        # if more than one agent_svc.result line ever arrives, the LAST one
        # wins, not the first.
        fake = FakePopen()
        fake.stdout.push(json.dumps(make_frame(final_message="first", exit_code=1)))
        fake.stdout.push(json.dumps(make_frame(final_message="second", exit_code=0)))
        fake.finish(0)

        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec({"timeout_s": 10}, on_event=lambda _e: None)

        self.assertEqual(result.final_message, "second")
        self.assertEqual(result.exit_code, 0)

    def test_malformed_frame_is_treated_as_no_frame(self) -> None:
        fake = FakePopen()
        bad_frame = make_frame()
        bad_frame["exit_code"] = "not-an-int"
        fake.stdout.push(json.dumps(bad_frame))
        fake.finish(3)

        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec({"timeout_s": 10}, on_event=lambda _e: None)

        self.assertIsNone(result.frame)
        # Falls back to the process's own exit status instead of trusting
        # any field out of the malformed frame.
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.final_message, "")
        self.assertIsNone(result.usage)

    def test_frame_with_non_string_stderr_tail_entry_is_rejected(self) -> None:
        fake = FakePopen()
        bad_frame = make_frame(stderr_tail=["ok", 123])
        fake.stdout.push(json.dumps(bad_frame))
        fake.finish(0)

        runner = CodexRunner(popen=lambda *a, **k: fake)
        result = runner.run_exec({"timeout_s": 10}, on_event=lambda _e: None)

        self.assertIsNone(result.frame)

    def test_finally_always_sigterms_before_sigkill_even_on_exception(self) -> None:
        fake = FakePopen()
        fake.stdout.push(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        # No frame ever arrives, and nothing else drives the main loop's own
        # SIGTERM path (no cancel/heartbeat/timeout) -- the only way the
        # process gets signaled at all is `run_exec`'s `finally` block.

        def boom(_event: dict) -> None:
            raise RuntimeError("boom in on_event")

        runner = CodexRunner(popen=lambda *a, **k: fake, sigkill_grace_s=0.2)
        with self.assertRaises(RuntimeError):
            runner.run_exec({"timeout_s": 30}, on_event=boom)

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)


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
