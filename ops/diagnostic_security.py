"""Strict SQL and output boundaries for owner-only Ketoshop diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MAX_QUERY_CHARS = 2_000
MAX_ROWS = 200
MAX_RESULT_BYTES = 64 * 1024

VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "ketoshop_diag_orders": (
        "order_id",
        "created_at",
        "status",
        "total",
        "quantity_total",
    ),
    "ketoshop_diag_order_items": (
        "order_id",
        "created_at",
        "status",
        "quantity",
        "unit",
        "line_amount",
    ),
}

_TOKEN = re.compile(
    r"\s*(?:(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<number>\d+(?:\.\d+)?)|"
    r"(?P<string>'(?:[^']|'')*')|(?P<op>>=|<=|<>|!=|=|>|<)|(?P<comma>,)|"
    r"(?P<star>\*)|(?P<other>.))",
    re.DOTALL,
)


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedQuery:
    sql: str
    params: tuple[Any, ...]
    limit: int
    digest: str


class _Parser:
    def __init__(self, query: str) -> None:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_QUERY_CHARS
        ):
            raise QueryError("query must be between 1 and 2000 characters")
        self.original = query
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(query):
            if not query[position:].strip():
                break
            match = _TOKEN.match(query, position)
            if match is None:
                raise QueryError("invalid query")
            position = match.end()
            kind = match.lastgroup
            value = match.group(kind or "other")
            if kind == "other":
                raise QueryError("unsupported SQL syntax")
            self.tokens.append((kind or "", value))
        self.index = 0
        self.params: list[Any] = []

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def pop(self, kind: str | None = None, value: str | None = None) -> str:
        token = self.peek()
        if token is None or (kind is not None and token[0] != kind):
            raise QueryError("invalid SELECT query")
        if value is not None and token[1].casefold() != value.casefold():
            raise QueryError("invalid SELECT query")
        self.index += 1
        return token[1]

    def keyword(self, value: str) -> bool:
        token = self.peek()
        return bool(
            token and token[0] == "word" and token[1].casefold() == value.casefold()
        )

    def identifier(self, allowed: tuple[str, ...]) -> str:
        name = self.pop("word").lower()
        if name not in allowed:
            raise QueryError("column is not available in this view")
        return name

    def literal(self) -> Any:
        token = self.peek()
        if token is None:
            raise QueryError("missing filter value")
        kind, value = token
        self.index += 1
        if kind == "number":
            return float(value) if "." in value else int(value)
        if kind == "string":
            return value[1:-1].replace("''", "'")
        raise QueryError("filter values must be numbers or quoted strings")

    def parse(self) -> ParsedQuery:
        self.pop("word", "select")
        table = ""
        if self.peek() == ("star", "*"):
            self.pop("star")
            self.pop("word", "from")
            table = self.pop("word").lower()
            if table not in VIEW_COLUMNS:
                raise QueryError(
                    "only approved Ketoshop diagnostic views are available"
                )
            columns = list(VIEW_COLUMNS[table])
        else:
            columns = [self.pop("word").lower()]
            while self.peek() == ("comma", ","):
                self.pop("comma")
                columns.append(self.pop("word").lower())
            self.pop("word", "from")
            table = self.pop("word").lower()
            if table not in VIEW_COLUMNS:
                raise QueryError(
                    "only approved Ketoshop diagnostic views are available"
                )
            if any(column not in VIEW_COLUMNS[table] for column in columns):
                raise QueryError("column is not available in this view")
        if len(set(columns)) != len(columns):
            raise QueryError("duplicate columns are not allowed")

        predicates: list[str] = []
        if self.keyword("where"):
            self.pop("word", "where")
            while True:
                column = self.identifier(VIEW_COLUMNS[table])
                operator = self.pop("op")
                value = self.literal()
                self.params.append(value)
                predicates.append(f'"{column}" {operator} %s')
                if not self.keyword("and"):
                    break
                self.pop("word", "and")

        order_clause = ""
        if self.keyword("order"):
            self.pop("word", "order")
            self.pop("word", "by")
            order_column = self.identifier(VIEW_COLUMNS[table])
            direction = "ASC"
            if self.keyword("asc") or self.keyword("desc"):
                direction = self.pop("word").upper()
            order_clause = f' ORDER BY "{order_column}" {direction}'

        limit = MAX_ROWS
        if self.keyword("limit"):
            self.pop("word", "limit")
            value = self.pop("number")
            if "." in value:
                raise QueryError("LIMIT must be a whole number")
            limit = int(value)
            if not 1 <= limit <= MAX_ROWS:
                raise QueryError("LIMIT must be between 1 and 200")
        if self.peek() is not None:
            raise QueryError("unsupported SQL syntax")

        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        sql = f'SELECT {quoted_columns} FROM "{table}"'
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        # Fetch one lookahead row so the caller can distinguish exactly 200
        # matches from a result that was cut at the row cap.
        sql += order_clause + f" LIMIT {min(limit + 1, MAX_ROWS + 1)}"
        return ParsedQuery(
            sql=sql,
            params=tuple(self.params),
            limit=limit,
            digest=hashlib.sha256(self.original.encode("utf-8")).hexdigest(),
        )


def parse_select(query: str) -> ParsedQuery:
    """Compile the narrow SELECT grammar to parameterized SQL over two views."""
    return _Parser(query).parse()


_SENSITIVE_KV = re.compile(
    r"(?i)(\b(?:phone|address|customer_name|username|user_id|chat_id|telegram_id)\b"
    r"\s*[=:]\s*)([^,;\s]+)"
)
_QUOTED_SENSITIVE_KV = re.compile(
    r"(?i)([\"']?(?:phone|address|customer_name|username|user_id|chat_id|telegram_id)"
    r"[\"']?\s*[=:]\s*)([\"'])(.*?)(\2)"
)
_MULTIWORD_SENSITIVE_REST = re.compile(
    r"(?i)([\"']?(?:address|customer_name|full_name|name)[\"']?\s*[=:]\s*).*$"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_BOT_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")


def redact_log_line(line: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove common customer identifiers and configured secrets from one line."""
    for secret in secrets:
        if secret:
            line = line.replace(secret, "[redacted]")
    line = _BEARER.sub("Bearer [redacted]", line)
    line = _BOT_TOKEN.sub("[redacted-token]", line)
    # Addresses and names are not reliably token-delimited. Drop everything
    # after one of these keys instead of leaving the remaining words behind.
    line = _MULTIWORD_SENSITIVE_REST.sub(r"\1[redacted]", line)
    line = _QUOTED_SENSITIVE_KV.sub(r"\1[redacted]", line)
    line = _SENSITIVE_KV.sub(r"\1[redacted]", line)
    line = _EMAIL.sub("[redacted-email]", line)
    return _PHONE.sub("[redacted-phone]", line)


_SAFE_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_SAFE_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "critical"})
_SAFE_LOG_EVENTS = frozenset(
    {
        "database_timeout",
        "db_timeout",
        "database_error",
        "db_connection_error",
        "connection_lost",
        "request_failed",
        "webhook_error",
        "worker_error",
        "startup_failed",
        "unhandled_exception",
    }
)
_SAFE_EXCEPTION_TYPES = frozenset(
    {
        "AssertionError",
        "CancelledError",
        "ConnectionError",
        "DatabaseError",
        "HTTPException",
        "InterfaceError",
        "IntegrityError",
        "JSONDecodeError",
        "KeyError",
        "OperationalError",
        "OSError",
        "PostgresError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)
_TEXT_LOG_PREFIX = re.compile(
    r"^(?:(?P<time>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+)?"
    r"(?:(?:stdout|stderr)\s+F\s+)?"
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL):"
    r"(?P<logger>[A-Za-z0-9_.-]{1,120}):"
)


def structured_log_metadata(line: str) -> dict[str, Any] | None:
    """Extract safe fields from JSON logs; omit free-form messages and raw lines."""
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        match = _TEXT_LOG_PREFIX.match(line)
        if match is None:
            return None
        metadata = {
            "level": match.group("level").casefold(),
            "event": "python_log",
        }
        if match.group("time"):
            metadata["time"] = match.group("time")
        return metadata
    if not isinstance(value, dict):
        return None

    safe: dict[str, Any] = {}
    for key in ("time", "timestamp", "asctime"):
        candidate = value.get(key)
        if isinstance(candidate, str) and _SAFE_TIMESTAMP.fullmatch(candidate):
            safe["time"] = candidate[:40]
            break
    for key in ("level", "levelname", "severity"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.casefold() in _SAFE_LEVELS:
            safe["level"] = candidate.casefold()
            break
    for key in ("event", "event_name", "error_code"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.casefold() in _SAFE_LOG_EVENTS:
            safe["event"] = candidate.casefold()
            break
    for key in ("exception_type", "exception_class"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate in _SAFE_EXCEPTION_TYPES:
            safe["exception_type"] = candidate
            break
    status = value.get("status_code")
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 100 <= status <= 599
    ):
        safe["status_code"] = status
    return safe or None
