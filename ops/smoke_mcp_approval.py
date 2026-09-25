"""Exercise the read-only finance MCP tool without touching production data."""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import discussion_appserver
from discussion_appserver import run_turn


def _git_snapshot(snapshot: Path) -> None:
    source = snapshot / "SMOKE.txt"
    source.write_text("synthetic MCP approval smoke\n", encoding="utf-8")
    git_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Task Manager MCP Smoke",
        "GIT_AUTHOR_EMAIL": "mcp-smoke@localhost",
        "GIT_COMMITTER_NAME": "Task Manager MCP Smoke",
        "GIT_COMMITTER_EMAIL": "mcp-smoke@localhost",
    }
    for command in (
        ["git", "init", "--quiet", str(snapshot)],
        ["git", "-C", str(snapshot), "add", "--all"],
        ["git", "-C", str(snapshot), "commit", "--quiet", "-m", "MCP smoke"],
    ):
        subprocess.run(command, env=git_env, check=True, capture_output=True)


def _synthetic_finance_result(marker: str) -> dict[str, Any]:
    return {
        "source": "synthetic_finance_fixture",
        "period": "day",
        "timezone": "Asia/Tashkent",
        "captured_at": "2026-09-25T12:00:00+00:00",
        "bucket_count": 1,
        "buckets": [
            {
                "bucket": "2026-09-25",
                "order_count": 1,
                "revenue": "10.00",
                "expenses": "2.00",
                "current_catalog_cost": "3.00",
                "missing_cost_items": 1,
            }
        ],
        "cost_basis": "current_catalog",
        "historical_cost_data_available": False,
        "verified_version": {
            "git_commit": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "verified": True,
        },
        "fixture_marker": marker,
    }


def main() -> None:
    marker = f"SYNTHETIC_FINANCE_{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix="taskmgr-mcp-approval-") as temporary:
        root = Path(temporary)
        snapshot = root / "snapshot"
        snapshot.mkdir()
        _git_snapshot(snapshot)
        socket_path = root / "diagnostics.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        received: list[dict[str, Any]] = []

        def respond() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    payload = bytearray()
                    while len(payload) <= 8192:
                        chunk = connection.recv(4096)
                        if not chunk or b"\n" in chunk:
                            if chunk:
                                payload.extend(chunk.split(b"\n", 1)[0])
                            break
                        payload.extend(chunk)
                    request = json.loads(payload)
                    received.append(request)
                    response = {
                        "ok": True,
                        "result": _synthetic_finance_result(marker),
                    }
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode() + b"\n"
                    )
            finally:
                server.close()

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        # Exercise the real proxy and its tool annotations, backed by an isolated
        # synthetic socket instead of the production diagnostics broker.
        discussion_appserver.DIAGNOSTIC_PROXY = str(
            Path(__file__).with_name("diagnostic_proxy.py")
        )
        discussion_appserver.DIAGNOSTIC_SOCKET = str(socket_path)
        _thread_id, answer = run_turn(
            snapshot=snapshot,
            thread_id=None,
            prompt=(
                "Call the ketoshop_finance_summary tool with period=day and count=1. "
                "Use only the returned synthetic fixture. Include its fixture_marker "
                "and source in your short answer. Do not invent or request real data."
            ),
            images=[],
            diagnostics_discussion_id=1,
            diagnostics_lease_id="synthetic-mcp-approval-capability",
            timeout=90,
        )
        worker.join(timeout=2)
        if worker.is_alive() or len(received) != 1:
            raise SystemExit("Codex did not execute the synthetic MCP finance call")
        call = received[0]
        if (
            call.get("tool") != "ketoshop_finance_summary"
            or call.get("arguments") != {"period": "day", "count": 1}
            or call.get("discussion_id") != 1
            or call.get("lease_id") != "synthetic-mcp-approval-capability"
        ):
            raise SystemExit("MCP finance call did not match the synthetic fixture")
        if marker not in answer:
            raise SystemExit("Codex did not use the synthetic finance result")
        print("approvalPolicy=never executed the annotated synthetic finance MCP tool")


if __name__ == "__main__":
    main()
