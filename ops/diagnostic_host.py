"""Privileged host-side broker for owner-only Ketoshop diagnostics."""

from __future__ import annotations

import grp
import hashlib
import json
import os
import re
import selectors
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
    structured_log_metadata,
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
# Phase 3 (agent-svc chat lane): a dedicated group that owns ONLY this socket,
# narrower than the old `codex-runner` group (the live GitHub Actions runner
# identity, far broader than "may read this one socket"). `agent-codex`'s
# sudoers rule for the `discussion` subcommand grants exactly this group, via
# `sudo -g`, nothing else.
SOCKET_GROUP = os.environ.get("TASK_MANAGER_DIAGNOSTICS_SOCKET_GROUP", "task-diag-client")
MAX_CALLS_PER_REVISION = 10
MAX_LOG_INPUT_BYTES = 4 * 1024 * 1024
MAX_LOG_LINE_BYTES = 4 * 1024
MAX_LOG_RESULT_BYTES = 24 * 1024
FINANCE_VERSION_TIMEOUT_SECONDS = 3
ALLOWED_CONTAINER = "ketoshop"
ALLOWED_TOOLS = frozenset(
    {"ketoshop_query", "ketoshop_recent_logs", "ketoshop_finance_summary"}
)
_KETOSHOP_IMAGE_REF = re.compile(r"^ghcr\.io/muradjanov-dev/ketoshop:([0-9a-f]{40})$")
_KETOSHOP_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

FINANCE_SUMMARY_SQL = """
WITH parameters AS (
    SELECT
        %s::text AS granularity,
        %s::integer AS bucket_count,
        date_trunc(
            %s,
            CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent'
        ) AS local_current_bucket,
        date_trunc(
            %s,
            CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent'
        ) - INTERVAL '5 hours' AS current_bucket,
        CASE WHEN %s::text = 'day' THEN INTERVAL '1 day'
             ELSE INTERVAL '1 month' END AS bucket_step
), buckets AS (
    SELECT generate_series(
        local_current_bucket - ((bucket_count - 1) * bucket_step),
        local_current_bucket,
        bucket_step
    )::date AS bucket
    FROM parameters
), orders_by_bucket AS (
    SELECT
        date_trunc(p.granularity, o.created_at + INTERVAL '5 hours')::date AS bucket,
        o.status,
        o.source,
        COUNT(*)::integer AS order_count,
        SUM(o.revenue)::numeric AS revenue,
        SUM(o.current_catalog_cost)::numeric AS current_catalog_cost,
        SUM(o.missing_cost_items)::integer AS missing_cost_items
    FROM public.ketoshop_diag_finance_orders AS o
    CROSS JOIN parameters AS p
    WHERE o.created_at >= p.current_bucket - ((p.bucket_count - 1) * p.bucket_step)
      AND o.created_at < p.current_bucket + p.bucket_step
    GROUP BY 1, 2, 3
), totals AS (
    SELECT
        bucket,
        SUM(order_count)::integer AS order_count,
        SUM(revenue)::numeric AS revenue,
        SUM(current_catalog_cost)::numeric AS current_catalog_cost,
        SUM(missing_cost_items)::integer AS missing_cost_items
    FROM orders_by_bucket
    GROUP BY bucket
), status_breakdown AS (
    SELECT
        bucket,
        jsonb_object_agg(status, jsonb_build_object(
            'order_count', order_count,
            'revenue', revenue
        )) AS breakdown
    FROM (
        SELECT bucket, status, SUM(order_count)::integer AS order_count,
               SUM(revenue)::numeric AS revenue
        FROM orders_by_bucket
        GROUP BY bucket, status
    ) AS grouped
    GROUP BY bucket
), source_breakdown AS (
    SELECT
        bucket,
        jsonb_object_agg(source, jsonb_build_object(
            'order_count', order_count,
            'revenue', revenue
        )) AS breakdown
    FROM (
        SELECT bucket, source, SUM(order_count)::integer AS order_count,
               SUM(revenue)::numeric AS revenue
        FROM orders_by_bucket
        GROUP BY bucket, source
    ) AS grouped
    GROUP BY bucket
), expenses_by_bucket AS (
    SELECT
        date_trunc(p.granularity, e.created_at + INTERVAL '5 hours')::date AS bucket,
        SUM(e.amount)::numeric AS expenses
    FROM public.ketoshop_diag_expenses AS e
    CROSS JOIN parameters AS p
    WHERE e.created_at >= p.current_bucket - ((p.bucket_count - 1) * p.bucket_step)
      AND e.created_at < p.current_bucket + p.bucket_step
    GROUP BY 1
)
SELECT
    b.bucket,
    COALESCE(t.order_count, 0)::integer AS order_count,
    COALESCE(t.revenue, 0::numeric)::numeric AS revenue,
    COALESCE(t.current_catalog_cost, 0::numeric)::numeric AS current_catalog_cost,
    COALESCE(t.missing_cost_items, 0)::integer AS missing_cost_items,
    COALESCE(e.expenses, 0::numeric)::numeric AS expenses,
    COALESCE(s.breakdown, '{}'::jsonb) AS status_breakdown,
    COALESCE(src.breakdown, '{}'::jsonb) AS source_breakdown,
    'current_catalog'::text AS cost_basis,
    FALSE AS historical_cost_data_available
FROM buckets AS b
LEFT JOIN totals AS t USING (bucket)
LEFT JOIN status_breakdown AS s USING (bucket)
LEFT JOIN source_breakdown AS src USING (bucket)
LEFT JOIN expenses_by_bucket AS e USING (bucket)
ORDER BY b.bucket DESC
"""


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
            "actor_id": (
                int(event["actor_id"])
                if isinstance(event.get("actor_id"), int)
                and not isinstance(event.get("actor_id"), bool)
                and event["actor_id"] > 0
                else None
            ),
            "project_id": (
                int(event["project_id"])
                if isinstance(event.get("project_id"), int)
                and not isinstance(event.get("project_id"), bool)
                and event["project_id"] > 0
                else None
            ),
            "project_key": (
                "ketoshop" if event.get("project_key") == "ketoshop" else "unknown"
            ),
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
        popen_factory: Any = subprocess.Popen,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        if not intake_token or not database_url:
            raise ValueError("diagnostic host credentials are required")
        self._intake_token = intake_token
        self._database_url = database_url
        self._api_base_url = api_base_url.rstrip("/")
        self._audit = audit or AuditLog()
        self._command_runner = command_runner
        self._popen_factory = popen_factory
        self._opener = opener
        self._counter_lock = threading.Lock()
        self._calls: dict[int, tuple[int, int, float]] = {}

    def _context(self, discussion_id: int, lease_id: str) -> tuple[int, int, int, str]:
        if (
            isinstance(discussion_id, bool)
            or not isinstance(discussion_id, int)
            or discussion_id < 1
            or not isinstance(lease_id, str)
            or not lease_id
            or len(lease_id) > 100
        ):
            raise DiagnosticsError("diagnostics are not available")
        request = urllib.request.Request(
            f"{self._api_base_url}/project-discussions/{discussion_id}/diagnostic-context",
            headers={
                "X-Intake-Worker-Token": self._intake_token,
                "X-Intake-Lease-ID": lease_id,
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
            or isinstance(context.get("actor_id"), bool)
            or not isinstance(context.get("actor_id"), int)
            or isinstance(context.get("project_id"), bool)
            or not isinstance(context.get("project_id"), int)
            or context["actor_id"] < 1
            or context["project_id"] < 1
        ):
            raise DiagnosticsError("diagnostics are not available")
        return (
            context["revision"],
            context["actor_id"],
            context["project_id"],
            "ketoshop",
        )

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

    def _select(self, query: str) -> tuple[list[dict[str, Any]], str, bool]:
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
            has_more = len(rows) > parsed.limit
            return rows[: parsed.limit], parsed.digest, has_more
        except psycopg.Error as exc:
            # Do not surface SQL, DSNs or server exception text to the model.
            raise DiagnosticsError("approved Ketoshop query failed") from exc

    @staticmethod
    def _clean_value(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {
                str(key): DiagnosticHost._clean_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [DiagnosticHost._clean_value(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)[:512]

    def _orders(self, query: str) -> tuple[dict[str, Any], str, int]:
        rows, digest, has_more = self._select(query)
        cleaned = [
            {key: self._clean_value(value) for key, value in row.items()}
            for row in rows
        ]
        result = {
            "rows": cleaned,
            "row_count": len(cleaned),
            "truncated": has_more,
        }
        return result, digest, len(cleaned)

    def _finance_summary(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str, int]:
        period = arguments.get("period")
        bucket_count = arguments.get("count")
        if period not in {"day", "month"}:
            raise DiagnosticsError("period must be day or month")
        maximum = 180 if period == "day" else 36
        if (
            isinstance(bucket_count, bool)
            or not isinstance(bucket_count, int)
            or not 1 <= bucket_count <= maximum
        ):
            raise DiagnosticsError(
                f"count must be between 1 and {maximum} for {period}"
            )
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
                cursor.execute(
                    FINANCE_SUMMARY_SQL,
                    (period, bucket_count, period, period, period),
                )
                rows = cursor.fetchmany(min(bucket_count, MAX_ROWS) + 1)
        except psycopg.Error as exc:
            raise DiagnosticsError("approved Ketoshop finance summary failed") from exc
        if len(rows) > bucket_count or len(rows) > MAX_ROWS:
            raise DiagnosticsError("finance summary exceeded its fixed bucket limit")
        image_version = self._verified_ketoshop_image_version()
        buckets = [
            {key: self._clean_value(value) for key, value in row.items()}
            for row in rows
        ]
        result = {
            "source": "ketoshop_postgresql_views",
            "period": period,
            "timezone": "Asia/Tashkent",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "bucket_count": len(buckets),
            "buckets": buckets,
            "cost_basis": "current_catalog",
            "historical_cost_data_available": False,
            "verified_version": {
                "git_commit": image_version["git_commit"] if image_version else None,
                "image_digest": (
                    image_version["image_digest"] if image_version else None
                ),
                "verified": image_version is not None,
            },
        }
        digest = hashlib.sha256(f"finance:{period}:{bucket_count}".encode()).hexdigest()
        return result, digest, len(buckets)

    def _verified_ketoshop_image_version(self) -> dict[str, str] | None:
        try:
            completed = self._command_runner(
                [
                    "/usr/bin/docker",
                    "inspect",
                    "--format",
                    "{{.Config.Image}} {{.Image}}",
                    ALLOWED_CONTAINER,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=FINANCE_VERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout or b""
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        fields = str(value).split()
        if len(fields) != 2:
            return None
        image_ref, image_id = fields
        ref_match = _KETOSHOP_IMAGE_REF.fullmatch(image_ref)
        if ref_match is None or _KETOSHOP_IMAGE_ID.fullmatch(image_id) is None:
            return None
        return {"git_commit": ref_match.group(1), "image_digest": image_id}

    def _logs(self) -> tuple[dict[str, Any], str, int]:
        command = [
            "/usr/bin/docker",
            "logs",
            "--since",
            "24h",
            "--tail",
            "500",
            ALLOWED_CONTAINER,
        ]
        try:
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except OSError:
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        if process.stdout is None:
            process.kill()
            raise DiagnosticsError("Ketoshop logs are unavailable")

        lines: list[bytes] = []
        pending = bytearray()
        dropping_long_line = False
        bytes_read = 0
        truncated = False
        input_capped = False
        deadline = time.monotonic() + 5
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 5)
                    events = selector.select(min(remaining, 0.5))
                    if not events:
                        continue
                    for key, _ in events:
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        bytes_read += len(chunk)
                        if bytes_read > MAX_LOG_INPUT_BYTES:
                            truncated = True
                            input_capped = True
                            process.kill()
                            selector.unregister(key.fileobj)
                            break
                        for byte in chunk:
                            if byte == 10:
                                if not dropping_long_line:
                                    lines.append(bytes(pending).rstrip(b"\r"))
                                    if len(lines) > 500:
                                        del lines[0]
                                        truncated = True
                                pending.clear()
                                dropping_long_line = False
                            elif not dropping_long_line:
                                if len(pending) < MAX_LOG_LINE_BYTES:
                                    pending.append(byte)
                                else:
                                    pending.clear()
                                    dropping_long_line = True
                                    truncated = True
                if pending and not dropping_long_line and len(lines) < 500:
                    lines.append(bytes(pending).rstrip(b"\r"))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        finally:
            process.stdout.close()
        try:
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        if return_code != 0 and not input_capped:
            raise DiagnosticsError("Ketoshop logs are unavailable")

        entries: list[dict[str, Any]] = []
        for line in lines[-500:]:
            metadata = structured_log_metadata(line.decode("utf-8", "replace"))
            if metadata is not None:
                entries.append(metadata)
        result: dict[str, Any] = {
            "entries": entries,
            "entry_count": len(entries),
            "window_hours": 24,
            "truncated": truncated,
        }
        while (
            entries
            and len(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            )
            > MAX_LOG_RESULT_BYTES
        ):
            entries.pop(0)
            truncated = True
            result["entry_count"] = len(entries)
            result["truncated"] = truncated
        return result, "", len(entries)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        discussion_id = request.get("discussion_id")
        lease_id = request.get("lease_id")
        tool = request.get("tool")
        revision = 0
        actor_id = 0
        project_id = 0
        digest = ""
        rows = 0
        outcome = "denied"
        body: dict[str, Any]
        try:
            revision, actor_id, project_id, _project = self._context(
                discussion_id, lease_id
            )
            self._consume_call(discussion_id, revision)
            if tool == "ketoshop_query":
                query = request.get("query")
                if not isinstance(query, str):
                    raise DiagnosticsError("invalid query")
                body, digest, rows = self._orders(query)
            elif tool == "ketoshop_recent_logs":
                body, digest, rows = self._logs()
            elif tool == "ketoshop_finance_summary":
                arguments = request.get("arguments")
                if not isinstance(arguments, dict):
                    raise DiagnosticsError("invalid finance summary arguments")
                body, digest, rows = self._finance_summary(arguments)
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
                    "actor_id": actor_id if revision > 0 else None,
                    "project_id": project_id if revision > 0 else None,
                    "project_key": "ketoshop" if revision > 0 else "unknown",
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
        connection.settimeout(22)
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


def _narrow_socket_group(socket_path: Path, group_name: str) -> None:
    """Best-effort: chgrp the socket to `group_name` if that group exists on
    this host; otherwise leave its group exactly as it is today (the
    process's own primary/effective group, `codex-runner` per the systemd
    unit's own `Group=`). This ordering means installing this code can never
    itself break the still-live legacy consumer (`ops/discussion_appserver.py`,
    running as `codex-runner`) regardless of whether the group-creation step
    of the installer has run yet on this host.
    """
    try:
        gid = grp.getgrnam(group_name).gr_gid
    except KeyError:
        return
    try:
        os.chown(socket_path, -1, gid)
    except OSError as error:
        # The service user must be a member of the group to chgrp (no
        # CAP_CHOWN). Never let this take the broker down: keep today's group.
        print(
            f"diagnostics: socket stays in its current group ({type(error).__name__})",
            flush=True,
        )


def serve(host: DiagnosticHost, socket_path: Path = SOCKET_PATH) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError("diagnostics socket path exists and is not a socket")
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o660)
    _narrow_socket_group(socket_path, SOCKET_GROUP)
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
