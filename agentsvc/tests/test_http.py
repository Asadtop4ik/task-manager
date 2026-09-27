from __future__ import annotations

import email.message
import io
import unittest
import urllib.error

from agent_svc.http import HttpError, JsonHttp
from agent_svc.log import Redactor


class FakeResponse:
    def __init__(
        self, body: bytes = b"", *, status: int = 200, headers: dict | None = None
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self.body = self.body, b""
        else:
            data, self.body = self.body[:size], self.body[size:]
        return data


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://example.test/x", status, "err", email.message.Message(), io.BytesIO(body)
    )


class FakeOpener:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.calls: list[object] = []

    def __call__(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.calls.append(request)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


class BuildRequestTests(unittest.TestCase):
    def test_auth_header_set_via_add_unredirected_header(self) -> None:
        request = JsonHttp.build_request(
            "GET", "http://x/y", auth_header=("X-Agent-Svc-Token", "abc")
        )
        self.assertEqual(request.get_header("X-agent-svc-token"), "abc")

    def test_extra_headers_and_content_type(self) -> None:
        request = JsonHttp.build_request(
            "POST",
            "http://x/y",
            extra_headers=(("X-Agent-Lease-ID", "lease-1"),),
            body=b"{}",
            content_type="application/json",
        )
        self.assertEqual(request.get_header("X-agent-lease-id"), "lease-1")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.data, b"{}")


class JsonHttpSendTests(unittest.TestCase):
    def test_get_success_returns_body(self) -> None:
        opener = FakeOpener([FakeResponse(b'{"ok": true}', status=200)])
        http = JsonHttp(opener=opener)
        request = JsonHttp.build_request("GET", "http://x/y")
        response = http.send(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(http.json(response), {"ok": True})

    def test_empty_body_decodes_to_none(self) -> None:
        opener = FakeOpener([FakeResponse(b"", status=204)])
        http = JsonHttp(opener=opener)
        response = http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertIsNone(http.json(response))

    def test_get_retries_on_5xx_then_succeeds(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(
            [_http_error(502, b"bad gateway"), FakeResponse(b"{}", status=200)]
        )
        http = JsonHttp(opener=opener, sleep=sleeps.append)
        response = http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertEqual(response.status, 200)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(sleeps, [0.5])

    def test_get_exhausts_retries_and_raises(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener([_http_error(500, b"x") for _ in range(4)])
        http = JsonHttp(opener=opener, sleep=sleeps.append)
        with self.assertRaises(HttpError) as ctx:
            http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])

    def test_get_does_not_retry_on_4xx(self) -> None:
        opener = FakeOpener([_http_error(404, b"missing")])
        http = JsonHttp(opener=opener, sleep=lambda _s: None)
        with self.assertRaises(HttpError) as ctx:
            http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(len(opener.calls), 1)

    def test_post_never_retries_on_5xx(self) -> None:
        opener = FakeOpener([_http_error(500, b"x"), FakeResponse(b"{}", status=200)])
        http = JsonHttp(opener=opener, sleep=lambda _s: None)
        with self.assertRaises(HttpError):
            http.send(JsonHttp.build_request("POST", "http://x/y", body=b"{}"))
        self.assertEqual(len(opener.calls), 1)

    def test_response_over_size_cap_raises(self) -> None:
        opener = FakeOpener([FakeResponse(b"x" * 100, status=200)])
        http = JsonHttp(opener=opener, max_json_bytes=10)
        with self.assertRaises(HttpError) as ctx:
            http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertIn("size cap", ctx.exception.snippet)

    def test_error_snippet_is_redacted(self) -> None:
        opener = FakeOpener([_http_error(500, b"token leaked-secret-here failed")])
        http = JsonHttp(opener=opener, redactor=Redactor(["leaked-secret-here"]))
        with self.assertRaises(HttpError) as ctx:
            http.send(JsonHttp.build_request("POST", "http://x/y", body=b"{}"))
        self.assertNotIn("leaked-secret-here", ctx.exception.snippet)

    def test_url_error_without_response_is_wrapped(self) -> None:
        opener = FakeOpener([urllib.error.URLError("connection refused") for _ in range(4)])
        http = JsonHttp(opener=opener, sleep=lambda _s: None)
        with self.assertRaises(HttpError) as ctx:
            http.send(JsonHttp.build_request("GET", "http://x/y"))
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(len(opener.calls), 4)


if __name__ == "__main__":
    unittest.main()
