"""JSON-lines logging to stderr (journald) with secret redaction.

Every string that reaches a log line is passed through a `Redactor` first, so
that a loaded token can never leak through an event name, an error message, or
an HTTP response snippet embedded in an exception.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TextIO

_MASK = "[REDACTED]"

# Well-known secret shapes, in addition to the exact loaded secret values.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){1,2}"),
    # Catches any "Basic <token>" HTTP-auth header value, whether or not the
    # underlying secret is one we loaded (e.g. MirrorManager's git
    # `http.extraHeader`, or a header echoed back in an error body). The
    # base64-charset + minimum-length requirement (a real credential, never
    # under ~16 chars) keeps this from masking the ordinary English word
    # "basic" followed by any other word ("a basic setup", "basic auth" as
    # prose) -- `\S+` alone did exactly that.
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}"),
)

_MAX_ERROR_CHARS = 500


class Redactor:
    """Masks known secret values and secret-shaped substrings in text."""

    def __init__(self, secrets: Iterable[str]) -> None:
        literals: set[str] = set()
        for value in secrets:
            if not value:
                continue
            literals.add(value)
            # MirrorManager sends git's `http.extraHeader` as
            # `base64(f"x-access-token:{token}")`; also cover a plain
            # base64 of the bare token in case it is logged elsewhere.
            literals.add(base64.b64encode(value.encode()).decode())
            literals.add(base64.b64encode(f"x-access-token:{value}".encode()).decode())
        # Longest first, so a secret that is a substring of another is not
        # left partially unmasked.
        self._literals: tuple[str, ...] = tuple(sorted(literals, key=len, reverse=True))

    def redact(self, text: str) -> str:
        if not text:
            return text
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, _MASK)
        for pattern in _PATTERNS:
            text = pattern.sub(_MASK, text)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings inside dicts/lists/tuples; pass through the rest."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Logger:
    """Writes one redacted JSON object per line to stderr."""

    def __init__(self, redactor: Redactor, *, stream: TextIO | None = None) -> None:
        self._redactor = redactor
        self._stream = stream if stream is not None else sys.stderr

    def event(
        self,
        event: str,
        *,
        level: str = "info",
        lane: str | None = None,
        run_id: str | None = None,
        task_id: int | str | None = None,
        stage: str | None = None,
        duration_ms: float | None = None,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "level": level,
            "event": self._redactor.redact(event),
        }
        for key, value in (
            ("lane", lane),
            ("run_id", run_id),
            ("task_id", task_id),
            ("stage", stage),
            ("duration_ms", duration_ms),
        ):
            if value is not None:
                record[key] = value
        for key, value in extra.items():
            record[key] = self._redactor.redact_value(value)
        self._write(record)

    def error(self, exc: BaseException, *, event: str = "error", **fields: Any) -> None:
        message = self._redactor.redact(str(exc))[:_MAX_ERROR_CHARS]
        self.event(
            event,
            level="error",
            error_type=type(exc).__name__,
            error=message,
            **fields,
        )

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True)
        # Field-level redaction (`redact_value`) does not reach values that
        # `json.dumps(..., default=str)` stringifies at serialization time,
        # nor dict keys. Scrubbing the fully serialized line is the one place
        # that is guaranteed to catch every one of those.
        line = self._redactor.redact(line)
        self._stream.write(line + "\n")
        self._stream.flush()
