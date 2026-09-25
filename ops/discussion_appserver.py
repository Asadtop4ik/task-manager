"""Small JSONL client for one read-only Codex app-server turn."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class DiscussionError(RuntimeError):
    pass


CODEX_BINARY = "/home/codex-runner/.local/bin/codex"
CODEX_PATH = (
    "/home/codex-runner/actions-runner/externals/node24/bin:"
    "/home/codex-runner/.local/bin:/usr/local/bin:/usr/bin:/bin"
)
DIAGNOSTIC_PROXY = "/opt/task-manager/ops/diagnostic_proxy.py"
DIAGNOSTIC_SOCKET = "/run/task-manager-diagnostics/diagnostics.sock"


def _app_server_command(
    diagnostics_discussion_id: int | None, diagnostics_lease_id: str | None
) -> list[str]:
    command = [CODEX_BINARY, "app-server", "--stdio"]
    if diagnostics_discussion_id is not None:
        if not diagnostics_lease_id:
            raise DiscussionError("diagnostic lease capability is required")
        proxy_args = json.dumps([
            DIAGNOSTIC_PROXY,
            "--discussion-id",
            str(diagnostics_discussion_id),
            "--lease-id",
            diagnostics_lease_id,
            "--socket",
            DIAGNOSTIC_SOCKET,
        ], separators=(",", ":"))
        command.extend([
            "--config", 'mcp_servers.ketoshop_diagnostics.command="/usr/bin/python3"',
            "--config",
            f"mcp_servers.ketoshop_diagnostics.args={proxy_args}",
        ])
    return command


def _messages(stdout: Any, output: queue.Queue[Any]) -> None:
    try:
        for line in stdout:
            if line.strip():
                output.put(json.loads(line))
    except (OSError, ValueError):
        pass
    finally:
        output.put(None)


def run_turn(
    *,
    snapshot: Path,
    thread_id: str | None,
    prompt: str,
    images: list[Path],
    diagnostics_discussion_id: int | None = None,
    diagnostics_lease_id: str | None = None,
    timeout: int = 150,
) -> tuple[str, str]:
    """Return the persisted thread id and final Uzbek answer, without logs."""
    if not snapshot.is_dir() or any(not image.is_file() for image in images):
        raise DiscussionError("invalid project snapshot or image")
    env = {
        "HOME": "/home/codex-runner",
        "CODEX_HOME": "/home/codex-runner/.codex",
        "PATH": CODEX_PATH,
        "LANG": "C.UTF-8",
    }
    if (diagnostics_discussion_id is None) != (diagnostics_lease_id is None):
        raise DiscussionError("diagnostic discussion and lease must be paired")
    process = subprocess.Popen(
        _app_server_command(diagnostics_discussion_id, diagnostics_lease_id),
        cwd=snapshot,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    output: queue.Queue[Any] = queue.Queue()
    reader = threading.Thread(target=_messages, args=(process.stdout, output), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(method: str, request_id: int | None, params: dict[str, Any]) -> None:
        value: dict[str, Any] = {"method": method, "params": params}
        if request_id is not None:
            value["id"] = request_id
        process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def read() -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DiscussionError("Codex javobi vaqtida kelmadi")
        try:
            message = output.get(timeout=remaining)
        except queue.Empty:
            raise DiscussionError("Codex javobi vaqtida kelmadi") from None
        if message is None or not isinstance(message, dict):
            raise DiscussionError("Codex suhbat aloqasi uzildi")
        return message

    def response(request_id: int) -> dict[str, Any]:
        while True:
            message = read()
            if "id" in message and "method" in message:
                raise DiscussionError("Codex qo‘shimcha ruxsat so‘radi")
            if message.get("id") == request_id:
                if "error" in message:
                    raise DiscussionError("Codex suhbatni boshlay olmadi")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise DiscussionError("Codex noto‘g‘ri javob berdi")
                return result

    try:
        send("initialize", 1, {"clientInfo": {
            "name": "task_manager_discussion", "title": "Task Manager discussion", "version": "1.0.0"
        }})
        response(1)
        send("initialized", None, {})
        if thread_id:
            send("thread/resume", 2, {"threadId": thread_id, "cwd": str(snapshot)})
        else:
            send("thread/start", 2, {
                "model": "gpt-6-sol", "cwd": str(snapshot), "approvalPolicy": "never",
                "sandbox": "read-only", "serviceName": "task_manager_discussion",
            })
        opened = response(2).get("thread")
        if not isinstance(opened, dict) or not isinstance(opened.get("id"), str):
            raise DiscussionError("Codex suhbatni saqlay olmadi")
        saved_thread_id = opened["id"]
        inputs: list[dict[str, str]] = [{"type": "text", "text": prompt}]
        inputs.extend({"type": "localImage", "path": str(image)} for image in images)
        send("turn/start", 3, {
            "threadId": saved_thread_id,
            "input": inputs,
            "cwd": str(snapshot),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly"},
            "model": "gpt-6-sol", "effort": "medium", "summary": "concise",
        })
        response(3)
        answer = ""
        while True:
            message = read()
            if message.get("method") == "item/completed":
                item = (message.get("params") or {}).get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                    answer = str(item.get("text") or "")
            elif message.get("method") == "turn/completed":
                turn = (message.get("params") or {}).get("turn") or {}
                if turn.get("status") != "completed" or not answer.strip():
                    raise DiscussionError("Codex suhbat javobini tugata olmadi")
                return saved_thread_id, answer.strip()[:4000]
            elif "id" in message and "method" in message:
                # Never grant tool or network approvals to an automated chat.
                raise DiscussionError("Codex qo‘shimcha ruxsat so‘radi")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
