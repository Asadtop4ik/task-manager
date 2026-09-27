from __future__ import annotations

import email.message
import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any

from agent_svc.api import (
    IntakeImage,
    InvalidResponse,
    InvalidWork,
    LeaseLost,
    TaskManagerApi,
    parse_discussion_lease,
    parse_intake_lease,
)
from agent_svc.http import JsonHttp

CATALOG = {"Asadtop4ik/task-manager": "main", "muradjanov-dev/ketoshop": "master"}
BASE_URL = "https://tasks.example.test/api/v1"
# The contract sets lease_id via uuid4(); every method now validates it as one.
LEASE_ID = "22222222-2222-2222-2222-222222222222"


def _work_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "kind": "implement",
        "lease_id": LEASE_ID,
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
    def __init__(
        self, body: bytes = b"", *, status: int = 200, mime: str | None = None
    ) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": mime} if mime else {}

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

    def test_non_uuid_lease_id_rejected(self) -> None:
        bad = _work_payload(lease_id="not-a-uuid")
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork):
            api.lease("code")

    def test_fast_mode_is_rejected(self) -> None:
        # `!fast` runs always stay on the GitHub executor; a local lease
        # response claiming mode="fast" must never be accepted.
        bad = _work_payload(mode="fast")
        api, _opener = _api([FakeResponse(json.dumps(bad).encode(), status=200)])
        with self.assertRaises(InvalidWork):
            api.lease("code")


class HeartbeatTests(unittest.TestCase):
    def test_success_returns_lease_until(self) -> None:
        body = json.dumps({"lease_until": "2026-09-26T12:05:00+00:00"}).encode()
        api, opener = _api([FakeResponse(body, status=200)])
        lease_until = api.heartbeat("run-1", LEASE_ID)
        self.assertEqual(lease_until.isoformat(), "2026-09-26T12:05:00+00:00")
        self.assertEqual(opener.requests[0].get_header("X-agent-lease-id"), LEASE_ID)

    def test_409_raises_lease_lost_with_detail(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "lease_expired"})])
        with self.assertRaises(LeaseLost) as ctx:
            api.heartbeat("run-1", LEASE_ID)
        self.assertEqual(ctx.exception.detail, "lease_expired")

    def test_non_uuid_lease_id_rejected_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.heartbeat("run-1", "not-a-uuid")
        self.assertEqual(opener.requests, [])


class StageTests(unittest.TestCase):
    def test_unknown_stage_rejected_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.stage("run-1", LEASE_ID, "not-a-real-stage")
        self.assertEqual(opener.requests, [])

    def test_valid_stage_sends_lease_header_and_body(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        api.stage("run-1", LEASE_ID, "codex_started", error=None)
        request = opener.requests[0]
        self.assertEqual(request.get_header("X-agent-lease-id"), LEASE_ID)
        sent = json.loads(request.data)
        self.assertEqual(sent, {"lease_id": LEASE_ID, "stage": "codex_started", "error": None})

    def test_409_raises_lease_lost(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "cancelled"})])
        with self.assertRaises(LeaseLost) as ctx:
            api.stage("run-1", LEASE_ID, "leased")
        self.assertEqual(ctx.exception.detail, "cancelled")

    def test_non_uuid_lease_id_rejected_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.stage("run-1", "not-a-uuid", "leased")
        self.assertEqual(opener.requests, [])


class CallbackFamilyTests(unittest.TestCase):
    def test_callback_uses_callback_token_and_lease_header(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        result = api.callback("run-1", LEASE_ID, {"status": "running"})
        self.assertIsNone(result)
        request = opener.requests[0]
        self.assertEqual(request.get_header("X-agent-callback-token"), "callback-token")
        self.assertEqual(request.get_header("X-agent-lease-id"), LEASE_ID)
        self.assertEqual(json.loads(request.data), {"status": "running"})

    def test_callback_rejects_a_non_uuid_lease_id_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.callback("run-1", "not-a-uuid", {"status": "running"})
        self.assertEqual(opener.requests, [])

    def test_review_result_409_raises_lease_lost(self) -> None:
        api, _opener = _api([_http_error(409, {"detail": "lease_mismatch"})])
        with self.assertRaises(LeaseLost):
            api.review_result("run-1", LEASE_ID, {"sha": "a" * 40})

    def test_action_result_rejects_a_non_uuid_lease_id_before_any_request(self) -> None:
        api, opener = _api([])
        with self.assertRaises(ValueError):
            api.action_result("run-1", "not-a-uuid", {"action_id": "a1"})
        self.assertEqual(opener.requests, [])

    def test_action_result_returns_parsed_body(self) -> None:
        api, _opener = _api(
            [FakeResponse(json.dumps({"run_id": "run-1"}).encode(), status=200)]
        )
        result = api.action_result(
            "run-1", LEASE_ID, {"action_id": "a1", "status": "completed"}
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


def _intake_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 34,
        "revision": 3,
        "lease_id": LEASE_ID,
        "text": "Task",
        "answer_text": None,
        "mode": "pr",
        "images": [],
        "repo_full_name": "Asadtop4ik/task-manager",
        "base_branch": "main",
        "analysis_rounds": 0,
    }
    payload.update(overrides)
    return payload


def _discussion_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 5,
        "revision": 1,
        "lease_id": LEASE_ID,
        "repo_full_name": "Asadtop4ik/task-manager",
        "base_branch": "main",
        "project_key": "task-manager",
        "diagnostics_enabled": False,
        "thread_id": None,
        "text": "Salom",
        "images": [],
    }
    payload.update(overrides)
    return payload


class IntakeLeaseParsingTests(unittest.TestCase):
    def test_valid_payload_parses(self) -> None:
        lease = parse_intake_lease(_intake_payload(), CATALOG)
        self.assertEqual(lease.intake_id, 34)
        self.assertEqual(lease.repo_full_name, "Asadtop4ik/task-manager")
        self.assertEqual(lease.images, ())

    def test_unapproved_repository_rejected(self) -> None:
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(_intake_payload(repo_full_name="someone-else/repo"), CATALOG)

    def test_unapproved_branch_for_an_approved_repository_rejected(self) -> None:
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(
                _intake_payload(repo_full_name="muradjanov-dev/ketoshop", base_branch="main"),
                CATALOG,
            )

    def test_non_uuid_lease_id_rejected(self) -> None:
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(_intake_payload(lease_id="not-a-uuid"), CATALOG)

    def test_fast_mode_accepted(self) -> None:
        lease = parse_intake_lease(_intake_payload(mode="fast"), CATALOG)
        self.assertEqual(lease.mode, "fast")

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(_intake_payload(mode="fastest"), CATALOG)

    def test_image_with_unsupported_mime_rejected(self) -> None:
        images = [{"mime": "image/gif", "size": 1}]
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(_intake_payload(images=images), CATALOG)

    def test_more_than_three_images_rejected(self) -> None:
        images = [{"mime": "image/png", "size": 1}] * 4
        with self.assertRaises(InvalidResponse):
            parse_intake_lease(_intake_payload(images=images), CATALOG)

    def test_valid_image_metadata_parses(self) -> None:
        images = [{"mime": "image/png", "size": 4}]
        lease = parse_intake_lease(_intake_payload(images=images), CATALOG)
        self.assertEqual(lease.images, (IntakeImage(mime="image/png", size=4),))


class DiscussionLeaseParsingTests(unittest.TestCase):
    def test_valid_payload_parses(self) -> None:
        lease = parse_discussion_lease(_discussion_payload(), CATALOG)
        self.assertEqual(lease.discussion_id, 5)
        self.assertFalse(lease.diagnostics_enabled)

    def test_diagnostics_only_allowed_for_ketoshop_project_key(self) -> None:
        payload = _discussion_payload(
            repo_full_name="muradjanov-dev/ketoshop",
            base_branch="master",
            project_key="task-manager",
            diagnostics_enabled=True,
        )
        with self.assertRaises(InvalidResponse):
            parse_discussion_lease(payload, CATALOG)

    def test_diagnostics_only_allowed_for_the_ketoshop_repository(self) -> None:
        payload = _discussion_payload(
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            project_key="ketoshop",
            diagnostics_enabled=True,
        )
        with self.assertRaises(InvalidResponse):
            parse_discussion_lease(payload, CATALOG)

    def test_diagnostics_enabled_for_ketoshop_parses(self) -> None:
        payload = _discussion_payload(
            repo_full_name="muradjanov-dev/ketoshop",
            base_branch="master",
            project_key="ketoshop",
            diagnostics_enabled=True,
        )
        lease = parse_discussion_lease(payload, CATALOG)
        self.assertTrue(lease.diagnostics_enabled)

    def test_invalid_thread_id_rejected(self) -> None:
        with self.assertRaises(InvalidResponse):
            parse_discussion_lease(_discussion_payload(thread_id="has spaces"), CATALOG)

    def test_valid_thread_id_parses(self) -> None:
        lease = parse_discussion_lease(_discussion_payload(thread_id="thr_abc-1"), CATALOG)
        self.assertEqual(lease.thread_id, "thr_abc-1")


class IntakeApiTests(unittest.TestCase):
    def test_lease_intake_204_returns_none(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        self.assertIsNone(api.lease_intake())
        self.assertEqual(
            opener.requests[0].get_header("X-intake-worker-token"), "intake-token"
        )
        self.assertTrue(opener.requests[0].full_url.endswith("/agent-intakes/lease"))

    def test_lease_intake_parses_valid_payload(self) -> None:
        api, _opener = _api([FakeResponse(json.dumps(_intake_payload()).encode(), status=200)])
        lease = api.lease_intake()
        assert lease is not None
        self.assertEqual(lease.intake_id, 34)

    def test_intake_image_sends_lease_header_and_returns_body_and_mime(self) -> None:
        api, opener = _api([FakeResponse(b"png!", status=200, mime="image/png")])
        body, mime = api.intake_image(34, LEASE_ID, 0)
        self.assertEqual((body, mime), (b"png!", "image/png"))
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/agent-intakes/34/images/0"))
        self.assertEqual(request.get_header("X-intake-lease-id"), LEASE_ID)
        self.assertEqual(request.get_header("X-intake-worker-token"), "intake-token")

    def test_report_intake_result_posts_revision_lease_id_and_result_fields(self) -> None:
        api, opener = _api([FakeResponse(b"", status=200)])
        api.report_intake_result(
            34, revision=3, lease_id=LEASE_ID, result={"status": "failed", "error": "x"}
        )
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/agent-intakes/34/result"))
        sent = json.loads(request.data)
        self.assertEqual(
            sent, {"revision": 3, "lease_id": LEASE_ID, "status": "failed", "error": "x"}
        )


class DiscussionApiTests(unittest.TestCase):
    def test_lease_discussion_204_returns_none(self) -> None:
        api, opener = _api([FakeResponse(b"", status=204)])
        self.assertIsNone(api.lease_discussion())
        self.assertTrue(opener.requests[0].full_url.endswith("/project-discussions/lease"))

    def test_lease_discussion_parses_valid_payload(self) -> None:
        api, _opener = _api(
            [FakeResponse(json.dumps(_discussion_payload()).encode(), status=200)]
        )
        lease = api.lease_discussion()
        assert lease is not None
        self.assertEqual(lease.discussion_id, 5)

    def test_discussion_image_sends_lease_header(self) -> None:
        api, opener = _api([FakeResponse(b"jpg!", status=200, mime="image/jpeg")])
        body, mime = api.discussion_image(5, LEASE_ID, 1)
        self.assertEqual((body, mime), (b"jpg!", "image/jpeg"))
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith("/project-discussions/5/images/1"))
        self.assertEqual(request.get_header("X-intake-lease-id"), LEASE_ID)

    def test_report_discussion_result_posts_expected_body(self) -> None:
        api, opener = _api([FakeResponse(b"", status=200)])
        api.report_discussion_result(
            5,
            revision=1,
            lease_id=LEASE_ID,
            thread_id="thr_1",
            response="Javob",
            error=None,
        )
        sent = json.loads(opener.requests[0].data)
        self.assertEqual(
            sent,
            {
                "revision": 1,
                "lease_id": LEASE_ID,
                "thread_id": "thr_1",
                "response": "Javob",
                "error": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
