"""Minimal stdio MCP server that forwards fixed diagnostic tools to the host."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

MAX_MESSAGE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 32 * 1024
# Above the host's worst case (3 s context + 3 s connect + 15 s statement + commit).
HOST_CALL_TIMEOUT_SECONDS = 35
MAX_SQL_CHARS = 16_000
DEFAULT_SOCKET = "/run/task-manager-diagnostics/diagnostics.sock"
LEASE_ENV = "DIAGNOSTIC_LEASE_ID"

TOOLS = [
    {
        "name": "ketoshop_query",
        "description": (
            "Read up to 200 rows from approved anonymized Ketoshop order views. "
            "Use one SELECT from ketoshop_diag_orders or ketoshop_diag_order_items. "
            "Available columns: ketoshop_diag_orders(order_id, created_at, status, total, "
            "quantity_total); ketoshop_diag_order_items(order_id, created_at, status, "
            "quantity, unit, line_amount). WHERE supports AND comparisons. "
            "ORDER BY and LIMIT are optional. No raw tables, writes, contact fields or joins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 2000}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Read anonymized Ketoshop order metadata",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "ketoshop_recent_logs",
        "description": (
            "Read safe structured metadata from Ketoshop bot logs in the last 24 hours. "
            "At most 500 lines are examined. Free-form message text and unstructured lines are omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Read redacted Ketoshop log metadata",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "ketoshop_finance_summary",
        "description": (
            "Return finance totals grouped by day or month, including status/source breakdowns, "
            "delivered revenue, expenses, known current-catalog cost and missing-cost item counts. "
            "Current catalog costs are estimates, not historical cost snapshots. Historical cost "
            "data is unavailable and is labeled as such. This bounded aggregate covers more than "
            "200 orders without returning one row per order. Results include source, selected "
            "period, UTC capture time and verified Ketoshop Git commit/image digest metadata when available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "month"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 180},
            },
            "required": ["period", "count"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Read bounded Ketoshop finance aggregates",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "db_sql",
        "description": (
            "Run exactly one SQL statement against this discussion's project PostgreSQL "
            "database (full access to its tables). write=false (default) runs in a READ "
            "ONLY transaction; write=true commits real changes and is only for an explicit "
            "owner request after a SELECT preview. 15 s statement timeout. Returns column "
            "names and at most 200 rows (cells clipped to 2000 chars); columns named like "
            "password/token/secret/hash/otp are returned as [masked]. Add LIMIT to large "
            "selects. Transaction control, SET/RESET, DO, COPY, VACUUM and ALTER ROLE/DATABASE "
            "are refused. Discover tables through information_schema or pg_catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "maxLength": MAX_SQL_CHARS},
                "write": {"type": "boolean", "default": False},
                "project": {"type": "string", "maxLength": 40},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Run SQL on the project database",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "logs",
        "description": (
            "Read full text lines from a project container's docker logs (credentials "
            "redacted). Defaults: first container of the project, last 60 minutes, 300 "
            "lines. Optional plain-substring grep (case-sensitive) scans the last 20000 "
            "lines and returns the newest matches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "maxLength": 40},
                "container": {"type": "string", "maxLength": 64},
                "since_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "tail": {"type": "integer", "minimum": 1, "maximum": 2000},
                "grep": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Read project container logs",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "containers",
        "description": (
            "List this project's containers with state, health, image, start time and "
            "restart count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "maxLength": 40}},
            "additionalProperties": False,
        },
        "annotations": {
            "title": "Inspect project containers",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _request_host(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    )
    if len(raw) > MAX_REQUEST_BYTES:
        return {"ok": False, "error": "diagnostic request is too large"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(HOST_CALL_TIMEOUT_SECONDS)
            connection.connect(socket_path)
            connection.sendall(raw)
            chunks = bytearray()
            while len(chunks) <= MAX_MESSAGE_BYTES:
                block = connection.recv(min(8192, MAX_MESSAGE_BYTES + 1 - len(chunks)))
                if not block:
                    break
                if b"\n" in block:
                    chunks.extend(block.split(b"\n", 1)[0])
                    break
                chunks.extend(block)
        if len(chunks) > MAX_MESSAGE_BYTES:
            return {"ok": False, "error": "diagnostic result is too large"}
        result = json.loads(chunks)
        return (
            result
            if isinstance(result, dict)
            else {"ok": False, "error": "invalid host response"}
        )
    except (OSError, ValueError, TimeoutError):
        return {"ok": False, "error": "diagnostics host is unavailable"}


def _is_int(value: Any, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _invalid_arguments(name: str, arguments: dict[str, Any]) -> str:
    """Shape check only; the host re-validates and enforces authorization."""
    allowed = {
        "db_sql": {"sql", "write", "project"},
        "logs": {"project", "container", "since_minutes", "tail", "grep"},
        "containers": {"project"},
    }[name]
    if not set(arguments) <= allowed:
        return f"unexpected argument for {name}"
    project = arguments.get("project")
    if project is not None and (not isinstance(project, str) or len(project) > 40):
        return "invalid project"
    if name == "db_sql":
        sql = arguments.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS:
            return f"sql must be 1 to {MAX_SQL_CHARS} characters"
        if not isinstance(arguments.get("write", False), bool):
            return "write must be true or false"
    elif name == "logs":
        container = arguments.get("container")
        if container is not None and (not isinstance(container, str) or len(container) > 64):
            return "invalid container"
        if "since_minutes" in arguments and not _is_int(arguments["since_minutes"], 1, 1440):
            return "since_minutes must be between 1 and 1440"
        if "tail" in arguments and not _is_int(arguments["tail"], 1, 2000):
            return "tail must be between 1 and 2000"
        grep = arguments.get("grep")
        if grep is not None and (not isinstance(grep, str) or not 1 <= len(grep) <= 200):
            return "grep must be 1 to 200 characters"
    return ""


def _tool_call(
    socket_path: str, discussion_id: int, lease_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "ketoshop_query":
        query = arguments.get("query")
        if (
            not isinstance(query, str)
            or len(query) > 2_000
            or set(arguments) != {"query"}
        ):
            return {"ok": False, "error": "invalid query"}
        action = {"tool": name, "query": query}
    elif name == "ketoshop_recent_logs":
        if arguments:
            return {"ok": False, "error": "this tool accepts no arguments"}
        action = {"tool": name}
    elif name == "ketoshop_finance_summary":
        period = arguments.get("period")
        count = arguments.get("count")
        if (
            period not in {"day", "month"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= (180 if period == "day" else 36)
            or set(arguments) != {"period", "count"}
        ):
            return {"ok": False, "error": "invalid finance summary arguments"}
        action = {"tool": name, "arguments": {"period": period, "count": count}}
    elif name in {"db_sql", "logs", "containers"}:
        error = _invalid_arguments(name, arguments)
        if error:
            return {"ok": False, "error": error}
        action = {"tool": name, "arguments": arguments}
    else:
        return {"ok": False, "error": "unknown diagnostic tool"}
    return _request_host(
        socket_path,
        {"discussion_id": discussion_id, "lease_id": lease_id, **action},
    )


def serve(socket_path: str, discussion_id: int, lease_id: str) -> None:
    for line in sys.stdin.buffer:
        if len(line) > MAX_MESSAGE_BYTES:
            continue
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "project-diagnostics", "version": "2.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            host_result = _tool_call(
                socket_path, discussion_id, lease_id, message.get("params", {})
            )
            successful = bool(host_result.get("ok"))
            body = host_result.get("result") if successful else host_result.get("error")
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(body, ensure_ascii=False)}
                ],
                "isError": not successful,
            }
        else:
            if request_id is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
            continue
        if request_id is not None:
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discussion-id", type=int, required=True)
    # The lease is a capability. It is passed through the environment so it does
    # not show up in the world-readable process list; `--lease-id` remains only
    # as a fallback for callers that cannot set the variable.
    parser.add_argument("--lease-id")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args()
    lease_id = os.environ.get(LEASE_ENV) or args.lease_id or ""
    if args.discussion_id < 1 or not lease_id or len(lease_id) > 100:
        raise SystemExit(2)
    serve(args.socket, args.discussion_id, lease_id)


if __name__ == "__main__":
    main()
