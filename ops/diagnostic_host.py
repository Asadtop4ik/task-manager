"""Privileged host-side broker for owner-only Ketoshop diagnostics."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from diagnostic_security import (
    MAX_RESULT_BYTES,
    MAX_ROWS,
    ParsedQuery,
    parse_select,
    redact_log_line,
)

API_BASE_URL = os.environ.get(
    "TASK_MANAGER_API_URL", "https://tasks.standart-eko.uz/api/v1"
)
SOCKET_PATH = Path(
    os.environ.get(
        "TASK_MANAGER_DIAGNOSTICS_SOCKET",
        "/run/task-manager-diagnostics/diagnostics.sock",
    )
)
AUDIT_PATH = Path(
    os.environ.get(
        "TASK_MANAGER_DIAGNOSTICS_AUDIT_FILE",
        "/var/log/task-manager-diagnostics/audit.jsonl",
    )
)
MAX_REQUEST_BYTES = 8 * 1024
MAX_AUDIT_AGE = timedelta(hours=24)
MAX_AUDIT_LINES = 500
MAX_CALLS_PER_REVISION = 10
MAX_LOG_BYTES = 1024 * 1024
ALLOWED_CONTAINER = "ketoshop"
ALLOWED_TOOLS = frozenset({"ketoshop_query", "ketoshop_recent_logs"})


class DiagnosticsError(RuntimeError):
    pass


class AuditLog:
    """Store bounded metadata only, pruning to 24 hours and 500 records."""

    def __init__(self, path: Path = AUDIT_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        safe = {
            "time": now.isoformat(),
            "discussion_id": int(event["discussion_id"]),
            "revision": int(event["revision"]),
            "tool": (
                event["tool"]
                if isinstance(event.get("tool"), str) and event["tool"] in ALLOWED_TOOLS
                else "invalid"
            ),
            "query_sha256": str(event.get("query_sha256", ""))[:64],
            "rows": int(event.get("rows", 0)),
            "result_bytes": int(event.get("result_bytes", 0)),
            "duration_ms": int(event.get("duration_ms", 0)),
            "outcome": str(event.get("outcome", "error"))[:24],
        }
        line = json.dumps(safe, separators=(",", ":"))
        cutoff = now - MAX_AUDIT_AGE
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prior: list[str] = []
            try:
                for old_line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(old_line)
                        timestamp = datetime.fromisoformat(item["time"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    if timestamp >= cutoff:
                        prior.append(old_line)
            except FileNotFoundError:
                pass
            records = (prior + [line])[-MAX_AUDIT_LINES:]
            temp = self.path.with_suffix(".tmp")
            temp.write_text("\n".join(records) + "\n", encoding="utf-8")
            temp.chmod(0o640)
            os.replace(temp, self.path)


class DiagnosticHost:
    def __init__(
        self,
        *,
        intake_token: str,
        database_url: str,
        api_base_url: str = API_BASE_URL,
        audit: AuditLog | None = None,
        command_runner: Any = subprocess.run,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        if not intake_token or not database_url:
            raise ValueError("diagnostic host credentials are required")
        self._intake_token = intake_token
        self._database_url = database_url
        self._api_base_url = api_base_url.rstrip("/")
        self._audit = audit or AuditLog()
        self._command_runner = command_runner
        self._opener = opener
        self._counter_lock = threading.Lock()
        self._calls: dict[int, tuple[int, int, float]] = {}

    def _context(self, discussion_id: int) -> tuple[int, str]:
        if (
            isinstance(discussion_id, bool)
            or not isinstance(discussion_id, int)
            or discussion_id < 1
        ):
            raise DiagnosticsError("diagnostics are not available")
        request = urllib.request.Request(
            f"{self._api_base_url}/project-discussions/{discussion_id}/diagnostic-context",
            headers={
                "X-Intake-Worker-Token": self._intake_token,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=3) as response:
                raw = response.read(2048)
        except (OSError, urllib.error.URLError, TimeoutError):
            raise DiagnosticsError("diagnostics are not available") from None
        try:
            context = json.loads(raw)
        except (ValueError, TypeError):
            raise DiagnosticsError("diagnostics are not available") from None
        if (
            not isinstance(context, dict)
            or context.get("authorized") is not True
            or context.get("active") is not True
            or context.get("project_key") != "ketoshop"
            or isinstance(context.get("revision"), bool)
            or not isinstance(context.get("revision"), int)
            or context["revision"] < 1
        ):
            raise DiagnosticsError("diagnostics are not available")
        return context["revision"], "ketoshop"

    def _consume_call(self, discussion_id: int, revision: int) -> None:
        now = time.monotonic()
        with self._counter_lock:
            for key, (saved_revision, _count, last_call) in list(self._calls.items()):
                if key == discussion_id and (
                    saved_revision != revision or now - last_call > 300
                ):
                    self._calls.pop(key, None)
            saved = self._calls.get(discussion_id)
            count = saved[1] if saved and saved[0] == revision else 0
            if count >= MAX_CALLS_PER_REVISION:
                raise DiagnosticsError(
                    "10 diagnostic calls per discussion turn are allowed"
                )
            self._calls[discussion_id] = (revision, count + 1, now)

    def _select(self, query: str) -> tuple[list[dict[str, Any]], str]:
        parsed: ParsedQuery = parse_select(query)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise DiagnosticsError(
                "diagnostics database driver is unavailable"
            ) from None
        try:
            with (
                psycopg.connect(
                    self._database_url, connect_timeout=3, row_factory=dict_row
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '5000ms'")
                cursor.execute(
                    "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
                )
                cursor.execute(parsed.sql, parsed.params)
                rows = cursor.fetchmany(min(parsed.limit, MAX_ROWS) + 1)
            return rows[:MAX_ROWS], parsed.digest
        except psycopg.Error as exc:
            # Do not surface SQL, DSNs or server exception text to the model.
            raise DiagnosticsError("approved Ketoshop query failed") from exc

    @staticmethod
    def _clean_value(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)[:512]

    def _orders(self, query: str) -> tuple[dict[str, Any], str, int]:
        rows, digest = self._select(query)
        cleaned = [
            {key: self._clean_value(value) for key, value in row.items()}
            for row in rows
        ]
        result = {
            "rows": cleaned,
            "row_count": len(cleaned),
            "truncated": len(rows) == MAX_ROWS,
        }
        return result, digest, len(cleaned)

    def _logs(self) -> tuple[dict[str, Any], str, int]:
        try:
            completed = self._command_runner(
                [
                    "/usr/bin/docker",
                    "logs",
                    "--since",
                    "24h",
                    "--tail",
                    "500",
                    ALLOWED_CONTAINER,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        raw = completed.stdout or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        raw = raw[-MAX_LOG_BYTES:]
        lines = raw.decode("utf-8", "replace").splitlines()[-500:]
        secrets = (self._intake_token, self._database_url)
        redacted = [redact_log_line(line[:2_000], secrets) for line in lines]
        result = {"lines": redacted, "line_count": len(redacted), "window_hours": 24}
        return result, "", len(redacted)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        discussion_id = request.get("discussion_id")
        tool = request.get("tool")
        revision = 0
        digest = ""
        rows = 0
        outcome = "denied"
        body: dict[str, Any]
        try:
            revision, _project = self._context(discussion_id)
            self._consume_call(discussion_id, revision)
            if tool == "ketoshop_query":
                query = request.get("query")
                if not isinstance(query, str):
                    raise DiagnosticsError("invalid query")
                body, digest, rows = self._orders(query)
            elif tool == "ketoshop_recent_logs":
                body, digest, rows = self._logs()
            else:
                raise DiagnosticsError("unknown diagnostic tool")
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if len(encoded) > MAX_RESULT_BYTES:
                raise DiagnosticsError("diagnostic result exceeds 64 KB")
            outcome = "ok"
            response = {"ok": True, "result": body}
            result_bytes = len(encoded)
        except DiagnosticsError as exc:
            response = {"ok": False, "error": str(exc)}
            result_bytes = len(str(exc).encode())
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            response = {"ok": False, "error": "diagnostics request failed"}
            result_bytes = len(response["error"].encode())
        if isinstance(discussion_id, int) and not isinstance(discussion_id, bool):
            self._audit.record(
                {
                    "discussion_id": discussion_id,
                    "revision": revision,
                    "tool": (
                        tool
                        if isinstance(tool, str) and tool in ALLOWED_TOOLS
                        else "invalid"
                    ),
                    "query_sha256": digest,
                    "rows": rows,
                    "result_bytes": result_bytes,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "outcome": outcome,
                }
            )
        return response


def _handle_client(connection: socket.socket, host: DiagnosticHost) -> None:
    with connection:
        connection.settimeout(7)
        data = bytearray()
        while len(data) <= MAX_REQUEST_BYTES:
            chunk = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
            if not chunk or b"\n" in chunk:
                if chunk:
                    data.extend(chunk.split(b"\n", 1)[0])
                break
            data.extend(chunk)
        if len(data) > MAX_REQUEST_BYTES:
            response = {"ok": False, "error": "diagnostic request is too large"}
        else:
            try:
                request = json.loads(data)
                if not isinstance(request, dict):
                    raise TypeError("request must be a JSON object")
                response = host.handle(request)
            except (ValueError, TypeError):
                response = {"ok": False, "error": "invalid diagnostic request"}
        encoded = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            encoded = b'{"ok":false,"error":"diagnostic result exceeds 64 KB"}'
        connection.sendall(encoded + b"\n")


def serve(host: DiagnosticHost, socket_path: Path = SOCKET_PATH) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError("diagnostics socket path exists and is not a socket")
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o660)
    server.listen(16)
    try:
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=_handle_client, args=(connection, host), daemon=True
            ).start()
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    host = DiagnosticHost(
        intake_token=os.environ["INTAKE_WORKER_TOKEN"],
        database_url=os.environ["KETOSHOP_DIAGNOSTICS_DATABASE_URL"],
    )
    serve(host)


if __name__ == "__main__":
    main()
