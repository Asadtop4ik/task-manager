from __future__ import annotations

import email.message
import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any

from agent_svc.github import (
    GitHubClient,
    InvalidResponse,
    UnknownRepository,
    build_token_selector,
)
from agent_svc.http import JsonHttp

DISPATCH_REPO = "Asadtop4ik/task-manager"
QA_REPO = "Asadtop4ik/agent-qa"
PUBLIC_REPOS = ("muradjanov-dev/qurbot", "muradjanov-dev/kans-shop")


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


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/x", status, "err", email.message.Message(), io.BytesIO(body)
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


def _token_for() -> Any:
    return build_token_selector(
        dispatch_repo=DISPATCH_REPO,
        qa_repo=QA_REPO,
        public_repos=PUBLIC_REPOS,
        agent_token="agent-token",
        qa_token="qa-token",
        public_token="public-token",
    )


def _client(results: list[object]) -> tuple[GitHubClient, FakeOpener]:
    opener = FakeOpener(results)
    http = JsonHttp(opener=opener, sleep=lambda _s: None)
    return GitHubClient(token_for=_token_for(), http=http), opener


class TokenSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token_for = _token_for()

    def test_dispatch_repo_uses_agent_token(self) -> None:
        self.assertEqual(self.token_for(DISPATCH_REPO), "agent-token")

    def test_qa_repo_uses_qa_token(self) -> None:
        self.assertEqual(self.token_for(QA_REPO), "qa-token")

    def test_public_repo_uses_public_token(self) -> None:
        self.assertEqual(self.token_for(PUBLIC_REPOS[0]), "public-token")

    def test_unknown_repo_is_refused(self) -> None:
        with self.assertRaises(UnknownRepository):
            self.token_for("someone-else/unapproved")

    def test_qa_repo_is_refused_when_qa_token_is_empty(self) -> None:
        # An empty github_qa_token means the secret was never provisioned
        # (config.load_secrets treats it as optional): the QA repo must then
        # be refused rather than authenticated with an empty credential.
        token_for = build_token_selector(
            dispatch_repo=DISPATCH_REPO,
            qa_repo=QA_REPO,
            public_repos=PUBLIC_REPOS,
            agent_token="agent-token",
            qa_token="",
            public_token="public-token",
        )
        with self.assertRaises(UnknownRepository):
            token_for(QA_REPO)
        # Everything else is unaffected by the missing QA token.
        self.assertEqual(token_for(DISPATCH_REPO), "agent-token")
        self.assertEqual(token_for(PUBLIC_REPOS[0]), "public-token")


class GetRefTests(unittest.TestCase):
    def test_existing_ref_returns_sha(self) -> None:
        sha = "a" * 40
        client, opener = _client([FakeResponse(json.dumps({"object": {"sha": sha}}).encode())])
        result = client.get_ref(DISPATCH_REPO, "main")
        self.assertEqual(result, sha)
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer agent-token")

    def test_missing_ref_returns_none(self) -> None:
        client, _opener = _client([_http_error(404)])
        self.assertIsNone(client.get_ref(DISPATCH_REPO, "no-such-branch"))

    def test_invalid_sha_shape_raises(self) -> None:
        client, _opener = _client(
            [FakeResponse(json.dumps({"object": {"sha": "xyz"}}).encode())]
        )
        with self.assertRaises(InvalidResponse):
            client.get_ref(DISPATCH_REPO, "main")


class CreatePullTests(unittest.TestCase):
    def test_posts_expected_body_and_returns_payload(self) -> None:
        payload = {"number": 7, "html_url": "https://github.com/x/y/pull/7"}
        client, opener = _client([FakeResponse(json.dumps(payload).encode())])
        result = client.create_pull(
            DISPATCH_REPO, head="codex/task-1", base="main", title="t", body="b"
        )
        self.assertEqual(result["number"], 7)
        sent = json.loads(opener.requests[0].data)
        self.assertEqual(
            sent, {"head": "codex/task-1", "base": "main", "title": "t", "body": "b"}
        )

    def test_missing_number_raises_invalid_response(self) -> None:
        client, _opener = _client([FakeResponse(json.dumps({}).encode())])
        with self.assertRaises(InvalidResponse):
            client.create_pull(DISPATCH_REPO, head="h", base="main", title="t", body="b")


class PullDiffTests(unittest.TestCase):
    def test_sets_diff_accept_header(self) -> None:
        client, opener = _client([FakeResponse(b"diff --git a b\n")])
        client.pull_diff(DISPATCH_REPO, 7)
        self.assertEqual(
            opener.requests[0].get_header("Accept"), "application/vnd.github.diff"
        )

    def test_truncates_to_250k_chars(self) -> None:
        big = ("x" * 300_000).encode()
        client, _opener = _client([FakeResponse(big)])
        result = client.pull_diff(DISPATCH_REPO, 7)
        self.assertEqual(len(result), 250_000)


class SetStatusTests(unittest.TestCase):
    def test_rejects_unknown_state(self) -> None:
        client, opener = _client([])
        with self.assertRaises(ValueError):
            client.set_status(DISPATCH_REPO, "a" * 40, "weird", "ctx", "desc")
        self.assertEqual(opener.requests, [])

    def test_rejects_invalid_sha(self) -> None:
        client, opener = _client([])
        with self.assertRaises(ValueError):
            client.set_status(DISPATCH_REPO, "not-a-sha", "success", "ctx", "desc")
        self.assertEqual(opener.requests, [])

    def test_posts_to_statuses_endpoint(self) -> None:
        sha = "b" * 40
        client, opener = _client([FakeResponse(b"{}", status=201)])
        client.set_status(DISPATCH_REPO, sha, "success", "ci/agent", "all good")
        self.assertTrue(opener.requests[0].full_url.endswith(f"/statuses/{sha}"))


class WorkflowRunsTests(unittest.TestCase):
    def test_list_workflow_runs_returns_run_list(self) -> None:
        payload = {"workflow_runs": [{"id": 1}, {"id": 2}]}
        client, _opener = _client([FakeResponse(json.dumps(payload).encode())])
        runs = client.list_workflow_runs(
            DISPATCH_REPO, "ci.yml", event="pull_request", head_sha="a" * 40
        )
        self.assertEqual(len(runs), 2)

    def test_run_jobs_returns_job_list(self) -> None:
        payload = {"jobs": [{"name": "gate", "conclusion": "success"}]}
        client, _opener = _client([FakeResponse(json.dumps(payload).encode())])
        jobs = client.run_jobs(DISPATCH_REPO, 123)
        self.assertEqual(jobs[0]["name"], "gate")

    def test_missing_run_list_raises_invalid_response(self) -> None:
        client, _opener = _client([FakeResponse(json.dumps({}).encode())])
        with self.assertRaises(InvalidResponse):
            client.list_workflow_runs(DISPATCH_REPO, "ci.yml", event="push", head_sha="a" * 40)


if __name__ == "__main__":
    unittest.main()
