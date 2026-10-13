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


# --------------------------------------------------------------------------
# Full-access tools (db_sql / logs / containers). The owner chose full database
# and log access for the agent; the controls kept here are that the agent never
# holds a DSN, that exactly one statement runs per call, and that credentials do
# not leak back through result cells or log lines.
# --------------------------------------------------------------------------

MAX_SQL_CHARS = 16_000
MAX_CELL_CHARS = 2_000
# Rows are packed into the 64 KB result with headroom for the JSON envelope.
RESULT_BODY_BUDGET = 60 * 1024

_TRANSACTION_CONTROL = frozenset(
    {"begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release"}
)
_LEADING_WORD = re.compile(r"[A-Za-z_]+")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_MASKED_NAME = re.compile(
    r"password|passwd|secret|token|api_?key|private_?key|hash",
    re.IGNORECASE,
)
# Short words need a boundary so "footprint" or "salted_at" style names are not
# masked by accident; "otp_code", "user_otp" and "salt" are.
_MASKED_SHORT_NAME = re.compile(r"(?:^|[^a-z])(?:otp|salt)(?:[^a-z]|$)", re.IGNORECASE)


def validate_single_statement(sql: object) -> str:
    """Return `sql` stripped of one trailing semicolon, or raise QueryError.

    Walks the text the way the server lexes it (comments, quoted strings and
    identifiers, dollar quoting) so a `;` inside them is not a separator. The
    check exists because psycopg runs a parameterless query as a simple query,
    which would otherwise accept `SELECT 1; COMMIT; ...` and escape the
    READ ONLY transaction.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise QueryError("sql must be a non-empty string")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryError(f"sql must be at most {MAX_SQL_CHARS} characters")
    if "\x00" in sql:
        raise QueryError("sql must not contain NUL characters")
    length = len(sql)
    index = 0
    statements = 0
    has_content = False
    first_word = ""
    while index < length:
        char = sql[index]
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if char == "/" and sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise QueryError("unterminated block comment")
            continue
        if char in "'\"":
            escaped = (
                char == "'"
                and index > 0
                and sql[index - 1] in "eE"
                and (index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_"))
            )
            index += 1
            while True:
                if index >= length:
                    raise QueryError("unterminated quoted string")
                if escaped and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == char:
                    if index + 1 < length and sql[index + 1] == char:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            has_content = True
            if not first_word:
                first_word = "?"
            continue
        if char == "$":
            tag = _DOLLAR_TAG.match(sql, index)
            if tag is not None:
                end = sql.find(tag.group(0), tag.end())
                if end < 0:
                    raise QueryError("unterminated dollar-quoted string")
                index = end + len(tag.group(0))
                has_content = True
                if not first_word:
                    first_word = "?"
                continue
        if char == ";":
            if has_content:
                statements += 1
                has_content = False
            index += 1
            continue
        if not char.isspace():
            if statements and not has_content:
                raise QueryError("only one SQL statement is allowed per call")
            if not first_word:
                match = _LEADING_WORD.match(sql, index)
                first_word = match.group(0).lower() if match else "?"
            has_content = True
        index += 1
    if has_content:
        statements += 1
    if statements != 1:
        raise QueryError("exactly one SQL statement is required")
    if first_word in _TRANSACTION_CONTROL:
        raise QueryError("transaction control statements are not allowed")
    return sql.strip()


def is_masked_column(name: object) -> bool:
    text = str(name)
    return bool(_MASKED_NAME.search(text) or _MASKED_SHORT_NAME.search(text))


def clean_cell(value: Any) -> Any:
    """JSON-safe cell, with long text clipped to MAX_CELL_CHARS."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, str):
        text = value
    elif hasattr(value, "isoformat"):
        text = value.isoformat()
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    else:
        text = str(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + "...[truncated]"
    return text


def pack_rows(
    columns: list[str], rows: list[tuple[Any, ...]], limit: int = MAX_ROWS
) -> tuple[list[list[Any]], bool, list[str]]:
    """Mask, clip and size-cap rows. Returns (rows, truncated, masked columns)."""
    masked_indexes = [i for i, name in enumerate(columns) if is_masked_column(name)]
    packed: list[list[Any]] = []
    used = len(json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode())
    truncated = len(rows) > limit
    for row in rows[:limit]:
        cells = [clean_cell(cell) for cell in row]
        for i in masked_indexes:
            if cells[i] is not None:
                cells[i] = "[masked]"
        size = len(json.dumps(cells, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if used + size > RESULT_BODY_BUDGET:
            truncated = True
            break
        used += size
        packed.append(cells)
    return packed, truncated, [columns[i] for i in masked_indexes]


_DSN_PASSWORD = re.compile(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:)[^@\s]+(@)")
_LOG_BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}")


def redact_credentials(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Strip known secrets and obvious credential shapes; keep everything else."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _DSN_PASSWORD.sub(r"\1[redacted]\2", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    return _LOG_BOT_TOKEN.sub("[redacted-token]", text)
