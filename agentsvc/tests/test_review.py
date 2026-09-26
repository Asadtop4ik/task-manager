from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any

from agent_svc.api import LeaseLost, Work
from agent_svc.codex import CodexResult
from agent_svc.log import Logger, Redactor
from agent_svc.review import GENERIC_FAILURE_SUMMARY, handle_review

_TRUSTED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent_pr_review.py"


def _load_trusted_agent_pr_review() -> ModuleType:
    """Load the real trusted script, exactly as `agent_svc.trusted` will."""
    spec = importlib.util.spec_from_file_location(
        "agent_svc._test_trusted_agent_pr_review", _TRUSTED_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TRUSTED = _load_trusted_agent_pr_review()


def _logger() -> Logger:
    return Logger(Redactor([]), stream=io.StringIO())


def _work(
    *,
    run_id: str = "11111111-1111-1111-1111-111111111111",
    repo: str = "Owner/task-manager",
    branch: str | None = "codex/task-1-11111111-1111-1111-1111-111111111111",
    head_sha: str = "a" * 40,
    pr_number: int | None = 7,
) -> Work:
    return Work(
        run_id=run_id,
        kind="review",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=1,
        task_revision=1,
        repo_full_name=repo,
        base_branch="main",
        mode="pr",
        title="t",
        description="d",
        image_count=0,
        complexity=None,
        relevant_files=(),
        branch=branch,
        pr_url=f"https://github.com/{repo}/pull/{pr_number}" if pr_number else None,
        pr_number=pr_number,
        head_sha=head_sha,
        action_id=None,
        instruction=None,
        expected_head_sha=None,
    )


def _pr(
    *,
    sha: str = "a" * 40,
    state: str = "open",
    merged: bool = False,
    head_repo: str = "Owner/task-manager",
    ref: str = "codex/task-1-11111111-1111-1111-1111-111111111111",
    merge_commit_sha: str | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "head": {"sha": sha, "ref": ref, "repo": {"full_name": head_repo}},
    }


class FakeApi:
    def __init__(
        self,
        *,
        heartbeat_exception: BaseException | None = None,
        review_result_exceptions: list[BaseException | None] | None = None,
    ) -> None:
        self.stage_calls: list[tuple[str, str, str, str | None]] = []
        self.heartbeat_calls: list[tuple[str, str]] = []
        self.review_result_calls: list[tuple[str, str, dict[str, Any]]] = []
        self._heartbeat_exception = heartbeat_exception
        self._heartbeat_exception_after = 0
        # Popped one at a time per `review_result` call; once exhausted (or
        # if never given), calls just succeed and get recorded.
        self._review_result_exceptions = list(review_result_exceptions or [])

    def stage(
        self, run_id: str, lease_id: str, stage: str, *, error: str | None = None
    ) -> None:
        self.stage_calls.append((run_id, lease_id, stage, error))

    def heartbeat(self, run_id: str, lease_id: str) -> datetime:
        self.heartbeat_calls.append((run_id, lease_id))
        if self._heartbeat_exception is not None and len(self.heartbeat_calls) > (
            self._heartbeat_exception_after
        ):
            raise self._heartbeat_exception
        return datetime.now(UTC)

    def review_result(self, run_id: str, lease_id: str, payload: dict[str, Any]) -> None:
        if self._review_result_exceptions:
            exc = self._review_result_exceptions.pop(0)
            if exc is not None:
                raise exc
        self.review_result_calls.append((run_id, lease_id, dict(payload)))


class FakeGitHub:
    def __init__(self, *, pulls: list[dict[str, Any]], diff: str = "diff --git a b\n") -> None:
        self._pulls = list(pulls)
        self.diff = diff
        self.set_status_calls: list[tuple[Any, ...]] = []
        self.dispatch_calls: list[tuple[str, str, dict[str, Any]]] = []

    def get_pull(self, _repo: str, _number: int) -> dict[str, Any]:
        if len(self._pulls) > 1:
            return self._pulls.pop(0)
        return self._pulls[0]

    def pull_diff(self, _repo: str, _number: int) -> str:
        return self.diff

    def set_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str,
        *,
        target_url: str | None = None,
    ) -> None:
        self.set_status_calls.append((repo, sha, state, context, description, target_url))

    def dispatch(self, repo: str, event_type: str, client_payload: dict[str, Any]) -> None:
        self.dispatch_calls.append((repo, event_type, dict(client_payload)))


class FakeCodexRunner:
    def __init__(self, result: CodexResult, *, heartbeat_calls: int = 0) -> None:
        self.result = result
        self._heartbeat_calls = heartbeat_calls
        self.requests: list[dict[str, Any]] = []
        self.cleanup_calls: list[dict[str, Any]] = []

    def run_exec(
        self,
        request: dict[str, Any],
        *,
        on_event: Any,
        heartbeat: Any = None,
        heartbeat_interval_s: float = 30.0,
        cancel: Event | None = None,
    ) -> CodexResult:
        self.requests.append(request)
        if heartbeat is not None:
            for _ in range(self._heartbeat_calls):
                if not heartbeat():
                    return CodexResult(
                        exit_code=-1,
                        timed_out=False,
                        cancelled=True,
                        idle_killed=False,
                        final_message="",
                        usage=None,
                        stderr_tail=[],
                        frame=None,
                    )
        return self.result

    def cleanup(self, request: dict[str, Any]) -> dict[str, Any]:
        self.cleanup_calls.append(request)
        return {"ok": True}


def _ok_result(
    summary: str = "Independent Codex review clean", findings: list[Any] | None = None
) -> CodexResult:
    payload = json.dumps({"summary": summary, "findings": findings or []})
    return CodexResult(
        exit_code=0,
        timed_out=False,
        cancelled=False,
        idle_killed=False,
        final_message=payload,
        usage={"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        stderr_tail=[],
        frame={"type": "agent_svc.result"},
    )


def _ctx(
    *, api: FakeApi, github: FakeGitHub, codex: FakeCodexRunner, dispatch_repo: str
) -> Any:
    work_root = tempfile.mkdtemp()
    settings = SimpleNamespace(
        work_root=work_root,
        model_matrix={"review": {"model": "gpt-6-sol", "effort": "medium"}},
        timeouts={"review": 480},
        idle_timeout_s=480,
    )
    return SimpleNamespace(
        settings=settings,
        logger=_logger(),
        api=api,
        github=github,
        codex=codex,
        trusted=SimpleNamespace(agent_pr_review=_TRUSTED),
        catalog=SimpleNamespace(dispatch_repo=dispatch_repo),
    )


class HandleReviewTests(unittest.TestCase):
    def test_happy_path_clean_posts_success(self) -> None:
        work = _work()
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()])
        codex = FakeCodexRunner(_ok_result(), heartbeat_calls=2)
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(len(api.review_result_calls), 1)
        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "clean")
        self.assertEqual(payload["sha"], work.head_sha)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(len(github.set_status_calls), 1)
        self.assertEqual(github.set_status_calls[0][2], "success")
        self.assertEqual(github.set_status_calls[0][3], "codex-review")
        # heartbeat was actually wired through to api.heartbeat
        self.assertEqual(len(api.heartbeat_calls), 2)
        # dispatch fires because the repo is the configured dispatch repo
        self.assertEqual(len(github.dispatch_calls), 1)
        _repo, event_type, client_payload = github.dispatch_calls[0]
        self.assertEqual(event_type, "agent_review_completed")
        self.assertEqual(
            client_payload, {"pull_number": work.pr_number, "head_sha": work.head_sha}
        )
        # cleanup always runs
        self.assertEqual(codex.cleanup_calls, [{"run_id": work.run_id}])
        # codex was asked to run read-only, in an empty cwd, on lane "code"
        request = codex.requests[0]
        self.assertEqual(request["cwd"], "empty")
        self.assertEqual(request["sandbox"], "read-only")
        self.assertEqual(request["lane"], "code")
        self.assertEqual(request["model"], "gpt-6-sol")
        self.assertEqual(request["effort"], "medium")
        # "review_started" was staged before the codex run
        self.assertIn((work.run_id, work.lease_id, "review_started", None), api.stage_calls)

    def test_happy_path_no_dispatch_for_non_dispatch_repo(self) -> None:
        work = _work(repo="muradjanov-dev/qurbot", branch=None)
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr(head_repo="muradjanov-dev/qurbot")])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(len(api.review_result_calls), 1)
        self.assertEqual(github.dispatch_calls, [])

    def test_happy_path_blocking_findings_posts_failure(self) -> None:
        work = _work()
        findings = [
            {"severity": "P1", "title": "bug", "evidence": "x = None; x.foo()"},
        ]
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()])
        codex = FakeCodexRunner(_ok_result(findings=findings))
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "findings")
        self.assertEqual(len(payload["findings"]), 1)
        self.assertEqual(github.set_status_calls[0][2], "failure")

    def test_stale_head_releases_the_lease_via_review_result(self) -> None:
        # The backend answers a review-result posted against a moved head
        # with 409, which `agent_svc.api` raises as `LeaseLost` -- and which
        # is exactly how the backend releases the review lease promptly
        # instead of leaving it to expire on its own (see
        # `_release_stale_head_lease`'s docstring).
        work = _work(head_sha="a" * 40)
        api = FakeApi(
            review_result_exceptions=[LeaseLost("review does not match current PR head")]
        )
        github = FakeGitHub(pulls=[_pr(sha="b" * 40)])  # head moved
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(github.set_status_calls, [])
        self.assertEqual(github.dispatch_calls, [])
        self.assertEqual(codex.requests, [])  # never even started codex
        self.assertEqual(api.stage_calls, [])  # the review-result 409 already released it
        # review_result was attempted with the stale (pre-move) sha, state "error"
        self.assertEqual(len(api.review_result_calls), 0)  # it raised, so nothing recorded

    def test_stale_head_falls_back_to_stage_error_when_release_fails(self) -> None:
        # If posting the release call itself fails for some other reason
        # (e.g. a network error, not the expected 409), fall back to a stage
        # error so the failure is at least visible; the lease still expires
        # naturally without burning an "attempts" counter (review leases
        # never use that counter).
        work = _work(head_sha="a" * 40)
        api = FakeApi(review_result_exceptions=[RuntimeError("network down")])
        github = FakeGitHub(pulls=[_pr(sha="b" * 40)])  # head moved
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(github.set_status_calls, [])
        self.assertEqual(github.dispatch_calls, [])
        self.assertEqual(codex.requests, [])
        self.assertEqual(len(api.stage_calls), 1)
        run_id, lease_id, stage, error = api.stage_calls[0]
        self.assertEqual(
            (run_id, lease_id, stage), (work.run_id, work.lease_id, "review_started")
        )
        assert error is not None
        self.assertIn("head changed", error)

    def test_closed_pr_releases_the_lease_via_review_result(self) -> None:
        work = _work()
        api = FakeApi(review_result_exceptions=[LeaseLost("agent PR is not awaiting review")])
        github = FakeGitHub(pulls=[_pr(state="closed")])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(github.set_status_calls, [])
        self.assertEqual(codex.requests, [])
        self.assertEqual(api.stage_calls, [])

    def test_closed_pr_release_accepted_with_a_plain_200_is_also_fine(self) -> None:
        # A plain success response also clears the lease server-side; either
        # outcome of the release call is a correct, complete handling here.
        work = _work()
        api = FakeApi()  # review_result succeeds normally
        github = FakeGitHub(pulls=[_pr(state="closed")])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(len(api.review_result_calls), 1)
        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["sha"], work.head_sha)
        self.assertEqual(github.set_status_calls, [])  # release path never posts a status
        self.assertEqual(api.stage_calls, [])

    def test_oversized_diff_posts_error_review(self) -> None:
        work = _work()
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()], diff="x" * 250_001)
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(codex.requests, [])  # codex never invoked
        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["summary"], GENERIC_FAILURE_SUMMARY)
        self.assertEqual(github.set_status_calls[0][2], "error")

    def test_codex_failure_posts_error_review(self) -> None:
        work = _work()
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()])
        failed = CodexResult(
            exit_code=1,
            timed_out=False,
            cancelled=False,
            idle_killed=False,
            final_message="",
            usage=None,
            stderr_tail=["boom"],
            frame={"type": "agent_svc.result"},
        )
        codex = FakeCodexRunner(failed)
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")
        self.assertEqual(github.set_status_calls[0][2], "error")

    def test_malformed_codex_output_posts_error_review(self) -> None:
        work = _work()
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()])
        bad = CodexResult(
            exit_code=0,
            timed_out=False,
            cancelled=False,
            idle_killed=False,
            final_message="not json",
            usage=None,
            stderr_tail=[],
            frame={"type": "agent_svc.result"},
        )
        codex = FakeCodexRunner(bad)
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")

    def test_review_result_failure_does_not_post_success_status_or_dispatch(self) -> None:
        # If the real outcome's `review_result` call fails for a reason other
        # than LeaseLost, the backend never recorded it: the success/failure
        # status and the dispatch must NOT run against an outcome the
        # backend doesn't know about. Only the legacy error report should go
        # out (and it should succeed, since the fake only fails once).
        work = _work()
        api = FakeApi(review_result_exceptions=[RuntimeError("network down")])
        github = FakeGitHub(pulls=[_pr()])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        # Only the error report's review_result call was actually recorded
        # (the first, real "clean" one raised and was never appended).
        self.assertEqual(len(api.review_result_calls), 1)
        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")
        self.assertEqual(payload["summary"], GENERIC_FAILURE_SUMMARY)
        # The real "clean" success status must never be posted.
        self.assertEqual(len(github.set_status_calls), 1)
        self.assertEqual(github.set_status_calls[0][2], "error")
        # No dispatch either -- the backend has no record of this review.
        self.assertEqual(github.dispatch_calls, [])

    def test_lease_lost_during_run_posts_nothing(self) -> None:
        work = _work()
        api = FakeApi(heartbeat_exception=LeaseLost("lease_expired"))
        github = FakeGitHub(pulls=[_pr()])
        codex = FakeCodexRunner(_ok_result(), heartbeat_calls=1)
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        cancel = Event()
        handle_review(ctx, work, cancel)

        self.assertEqual(api.review_result_calls, [])
        self.assertEqual(github.set_status_calls, [])
        self.assertEqual(github.dispatch_calls, [])
        self.assertTrue(cancel.is_set())
        # cleanup still runs even when cancelled
        self.assertEqual(codex.cleanup_calls, [{"run_id": work.run_id}])

    def test_stale_head_after_run_posts_error_review(self) -> None:
        work = _work()
        api = FakeApi()
        # First get_pull (pre-run) is current; second (post-run) has moved.
        github = FakeGitHub(pulls=[_pr(), _pr(sha="c" * 40)])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        _run_id, _lease_id, payload = api.review_result_calls[0]
        self.assertEqual(payload["state"], "error")
        self.assertEqual(github.set_status_calls[0][2], "error")
        self.assertEqual(github.dispatch_calls, [])

    def test_missing_pr_number_stages_error_without_posting(self) -> None:
        work = _work(pr_number=None)
        api = FakeApi()
        github = FakeGitHub(pulls=[_pr()])
        codex = FakeCodexRunner(_ok_result())
        ctx = _ctx(api=api, github=github, codex=codex, dispatch_repo="Owner/task-manager")

        handle_review(ctx, work, Event())

        self.assertEqual(api.review_result_calls, [])
        self.assertEqual(len(api.stage_calls), 1)


if __name__ == "__main__":
    unittest.main()
