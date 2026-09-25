"""Minimal stdio MCP server that forwards fixed diagnostic tools to the host."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

MAX_MESSAGE_BYTES = 64 * 1024
HOST_CALL_TIMEOUT_SECONDS = 15
DEFAULT_SOCKET = "/run/task-manager-diagnostics/diagnostics.sock"

TOOLS = [
    {
        "name": "ketoshop_query",
        "description": (
            "Read up to 200 rows from approved anonymized Ketoshop order views. "
            "Use one SELECT from ketoshop_diag_orders or ketoshop_diag_order_items. "
            "Available columns: ketoshop_diag_orders(order_id, created_at, status, total, "
            "quantity_total); ketoshop_diag_order_items(order_id, created_at, status, "
            "item_name, quantity, unit, line_amount). WHERE supports AND comparisons. "
            "ORDER BY and LIMIT are optional. No raw tables, writes, contact fields or joins."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 2000}},
            "required": ["query"],
            "additionalProperties": False,
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
    },
    {
        "name": "ketoshop_finance_summary",
        "description": (
            "Return finance totals grouped by day or month, including status/source breakdowns, "
            "delivered revenue, expenses, known current-catalog cost and missing-cost item counts. "
            "Current catalog costs are estimates, not historical cost snapshots. Historical cost "
            "data is unavailable and is labeled as such. This bounded aggregate covers more than "
            "200 orders without returning one row per order."
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
    },
]


def _request_host(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    )
    if len(raw) > MAX_MESSAGE_BYTES:
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
        return {"ok": False, "error": "Ketoshop diagnostics host is unavailable"}


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
                "serverInfo": {"name": "ketoshop-diagnostics", "version": "1.0.0"},
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
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args()
    if args.discussion_id < 1 or not args.lease_id or len(args.lease_id) > 100:
        raise SystemExit(2)
    serve(args.socket, args.discussion_id, args.lease_id)


if __name__ == "__main__":
    main()
