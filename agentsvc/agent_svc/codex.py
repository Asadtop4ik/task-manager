"""Parent-side driver for the sandboxed Codex child (`libexec/codex_child.py`).

Runs as the `agent-svc` user. Every call shells out to
`sudo -n -u agent-codex -- /usr/bin/python3 -I <libexec>/codex_child.py <cmd>`
(the command prefix and interpreter/script path are constructor arguments so
tests can run the child directly with the current Python interpreter,
bypassing sudo; `-I`, isolated mode, is always inserted regardless -- it
ignores PYTHON*/site customization even if the caller's own environment
somehow carried any, matching the pinned sudoers command exactly). This
module never reads secrets and never has any to leak: it only forwards the
request the caller builds and the events the child prints.
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SUDO_PREFIX = ["/usr/bin/sudo", "-n", "-u", "agent-codex", "--"]
DEFAULT_PYTHON_BIN = "/usr/bin/python3"
DEFAULT_LIBEXEC_DIR = "/opt/agent-svc/libexec"

HARD_WALL_SAFETY_S = 60.0
# A generous grace period: SIGKILL must never be the first signal we send
# (always SIGTERM, then wait), and a real codex process legitimately needs
# time to unwind (tree-kill its own sandboxed children, flush the rollout,
# print its final frame) before it can exit on SIGTERM.
SIGKILL_GRACE_S = 30.0
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
HEARTBEAT_FAILURE_THRESHOLD = 3
MAX_STDERR_LINES = 40
MAX_STDERR_LINE_CHARS = 500
MAX_LINE_BYTES = 1024 * 1024
STDOUT_QUEUE_MAXSIZE = 8192
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


class LeaseLost(Exception):
    """A `heartbeat` callback raises this to force immediate cancellation
    (e.g. the run's lease was stolen or explicitly revoked), bypassing the
    normal debounce for transient heartbeat failures."""


class CodexChildError(RuntimeError):
    """Raised when `prepare`/`package`/`preflight`/`cleanup` exit non-zero or
    return unusable JSON. `body` is the full parsed JSON the child printed on
    a clean (well-formed) refusal, when there is one -- `preflight` uses it
    to carry `preflight_failure`, the trusted script's own failure text,
    alongside the generic `reason` every subcommand reports."""

    def __init__(
        self, exit_code: int, reason: str, *, body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.reason = reason
        self.body = body


@dataclass(frozen=True)
class CodexResult:
    exit_code: int
    timed_out: bool
    cancelled: bool
    idle_killed: bool
    final_message: str
    usage: dict[str, int] | None
    stderr_tail: list[str]
    frame: dict[str, Any] | None


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _put_important(sink: queue.Queue[Any], item: Any) -> None:
    while True:
        try:
            sink.put_nowait(item)
            return
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                sink.get_nowait()


def _pump_stdout(stream: Any, sink: queue.Queue[str | None]) -> None:
    try:
        while True:
            raw_line = stream.readline(MAX_LINE_BYTES)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            with contextlib.suppress(queue.Full):
                sink.put_nowait(text)
    except (OSError, ValueError):
        pass
    finally:
        _put_important(sink, None)


def _pump_stderr(stream: Any, sink: deque[str]) -> None:
    try:
        while True:
            raw_line = stream.readline(MAX_LINE_BYTES)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            sink.append(text[:MAX_STDERR_LINE_CHARS])
    except (OSError, ValueError):
        pass


def _is_valid_result_frame(event: dict[str, Any]) -> bool:
    """Type-check a candidate `agent_svc.result` frame before trusting any of
    its fields. The child is supposed to be the only source of this frame
    (and now refuses to forward a forged one of its own), but a parent that
    blindly trusts field types is still one bug away from a crash or a
    spoofed result; a malformed frame is treated as no frame at all."""
    exit_code = event.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return False
    for key in ("timed_out", "idle_killed"):
        if not isinstance(event.get(key), bool):
            return False
    if not isinstance(event.get("final_message"), str):
        return False
    usage = event.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            return False
        if not all(isinstance(key, str) for key in usage):
            return False
    stderr_tail = event.get("stderr_tail")
    if not isinstance(stderr_tail, list) or not all(isinstance(x, str) for x in stderr_tail):
        return False
    thread_id = event.get("thread_id")
    return thread_id is None or isinstance(thread_id, str)


def _frame_usage(event: dict[str, Any]) -> dict[str, int] | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    return {key: _safe_int(usage.get(key)) for key in USAGE_KEYS}


class CodexRunner:
    """Spawns the codex child under sudo and speaks its stdin/stdout protocol."""

    def __init__(
        self,
        *,
        command_prefix: list[str] | None = None,
        python_bin: str = DEFAULT_PYTHON_BIN,
        libexec_dir: str | Path = DEFAULT_LIBEXEC_DIR,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        hard_wall_safety_s: float = HARD_WALL_SAFETY_S,
        sigkill_grace_s: float = SIGKILL_GRACE_S,
    ) -> None:
        self._command_prefix = list(
            DEFAULT_SUDO_PREFIX if command_prefix is None else command_prefix
        )
        self._python_bin = python_bin
        self._child_script = Path(libexec_dir) / "codex_child.py"
        self._runner = runner
        self._popen = popen
        # Not overridable in production (no env/test-mode gate needed here:
        # unlike codex_child.py, CodexRunner never crosses a privilege
        # boundary, so plain constructor defaults are safe); tests shrink
        # these to keep the timeout/SIGKILL-grace suite fast.
        self._hard_wall_safety_s = hard_wall_safety_s
        self._sigkill_grace_s = sigkill_grace_s

    def _argv(self, subcommand: str) -> list[str]:
        # `-I` (isolated mode): no PYTHONPATH/user site/.pth files, matching
        # the exact command sudoers pins in production.
        return [
            *self._command_prefix,
            self._python_bin,
            "-I",
            str(self._child_script),
            subcommand,
        ]

    def _simple_call(
        self, subcommand: str, request: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        argv = self._argv(subcommand)
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        try:
            completed = self._runner(
                argv,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexChildError(-1, f"codex child {subcommand} timed out") from exc
        stdout_text = completed.stdout.decode("utf-8", "replace").strip()
        try:
            body: Any = json.loads(stdout_text) if stdout_text else None
        except json.JSONDecodeError:
            body = None
        if completed.returncode != 0:
            reason = body.get("reason") if isinstance(body, dict) else None
            raise CodexChildError(
                completed.returncode,
                reason or f"codex child {subcommand} failed",
                body=body if isinstance(body, dict) else None,
            )
        if not isinstance(body, dict):
            raise CodexChildError(
                completed.returncode, f"codex child {subcommand} returned invalid JSON"
            )
        return body

    def prepare(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        return self._simple_call("prepare", request, timeout_s)

    def package(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        return self._simple_call("package", request, timeout_s)

    def preflight(
        self, request: dict[str, Any], *, timeout_s: float = 180.0
    ) -> dict[str, Any]:
        return self._simple_call("preflight", request, timeout_s)

    def cleanup(self, request: dict[str, Any], *, timeout_s: float = 60.0) -> dict[str, Any]:
        return self._simple_call("cleanup", request, timeout_s)

    def run_exec(
        self,
        request: dict[str, Any],
        *,
        on_event: Callable[[dict[str, Any]], None],
        heartbeat: Callable[[], bool] | None = None,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        cancel: threading.Event | None = None,
    ) -> CodexResult:
        timeout_s = request.get("timeout_s")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or timeout_s <= 0
        ):
            raise ValueError("request.timeout_s must be a positive number")
        hard_wall_s = float(timeout_s) + self._hard_wall_safety_s

        argv = self._argv("exec")
        process = self._popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert (
            process.stdin is not None
            and process.stdout is not None
            and process.stderr is not None
        )
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                process.stdin.close()

        lines: queue.Queue[str | None] = queue.Queue(maxsize=STDOUT_QUEUE_MAXSIZE)
        stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)
        reader = threading.Thread(
            target=_pump_stdout, args=(process.stdout, lines), daemon=True
        )
        stderr_reader = threading.Thread(
            target=_pump_stderr, args=(process.stderr, stderr_tail), daemon=True
        )
        reader.start()
        stderr_reader.start()

        stop_heartbeat = threading.Event()
        heartbeat_failed = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        if heartbeat is not None:

            def _heartbeat_loop() -> None:
                consecutive_failures = 0
                while not stop_heartbeat.wait(heartbeat_interval_s):
                    try:
                        ok = heartbeat()
                    except LeaseLost:
                        heartbeat_failed.set()
                        return
                    except Exception:
                        ok = False
                    if ok:
                        consecutive_failures = 0
                        continue
                    # A single transient failure (a blip in the lease check
                    # itself, a momentary network error) must not cancel a
                    # run that is otherwise healthy; only debounced repeats
                    # do.
                    consecutive_failures += 1
                    if consecutive_failures >= HEARTBEAT_FAILURE_THRESHOLD:
                        heartbeat_failed.set()
                        return

            heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
            heartbeat_thread.start()

        deadline = time.monotonic() + hard_wall_s
        frame: dict[str, Any] | None = None
        terminate_reason: str | None = None
        grace_deadline: float | None = None

        try:
            while True:
                now = time.monotonic()
                if grace_deadline is not None:
                    if now >= grace_deadline:
                        process.kill()
                        break
                    poll = min(max(grace_deadline - now, 0.05), 0.5)
                else:
                    if now >= deadline:
                        terminate_reason = "timed_out"
                    elif (cancel is not None and cancel.is_set()) or heartbeat_failed.is_set():
                        terminate_reason = "cancelled"
                    if terminate_reason is not None:
                        self._send_sigterm(process)
                        grace_deadline = time.monotonic() + self._sigkill_grace_s
                        continue
                    poll = min(max(deadline - now, 0.05), 0.5)
                try:
                    item = lines.get(timeout=poll)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    event = json.loads(item)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "agent_svc.result":
                    # Keep reading to EOF and remember the LAST valid frame,
                    # rather than trusting (and stopping at) the first one:
                    # defense in depth against a bug or a forged frame
                    # slipping through, on top of the child's own filter.
                    if _is_valid_result_frame(event):
                        frame = event
                    continue
                on_event(event)
        finally:
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            # Never SIGKILL first: if the process is still alive for any
            # reason by the time we get here (including an exception above
            # that skipped the loop's own SIGTERM+grace handling), always
            # try SIGTERM and wait before escalating.
            if process.poll() is None:
                self._send_sigterm(process)
                try:
                    process.wait(timeout=self._sigkill_grace_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=5)
            else:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            reader.join(timeout=1)
            stderr_reader.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()

        exit_code = process.returncode if process.returncode is not None else -1
        cancelled = terminate_reason == "cancelled"
        timed_out = terminate_reason == "timed_out"
        if frame is not None:
            return CodexResult(
                exit_code=_safe_int(frame.get("exit_code"), exit_code),
                timed_out=bool(frame.get("timed_out", False)) or timed_out,
                cancelled=cancelled,
                idle_killed=bool(frame.get("idle_killed", False)),
                final_message=str(frame.get("final_message", "")),
                usage=_frame_usage(frame),
                stderr_tail=list(stderr_tail),
                frame=frame,
            )
        return CodexResult(
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            idle_killed=False,
            final_message="",
            usage=None,
            stderr_tail=list(stderr_tail),
            frame=None,
        )

    @staticmethod
    def _send_sigterm(process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            process.terminate()


def _main() -> None:  # pragma: no cover - manual smoke helper, not exercised by tests
    print("agent_svc.codex is a library module; nothing to run directly.", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    _main()
