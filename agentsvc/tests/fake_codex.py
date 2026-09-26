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
                       "huge_stdout" (default "normal")
  thread_id           id reported in `thread.started` (default "fake-parent-thread")
  parent_thread_id    if set, this run's own rollout claims to be a sub-agent
                       spawned by that parent thread id
  child_thread_id     if set, also write a second rollout file whose
                       session_meta names this run as ITS parent
  tokens              input_tokens recorded for this run's rollout (default 1000)
  child_tokens        input_tokens recorded for the child rollout (default 500)
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


def _write_rollout(thread_id: str, *, parent_thread_id: str | None, tokens: int) -> None:
    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home:
        return
    sessions_dir = Path(codex_home) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    rollout = sessions_dir / f"rollout-test-{thread_id}.jsonl"
    session_meta: dict = {"type": "session_meta", "payload": {"id": thread_id}}
    if parent_thread_id is not None:
        session_meta["payload"]["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": parent_thread_id}}
        }
    lines = [
        session_meta,
        {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": max(tokens // 4, 1),
                }
            },
        },
    ]
    with rollout.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


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
    parent_thread_id = control.get("parent_thread_id") or os.environ.get(
        "FAKE_CODEX_PARENT_THREAD_ID"
    )
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

    if scenario == "huge_stdout":
        _emit({"type": "thread.started", "thread_id": thread_id})
        for index in range(line_count):
            _emit({"type": "item.completed", "index": index, "pad": "x" * 200})
        if final_path is not None:
            final_path.write_text("done", encoding="utf-8")
        _write_rollout(thread_id, parent_thread_id=None, tokens=tokens)
        return exit_code

    # "normal"
    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "item.completed", "item": {"type": "agentMessage", "text": "working"}})
    usage = {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}
    _emit({"type": "turn.completed", "usage": usage})
    if final_path is not None:
        final_path.write_text(final_message, encoding="utf-8")
    _write_rollout(thread_id, parent_thread_id=parent_thread_id, tokens=tokens)
    if child_thread_id:
        _write_rollout(child_thread_id, parent_thread_id=thread_id, tokens=child_tokens)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
