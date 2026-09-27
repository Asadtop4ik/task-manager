"""A small urllib wrapper: JSON requests, size caps, and GET-only retries.

Follows the pattern in `ops/intake_worker.py`: auth headers are attached with
`add_unredirected_header` so they are dropped across a redirect, and the
opener is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .log import Redactor

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_JSON_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TEXT_BYTES = 1024 * 1024
_RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)
_SNIPPET_CHARS = 500


class HttpError(Exception):
    """A non-2xx HTTP response, or a network failure with no response at all."""

    def __init__(self, status: int, snippet: str) -> None:
        super().__init__(f"HTTP {status}: {snippet}")
        self.status = status
        self.snippet = snippet


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class JsonHttp:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
        max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
        redactor: Redactor | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout
        self.max_json_bytes = max_json_bytes
        self.max_text_bytes = max_text_bytes
        self._redactor = redactor
        self._sleep = sleep or time.sleep

    @staticmethod
    def build_request(
        method: str,
        url: str,
        *,
        auth_header: tuple[str, str] | None = None,
        extra_headers: Iterable[tuple[str, str]] = (),
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> urllib.request.Request:
        headers = {"Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        if auth_header is not None:
            name, value = auth_header
            request.add_unredirected_header(name, value)
        for name, value in extra_headers:
            request.add_unredirected_header(name, value)
        return request

    def send(
        self,
        request: urllib.request.Request,
        *,
        max_bytes: int | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """`timeout` overrides this instance's own default for this one
        call only (used by the chat lane to clamp a call to whatever
        remains of its own job budget); omitted, this reproduces the exact
        prior behavior."""
        cap = max_bytes if max_bytes is not None else self.max_json_bytes
        call_timeout = timeout if timeout is not None else self._timeout
        idempotent_get = request.get_method() == "GET"
        attempts = (len(_RETRY_BACKOFF_S) + 1) if idempotent_get else 1
        last_error: HttpError | None = None
        for attempt in range(attempts):
            try:
                return self._send_once(request, cap, call_timeout)
            except HttpError as exc:
                if not idempotent_get or exc.status < 500:
                    raise
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = HttpError(0, self._snippet(str(exc.reason)))
            if attempt < attempts - 1:
                self._sleep(_RETRY_BACKOFF_S[attempt])
        assert last_error is not None
        raise last_error

    def _send_once(
        self, request: urllib.request.Request, cap: int, timeout: float
    ) -> HttpResponse:
        try:
            with self._opener(request, timeout=timeout) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read(cap + 1)
                headers = dict(getattr(response, "headers", {}) or {})
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(cap + 1)
            headers = dict(getattr(exc, "headers", {}) or {})
        if len(raw) > cap:
            raise HttpError(status, "response exceeded the size cap")
        if status >= 400:
            raise HttpError(status, self._snippet(raw.decode("utf-8", "replace")))
        return HttpResponse(status=status, body=raw, headers=headers)

    def _snippet(self, text: str) -> str:
        text = text[:_SNIPPET_CHARS]
        return self._redactor.redact(text) if self._redactor is not None else text

    @staticmethod
    def json(response: HttpResponse) -> Any:
        if not response.body:
            return None
        return json.loads(response.body)
