from __future__ import annotations

import email.message
import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any

from agent_svc.api import InvalidWork, LeaseLost, TaskManagerApi
from agent_svc.http import JsonHttp

CATALOG = {"Asadtop4ik/task-manager": "main"}
BASE_URL = "https://tasks.example.test/api/v1"


def _work_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "kind": "implement",
        "lease_id": "lease-abc",
        "lease_until": "2026-09-26T12:00:00+00:00",
        "attempts": 1,
        "attempt_index": 1,
        "task_id": 42,
        "task_revision": "rev-1",
        "repo_full_name": "Asadtop4ik/task-manager",
        "base_branch": "main",
        "mode": "pr",
        "title": "Fix the thing",
        "description": "Do the fix",
        "image_count": 0,
        "complexity": "simple",
        "relevant_files": ["backend/app/main.py"],
        "branch": None,
        "pr_url": None,
        "pr_number": None,
        "head_sha": None,
        "action_id": None,
        "instruction": None,
        "expected_head_sha": None,
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, body: bytes = b"", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {}

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


def _http_error(status: int, payload: dict[str, Any] | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode() if payload is not None else b""
    return urllib.error.HTTPError(
        f"{BASE_URL}/agent-runs/x", status, "err", email.message.Message(), io.BytesIO(body)
    )


class FakeOpener:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _api(results: list[object]) -> tuple[TaskManagerApi, FakeOpener]:
    opener = FakeOpener(results)
    http = JsonHttp(opener=opener, sleep=lambda _s: None)
    api = TaskManagerApi(
        BASE_URL, "svc-token", "callback-token", "intake-token", http=http, catalog=CATALOG
    )
    return api, opener


class LeaseTests(unittest.TestCase):
    def test_204_returns_none(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        self.assertIsNone(api.lease("code"))
        self.assertEqual(opener.requests[0].get_header("X-agent-svc-token"), "svc-token")

    def test_valid_payload_parses_into_work(self) -> None:
        api, _opener = _api([FakeResponse(json.dumps(_work_payload()).encode(), status=200)])
        work = api.lease("code")
        assert work is not None
        self.assertEqual(work.run_id, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(work.kind, "implement")
        self.assertEqual(work.relevant_files, ("backend/app/main.py",))
        self.assertEqual(work.repo_full_name, "Asadtop4ik/task-manager")

    def test_unknown_repository_raises_invalid_work_with_run_id(self) -> None:
        bad = _work_payload(repo_full_name="someone-else/repo")
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork) as ctx:
            api.lease("code")
        self.assertEqual(ctx.exception.run_id, "11111111-1111-1111-1111-111111111111")

    def test_missing_kind_raises_invalid_work(self) -> None:
        bad = _work_payload()
        del bad["kind"]
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork):
            api.lease("code")

    def test_invalid_run_id_has_no_run_id_on_exception(self) -> None:
        bad = _work_payload(run_id="not-a-uuid")
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork) as ctx:
            api.lease("code")
        self.assertIsNone(ctx.exception.run_id)

    def test_unsafe_relevant_file_rejected(self) -> None:
        bad = _work_payload(relevant_files=["../etc/passwd"])
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork):
            api.lease("code")


class HeartbeatTests(unittest.TestCase):
    def test_success_returns_lease_until(self) -> None:
        body = json.dumps({"lease_until": "2026-09-26T12:05:00+00:00"}).encode()
        api, opener = _api([FakeResponse(body, status=200)])
        lease_until = api.heartbeat("run-1", "lease-1")
        self.assertEqual(lease_until.isoformat(), "2026-09-26T12:05:00+00:00")
        self.assertEqual(opener.requests[0].get_header("X-agent-lease-id"), "lease-1")

    def test_409_raises_lease_lost_with_detail(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "lease_expired"})])
        with self.assertRaises(LeaseLost) as ctx:
            api.heartbeat("run-1", "lease-1")
        self.assertEqual(ctx.exception.detail, "lease_expired")


class StageTests(unittest.TestCase):
    def test_unknown_stage_rejected_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.stage("run-1", "lease-1", "not-a-real-stage")
        self.assertEqual(opener.requests, [])

    def test_valid_stage_sends_lease_header_and_body(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        api.stage("run-1", "lease-1", "codex_started", error=None)
        request = opener.requests[0]
        self.assertEqual(request.get_header("X-agent-lease-id"), "lease-1")
        sent = json.loads(request.data)
        self.assertEqual(
            sent, {"lease_id": "lease-1", "stage": "codex_started", "error": None}
        )

    def test_409_raises_lease_lost(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "cancelled"})])
        with self.assertRaises(LeaseLost) as ctx:
            api.stage("run-1", "lease-1", "leased")
        self.assertEqual(ctx.exception.detail, "cancelled")


class CallbackFamilyTests(unittest.TestCase):
    def test_callback_uses_callback_token_and_lease_header(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        result = api.callback("run-1", "lease-1", {"status": "running"})
        self.assertIsNone(result)
        request = opener.requests[0]
        self.assertEqual(request.get_header("X-agent-callback-token"), "callback-token")
        self.assertEqual(request.get_header("X-agent-lease-id"), "lease-1")
        self.assertEqual(json.loads(request.data), {"status": "running"})

    def test_review_result_409_raises_lease_lost(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "lease_mismatch"})])
        with self.assertRaises(LeaseLost):
            api.review_result("run-1", "lease-1", {"sha": "a" * 40})

    def test_action_result_returns_parsed_body(self) -> None:
        api, _opener = _api(
            [FakeResponse(json.dumps({"run_id": "run-1"}).encode(), status=200)]
        )
        result = api.action_result(
            "run-1", "lease-1", {"action_id": "a1", "status": "completed"}
        )
        self.assertEqual(result, {"run_id": "run-1"})


class StatusAndMonitorTests(unittest.TestCase):
    def test_status_uses_callback_token_and_get(self) -> None:
        api, opener = _api(
            [FakeResponse(json.dumps({"status": "pr_opened"}).encode(), status=200)]
        )
        result = api.status("run-1")
        self.assertEqual(result, {"status": "pr_opened"})
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-agent-callback-token"), "callback-token")

    def test_ci_pending_includes_after_id_in_query(self) -> None:
        api, opener = _api([FakeResponse(b"[]", status=200)])
        result = api.ci_pending(after_id=5)
        self.assertEqual(result, [])
        self.assertIn("after_id=5", opener.requests[0].full_url)

    def test_ci_result_posts_expected_body(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        api.ci_result("run-1", sha="a" * 40, conclusion="success", github_run_url="https://x")
        sent = json.loads(opener.requests[0].data)
        self.assertEqual(
            sent, {"sha": "a" * 40, "conclusion": "success", "github_run_url": "https://x"}
        )

    def test_merged_and_deployed_post_to_expected_paths(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204), FakeResponse(b"", status=204)])
        api.merged("run-1", sha="a" * 40)
        api.deployed("run-1", sha="a" * 40, github_run_url="https://x")
        self.assertTrue(opener.requests[0].full_url.endswith("/run-1/merged"))
        self.assertTrue(opener.requests[1].full_url.endswith("/run-1/deployed"))


if __name__ == "__main__":
    unittest.main()
