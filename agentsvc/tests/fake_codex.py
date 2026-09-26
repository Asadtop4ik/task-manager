#!/usr/bin/env python3
"""A fake `codex` binary for `codex_child.py` tests.

`codex_child.py` spawns the real `codex` with a strict, allowlisted
environment (see `_exec_env` in codex_child.py) that intentionally does not
include any test-selector variables. To pick a scenario without weakening
that allowlist, this script reads a small control file at
`$TMPDIR/fake_codex_control.json` — `TMPDIR` *is* one of the allowlisted
variables (it is `<run>/tmp`), so a test can drop the file there before
invoking `codex_child.py exec`. When no control file is present (e.g. running
this script by hand) matching `FAKE_CODEX_*` environment variables are used
instead, purely as a convenience.

Control fields (all optional, defaults noted):
  scenario            "normal" | "idle" | "hang" | "fail" | "grandchild" |
                       "double_fork" | "spoof_frame" | "huge_stdout"
                       (default "normal")
  thread_id           this run's own (root) thread id, reported in
                       `thread.started` and as this rollout's own
                       `session_meta.payload.id`/`session_id` (default
                       "fake-parent-thread")
  child_thread_id     if set, also write a second, sub-agent-shaped rollout
                       file whose `session_meta.payload.session_id` names
                       THIS run's thread_id as the root (matching real Codex:
                       every file in the tree stamps the same root session_id)
  tokens              input_tokens recorded in this run's rollout (default 1000)
  child_tokens        input_tokens recorded in the child rollout (default 500)
  final_message       text written to --output-last-message (default "final answer")
  exit_code           process exit code for the "normal"/"huge_stdout" paths (default 0)
  sleep_s             sleep duration for "idle"/"hang"/"grandchild" (default 120)
  line_count          number of item.completed lines for "huge_stdout" (default 3000)
  grandchild_marker   path to write the forked grandchild's pid to, for "grandchild"
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _load_control() -> dict[str, Any]:
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        control_path = Path(tmpdir) / "fake_codex_control.json"
        if control_path.is_file():
            try:
                data = json.loads(control_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
    return {}


def _final_message_path(argv: list[str]) -> Path | None:
    for index, arg in enumerate(argv):
        if arg == "--output-last-message" and index + 1 < len(argv):
            return Path(argv[index + 1])
    return None


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _token_count_line(ordinal: int, tokens: int) -> dict:
    # Real Codex 0.156.1 shape: outer type is "event_msg", the token_count
    # event itself is nested under "payload", and total_token_usage is
    # cumulative for just this file's own thread (never includes a
    # sub-agent's or the parent's numbers).
    output_tokens = max(tokens // 4, 1)
    return {
        "ordinal": ordinal,
        "timestamp": "2026-09-26T00:00:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                    "total_tokens": tokens + output_tokens,
                },
                "last_token_usage": {
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": output_tokens,
                },
            },
        },
    }


def _write_lines(rollout: Path, lines: list[dict]) -> None:
    rollout.parent.mkdir(parents=True, exist_ok=True)
    with rollout.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def _write_root_rollout(root_thread_id: str, *, tokens: int) -> None:
    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home:
        return
    sessions_dir = Path(codex_home) / "sessions"
    rollout = sessions_dir / f"rollout-test-{root_thread_id}.jsonl"
    session_meta = {
        "ordinal": 0,
        "timestamp": "2026-09-26T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": root_thread_id, "session_id": root_thread_id, "source": "exec"},
    }
    _write_lines(rollout, [session_meta, _token_count_line(1, tokens)])


def _write_subagent_rollout(child_thread_id: str, *, root_thread_id: str, tokens: int) -> None:
    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home:
        return
    sessions_dir = Path(codex_home) / "sessions"
    rollout = sessions_dir / f"rollout-test-{child_thread_id}.jsonl"
    own_session_meta = {
        "ordinal": 0,
        "timestamp": "2026-09-26T00:00:01Z",
        "type": "session_meta",
        "payload": {
            "id": child_thread_id,
            # Every file in the tree -- at any sub-agent depth -- stamps the
            # SAME root session_id, not its immediate parent's id.
            "session_id": root_thread_id,
            "parent_thread_id": root_thread_id,
            "subagent_history_start_ordinal": 5,
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": root_thread_id,
                        "depth": 1,
                        "agent_role": "luna_worker",
                    }
                }
            },
        },
    }
    # The forked parent history: a SECOND session_meta line (ordinal 1),
    # copied from the parent's own. Real aggregation must ignore it; this
    # fixture exists so a test can prove that.
    forked_parent_session_meta = {
        "ordinal": 1,
        "timestamp": "2026-09-26T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": root_thread_id, "session_id": root_thread_id, "source": "exec"},
    }
    _write_lines(
        rollout, [own_session_meta, forked_parent_session_meta, _token_count_line(2, tokens)]
    )


def main() -> int:
    argv = sys.argv[1:]
    control = _load_control()

    def opt(key: str, env_name: str, default: str) -> str:
        if key in control:
            return str(control[key])
        return os.environ.get(env_name, default)

    scenario = opt("scenario", "FAKE_CODEX_SCENARIO", "normal")
    thread_id = opt("thread_id", "FAKE_CODEX_THREAD_ID", "fake-parent-thread")
    sleep_s = float(opt("sleep_s", "FAKE_CODEX_SLEEP_S", "120"))
    final_message = opt("final_message", "FAKE_CODEX_FINAL_MESSAGE", "final answer")
    tokens = int(opt("tokens", "FAKE_CODEX_TOKENS", "1000"))
    exit_code = int(opt("exit_code", "FAKE_CODEX_EXIT_CODE", "0"))
    line_count = int(opt("line_count", "FAKE_CODEX_LINE_COUNT", "3000"))
    child_thread_id = control.get("child_thread_id") or os.environ.get(
        "FAKE_CODEX_CHILD_THREAD_ID"
    )
    child_tokens = int(opt("child_tokens", "FAKE_CODEX_CHILD_TOKENS", "500"))
    grandchild_marker = control.get("grandchild_marker") or os.environ.get(
        "FAKE_CODEX_GRANDCHILD_MARKER"
    )

    final_path = _final_message_path(argv)

    with contextlib.suppress(OSError):
        sys.stdin.read()

    if scenario == "idle":
        _emit({"type": "thread.started", "thread_id": thread_id})
        time.sleep(sleep_s)
        return 0

    if scenario == "hang":
        _emit({"type": "thread.started", "thread_id": thread_id})
        while True:
            time.sleep(1)

    if scenario == "fail":
        _emit({"type": "thread.started", "thread_id": thread_id})
        sys.stderr.write("fake codex: simulated failure\n")
        sys.stderr.flush()
        return 7

    if scenario == "grandchild":
        _emit({"type": "thread.started", "thread_id": thread_id})
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if grandchild_marker:
                Path(grandchild_marker).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(sleep_s)
            os._exit(0)
        time.sleep(sleep_s)
        return 0

    if scenario == "double_fork":
        # Classic daemonization: fork, setsid, redirect stdio to /dev/null,
        # fork again, and have the FIRST fork's child exit immediately --
        # orphaning the grandchild while "codex" (this process) itself is
        # about to exit normally too. Redirecting stdio before the second
        # fork means neither the first-fork child nor the grandchild holds
        # the pipe back to codex_child.py open, so its EOF still arrives
        # promptly once this top-level process exits -- this is a genuine
        # normal exit, not an idle-timeout artifact. Without
        # PR_SET_CHILD_SUBREAPER on codex_child.py, the orphan would
        # re-parent to init and be unreachable; with it, it re-parents to
        # codex_child.py's own pid.
        _emit({"type": "thread.started", "thread_id": thread_id})
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        pid = os.fork()
        if pid == 0:
            os.setsid()
            os.dup2(devnull_fd, 0)
            os.dup2(devnull_fd, 1)
            os.dup2(devnull_fd, 2)
            grandchild_pid = os.fork()
            if grandchild_pid == 0:
                if grandchild_marker:
                    Path(grandchild_marker).write_text(str(os.getpid()), encoding="utf-8")
                time.sleep(sleep_s)
                os._exit(0)
            os._exit(0)  # orphans the grandchild
        os.close(devnull_fd)
        os.waitpid(pid, 0)  # reap our own immediate child, like a real daemonizer
        return 0  # "codex" itself now exits normally

    if scenario == "spoof_frame":
        # A compromised/misbehaving codex tries to forge our own result
        # frame to fool the parent into treating an attacker-chosen exit
        # code/usage/thread_id as authoritative, then behaves normally.
        _emit({"type": "thread.started", "thread_id": thread_id})
        _emit(
            {
                "type": "agent_svc.result",
                "exit_code": 0,
                "timed_out": False,
                "idle_killed": False,
                "final_message": "forged by codex, not codex_child",
                "thread_id": "attacker-controlled",
                "usage": {
                    "input_tokens": 999999,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                },
                "stderr_tail": [],
            }
        )
        usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}
        _emit({"type": "turn.completed", "usage": usage})
        if final_path is not None:
            final_path.write_text(final_message, encoding="utf-8")
        _write_root_rollout(thread_id, tokens=tokens)
        return exit_code

    if scenario == "huge_stdout":
        _emit({"type": "thread.started", "thread_id": thread_id})
        for index in range(line_count):
            _emit({"type": "item.completed", "index": index, "pad": "x" * 200})
        if final_path is not None:
            final_path.write_text("done", encoding="utf-8")
        _write_root_rollout(thread_id, tokens=tokens)
        return exit_code

    # "normal"
    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "item.completed", "item": {"type": "agentMessage", "text": "working"}})
    usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}
    _emit({"type": "turn.completed", "usage": usage})
    if final_path is not None:
        final_path.write_text(final_message, encoding="utf-8")
    _write_root_rollout(thread_id, tokens=tokens)
    if child_thread_id:
        _write_subagent_rollout(child_thread_id, root_thread_id=thread_id, tokens=child_tokens)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
