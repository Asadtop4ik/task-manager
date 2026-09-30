from __future__ import annotations

import io
import json
import subprocess
import unittest
from types import SimpleNamespace
from typing import Any

from agent_svc.github import UnknownRepository
from agent_svc.log import Logger, Redactor
from agent_svc.repos import Catalog, RepoInfo
from agent_svc.watch import (
    FAST_WINDOW_S,
    WatchChecks,
    _RecordScheduler,
    build_watch_checks,
)

DISPATCH_REPO = "Owner/task-manager"
PUBLIC_REPO = "muradjanov-dev/demo"
QA_REPO = "Owner/agent-qa"
RUN_ID = "22222222-2222-2222-2222-222222222222"


def _logger() -> Logger:
    return Logger(Redactor([]), stream=io.StringIO())


def _catalog() -> Catalog:
    return Catalog(
        repos=(
            RepoInfo(
                project_key="task-manager",
                full_name=DISPATCH_REPO,
                branch="main",
                private=True,
                ci_jobs=(),
                pr_ci_jobs=("gate",),
                pr_ci_workflow=".github/workflows/ci.yml",
                images=(),
                qa_only=False,
            ),
            RepoInfo(
                project_key="demo",
                full_name=PUBLIC_REPO,
                branch="main",
                private=False,
                ci_jobs=("ci / check",),
                pr_ci_jobs=("check",),
                pr_ci_workflow=".github/workflows/ci.yml",
                images=(("demo-web", "ghcr.io/muradjanov-dev/demo-web"),),
                qa_only=False,
            ),
        )
    )


def _catalog_with_qa() -> Catalog:
    base = _catalog()
    return Catalog(
        repos=(
            *base.repos,
            RepoInfo(
                project_key="agent-qa",
                full_name=QA_REPO,
                branch="main",
                private=True,
                ci_jobs=(),
                pr_ci_jobs=("PR CI",),
                pr_ci_workflow=".github/workflows/agent-qa.yml",
                images=(),
                qa_only=True,
            ),
        )
    )


class FakeApi:
    def __init__(self) -> None:
        self.ci_pending_pages: list[list[dict[str, Any]]] = [[]]
        self.external_pending_pages: list[list[dict[str, Any]]] = [[]]
        self.ci_result_calls: list[tuple[str, str, str, str | None]] = []
        self.merged_calls: list[tuple[str, str]] = []
        self.deployed_calls: list[tuple[str, str, str]] = []

    def ci_pending(self, after_id: int = 0) -> list[dict[str, Any]]:
        return self.ci_pending_pages[0] if self.ci_pending_pages else []

    def external_pending(self, after_id: int = 0) -> list[dict[str, Any]]:
        return self.external_pending_pages[0] if self.external_pending_pages else []

    def ci_result(
        self, run_id: str, *, sha: str, conclusion: str, github_run_url: str | None = None
    ) -> None:
        self.ci_result_calls.append((run_id, sha, conclusion, github_run_url))

    def merged(self, run_id: str, *, sha: str) -> None:
        self.merged_calls.append((run_id, sha))

    def deployed(self, run_id: str, *, sha: str, github_run_url: str) -> None:
        self.deployed_calls.append((run_id, sha, github_run_url))


class FakeGitHub:
    def __init__(self) -> None:
        self.get_pull_results: dict[tuple[str, int], Any] = {}
        self.workflow_runs: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        self.jobs: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.calls: list[tuple[Any, ...]] = []

    def get_pull(self, repo: str, number: int) -> dict[str, Any]:
        self.calls.append(("get_pull", repo, number))
        value = self.get_pull_results[(repo, number)]
        if isinstance(value, Exception):
            raise value
        return value

    def list_workflow_runs(
        self, repo: str, workflow_file: str, *, event: str, head_sha: str
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_workflow_runs", repo, workflow_file, event, head_sha))
        return self.workflow_runs.get((repo, workflow_file, event, head_sha), [])

    def run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        self.calls.append(("run_jobs", repo, run_id))
        return self.jobs.get((repo, run_id), [])


def _ci_pending_row(
    *,
    row_id: int = 1,
    repo: str = PUBLIC_REPO,
    run_id: str = RUN_ID,
    pr_number: int = 5,
    head_sha: str = "a" * 40,
    status: str = "pr_opened",
    ci_status: str | None = None,
    ci_verified_sha: str | None = None,
    ci_url: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "run_id": run_id,
        "repo_full_name": repo,
        "base_branch": "main",
        "pr_url": f"https://github.com/{repo}/pull/{pr_number}",
        "head_sha": head_sha,
        "status": status,
        "ci_status": ci_status,
        "ci_verified_sha": ci_verified_sha,
        "ci_url": ci_url,
    }


def _external_pending_row(
    *,
    row_id: int = 1,
    repo: str = PUBLIC_REPO,
    run_id: str = RUN_ID,
    pr_number: int = 5,
    status: str = "pr_ready",
    merged_sha: str | None = None,
    notified: bool = True,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "run_id": run_id,
        "repo_full_name": repo,
        "base_branch": "main",
        "pr_url": f"https://github.com/{repo}/pull/{pr_number}",
        "status": status,
        "merged_sha": merged_sha,
        "notified": notified,
    }


def _completed_run(
    *,
    run_id: int,
    sha: str,
    branch: str,
    path: str = ".github/workflows/ci.yml",
    event: str = "pull_request",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "path": path,
        "status": "completed",
        "conclusion": "success",
    }


def _ctx(
    *,
    api: FakeApi,
    github: FakeGitHub,
    catalog: Catalog | None = None,
    logger: Logger | None = None,
) -> Any:
    return SimpleNamespace(
        api=api,
        github=github,
        logger=logger or _logger(),
        catalog=catalog or _catalog(),
        settings=SimpleNamespace(libexec_dir="/opt/agent-svc/libexec"),
    )


def _image_state_runner(responses: dict[str, Any], *, returncode: int = 0) -> Any:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, returncode, stdout=json.dumps(responses), stderr=""
        )

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


class RecordSchedulerTests(unittest.TestCase):
    def test_fast_interval_then_slow_after_window(self) -> None:
        now = [0.0]
        scheduler = _RecordScheduler(now=lambda: now[0])

        self.assertTrue(scheduler.due("r1"))  # first sight is always due
        now[0] = 5.0
        self.assertFalse(scheduler.due("r1"))  # 15s fast interval not elapsed
        now[0] = 15.0
        self.assertTrue(scheduler.due("r1"))

        # Well past the 15-minute fast window: interval becomes 60s.
        now[0] = FAST_WINDOW_S + 100.0
        self.assertTrue(scheduler.due("r1"))
        now[0] = FAST_WINDOW_S + 130.0  # only 30s later: still within the 60s slow interval
        self.assertFalse(scheduler.due("r1"))
        now[0] = FAST_WINDOW_S + 161.0
        self.assertTrue(scheduler.due("r1"))

    def test_independent_records(self) -> None:
        scheduler = _RecordScheduler(now=lambda: 0.0)
        self.assertTrue(scheduler.due("a"))
        self.assertTrue(scheduler.due("b"))  # unaffected by "a" being consumed


class CheckCiTests(unittest.TestCase):
    def test_success_posts_once(self) -> None:
        api = FakeApi()
        api.ci_pending_pages = [[_ci_pending_row()]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_ci()

        self.assertEqual(len(api.ci_result_calls), 1)
        run_id, sha, conclusion, url = api.ci_result_calls[0]
        self.assertEqual((run_id, sha, conclusion), (RUN_ID, "a" * 40, "success"))
        self.assertIn("actions/runs/100", url or "")

    def test_successful_post_logs_an_info_event_without_secrets(self) -> None:
        stream = io.StringIO()
        logger = Logger(Redactor(["tok-secret-value"]), stream=stream)
        api = FakeApi()
        api.ci_pending_pages = [[_ci_pending_row()]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]

        WatchChecks(_ctx(api=api, github=github, logger=logger)).check_ci()

        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        events = [line for line in lines if line["event"] == "watch_ci_result_reported"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["level"], "info")
        self.assertEqual(events[0]["run_id"], RUN_ID)
        self.assertEqual(events[0]["conclusion"], "success")
        self.assertEqual(events[0]["sha"], "a" * 40)
        self.assertNotIn("tok-secret-value", stream.getvalue())

    def test_no_change_logs_nothing(self) -> None:
        stream = io.StringIO()
        logger = Logger(Redactor([]), stream=stream)
        api = FakeApi()
        sha = "a" * 40
        url = f"https://github.com/{PUBLIC_REPO}/actions/runs/100"
        api.ci_pending_pages = [
            [
                _ci_pending_row(
                    head_sha=sha, ci_status="success", ci_verified_sha=sha, ci_url=url
                )
            ]
        ]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": sha, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", sha)] = [
            _completed_run(run_id=100, sha=sha, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]

        WatchChecks(_ctx(api=api, github=github, logger=logger)).check_ci()

        self.assertEqual(api.ci_result_calls, [])
        self.assertNotIn("watch_ci_result_reported", stream.getvalue())

    def test_no_change_skips_post(self) -> None:
        api = FakeApi()
        sha = "a" * 40
        url = f"https://github.com/{PUBLIC_REPO}/actions/runs/100"
        api.ci_pending_pages = [
            [
                _ci_pending_row(
                    head_sha=sha, ci_status="success", ci_verified_sha=sha, ci_url=url
                )
            ]
        ]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": sha, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", sha)] = [
            _completed_run(run_id=100, sha=sha, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_ci()

        self.assertEqual(api.ci_result_calls, [])

    def test_missing_required_job_posts_failure(self) -> None:
        api = FakeApi()
        api.ci_pending_pages = [[_ci_pending_row()]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        run = _completed_run(run_id=100, sha="a" * 40, branch="feature")
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [run]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "other-job", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_ci()

        self.assertEqual(len(api.ci_result_calls), 1)
        self.assertEqual(api.ci_result_calls[0][2], "failure")

    def test_one_bad_record_does_not_block_others(self) -> None:
        api = FakeApi()
        good = _ci_pending_row(row_id=2, pr_number=5)
        bad = _ci_pending_row(
            row_id=1, repo="unknown/repo", run_id="33333333-3333-3333-3333-333333333333"
        )
        api.ci_pending_pages = [[bad, good]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_ci()  # must not raise despite the "unknown/repo" record

        self.assertEqual(len(api.ci_result_calls), 1)
        self.assertEqual(api.ci_result_calls[0][0], RUN_ID)

    def test_one_bad_record_raising_during_processing_does_not_block_others(self) -> None:
        api = FakeApi()
        broken = _ci_pending_row(
            row_id=1, pr_number=9, run_id="55555555-5555-5555-5555-555555555555"
        )
        good = _ci_pending_row(row_id=2, pr_number=5)
        api.ci_pending_pages = [[broken, good]]
        github = FakeGitHub()
        # No get_pull_results entry for (PUBLIC_REPO, 9): raises KeyError.
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_ci()  # must not raise despite the broken record's KeyError

        self.assertEqual(len(api.ci_result_calls), 1)

    def test_adaptive_interval_skips_a_too_soon_recheck(self) -> None:
        api = FakeApi()
        api.ci_pending_pages = [[_ci_pending_row()]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        now = [0.0]
        checks = WatchChecks(_ctx(api=api, github=github), now=lambda: now[0])

        checks.check_ci()
        get_pull_calls_after_first = len([c for c in github.calls if c[0] == "get_pull"])
        now[0] = 5.0  # well within the 15s fast interval
        checks.check_ci()
        get_pull_calls_after_second = len([c for c in github.calls if c[0] == "get_pull"])

        self.assertEqual(get_pull_calls_after_first, 1)
        self.assertEqual(get_pull_calls_after_second, 1)  # not re-checked yet


class CheckMergeDeployTests(unittest.TestCase):
    def test_merged_detection(self) -> None:
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="pr_ready")]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "merged": True,
            "merge_commit_sha": "b" * 40,
        }
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_merge_deploy()

        self.assertEqual(api.merged_calls, [(RUN_ID, "b" * 40)])

    def test_merged_and_deployed_reports_are_logged(self) -> None:
        stream = io.StringIO()
        logger = Logger(Redactor([]), stream=stream)
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="pr_ready")]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "merged": True,
            "merge_commit_sha": "b" * 40,
        }
        WatchChecks(_ctx(api=api, github=github, logger=logger)).check_merge_deploy()
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        merged = [line for line in lines if line["event"] == "watch_merged_reported"]
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["level"], merged[0]["sha"]), ("info", "b" * 40))

        sha = "c" * 40
        stream2 = io.StringIO()
        api2 = FakeApi()
        api2.external_pending_pages = [[_external_pending_row(status="merged", merged_sha=sha)]]
        github2 = FakeGitHub()
        github2.workflow_runs[(PUBLIC_REPO, "deploy.yml", "push", sha)] = [
            {
                "id": 200,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        github2.jobs[(PUBLIC_REPO, 200)] = [
            {"name": "ci / check", "conclusion": "success"},
            {"name": "deploy", "conclusion": "success"},
        ]
        healthy = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "healthy",
                }
            }
        )
        WatchChecks(
            _ctx(api=api2, github=github2, logger=Logger(Redactor([]), stream=stream2)),
            command_runner=healthy,
        ).check_merge_deploy()
        lines = [json.loads(line) for line in stream2.getvalue().splitlines() if line]
        deployed = [line for line in lines if line["event"] == "watch_deployed_reported"]
        self.assertEqual(len(deployed), 1)
        self.assertEqual(deployed[0]["run_id"], RUN_ID)

    def test_not_yet_merged_posts_nothing(self) -> None:
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="pr_ready")]]
        github = FakeGitHub()
        github.get_pull_results[(PUBLIC_REPO, 5)] = {"merged": False, "merge_commit_sha": None}
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_merge_deploy()

        self.assertEqual(api.merged_calls, [])

    def test_deploy_requires_all_containers_healthy(self) -> None:
        sha = "c" * 40
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="merged", merged_sha=sha)]]
        github = FakeGitHub()
        github.workflow_runs[(PUBLIC_REPO, "deploy.yml", "push", sha)] = [
            {
                "id": 200,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        github.jobs[(PUBLIC_REPO, 200)] = [
            {"name": "ci / check", "conclusion": "success"},
            {"name": "deploy", "conclusion": "success"},
        ]
        unhealthy = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "unhealthy",
                }
            }
        )
        checks = WatchChecks(_ctx(api=api, github=github), command_runner=unhealthy)

        checks.check_merge_deploy()
        self.assertEqual(api.deployed_calls, [])

        healthy = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "healthy",
                }
            }
        )
        checks2 = WatchChecks(_ctx(api=api, github=github), command_runner=healthy)
        checks2.check_merge_deploy()
        self.assertEqual(
            api.deployed_calls,
            [(RUN_ID, sha, f"https://github.com/{PUBLIC_REPO}/actions/runs/200")],
        )

    def test_image_state_argv_is_exact(self) -> None:
        sha = "d" * 40
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="merged", merged_sha=sha)]]
        github = FakeGitHub()
        github.workflow_runs[(PUBLIC_REPO, "deploy.yml", "push", sha)] = [
            {
                "id": 201,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        github.jobs[(PUBLIC_REPO, 201)] = [
            {"name": "ci / check", "conclusion": "success"},
            {"name": "deploy", "conclusion": "success"},
        ]
        runner = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "healthy",
                }
            }
        )
        checks = WatchChecks(_ctx(api=api, github=github), command_runner=runner)

        checks.check_merge_deploy()

        self.assertEqual(
            runner.calls,  # type: ignore[attr-defined]
            [
                [
                    "/usr/bin/sudo",
                    "-n",
                    "/usr/bin/python3",
                    "-I",
                    "/opt/agent-svc/libexec/image_state.py",
                    "demo-web",
                ]
            ],
        )

    def test_custom_image_state_command_prefix(self) -> None:
        sha = "e" * 40
        api = FakeApi()
        api.external_pending_pages = [[_external_pending_row(status="merged", merged_sha=sha)]]
        github = FakeGitHub()
        github.workflow_runs[(PUBLIC_REPO, "deploy.yml", "push", sha)] = [
            {
                "id": 202,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        github.jobs[(PUBLIC_REPO, 202)] = [
            {"name": "ci / check", "conclusion": "success"},
            {"name": "deploy", "conclusion": "success"},
        ]
        runner = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "healthy",
                }
            }
        )
        checks = WatchChecks(
            _ctx(api=api, github=github),
            image_state_command=["/usr/bin/python3", "/fixtures/image_state.py"],
            command_runner=runner,
        )

        checks.check_merge_deploy()

        self.assertEqual(
            runner.calls, [["/usr/bin/python3", "/fixtures/image_state.py", "demo-web"]]  # type: ignore[attr-defined]
        )

    def test_one_bad_record_does_not_block_deploy_check(self) -> None:
        sha = "f" * 40
        api = FakeApi()
        bad = _external_pending_row(
            row_id=1, repo="unknown/repo", run_id="44444444-4444-4444-4444-444444444444"
        )
        good = _external_pending_row(row_id=2, status="merged", merged_sha=sha)
        api.external_pending_pages = [[bad, good]]
        github = FakeGitHub()
        github.workflow_runs[(PUBLIC_REPO, "deploy.yml", "push", sha)] = [
            {
                "id": 203,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        github.jobs[(PUBLIC_REPO, 203)] = [
            {"name": "ci / check", "conclusion": "success"},
            {"name": "deploy", "conclusion": "success"},
        ]
        runner = _image_state_runner(
            {
                "demo-web": {
                    "image": f"ghcr.io/muradjanov-dev/demo-web:{sha}",
                    "running": True,
                    "health": "healthy",
                }
            }
        )
        checks = WatchChecks(_ctx(api=api, github=github), command_runner=runner)

        checks.check_merge_deploy()

        self.assertEqual(len(api.deployed_calls), 1)

    def test_unnotified_record_is_skipped(self) -> None:
        api = FakeApi()
        api.external_pending_pages = [
            [_external_pending_row(status="pr_ready", notified=False)]
        ]
        github = FakeGitHub()
        checks = WatchChecks(_ctx(api=api, github=github))

        checks.check_merge_deploy()  # must not even try get_pull

        self.assertEqual([c for c in github.calls if c[0] == "get_pull"], [])


class QaRepoWithoutTokenTests(unittest.TestCase):
    def test_qa_repo_is_skipped_quietly_and_logged_once(self) -> None:
        stream = io.StringIO()
        logger = Logger(Redactor([]), stream=stream)
        api = FakeApi()
        api.ci_pending_pages = [
            [
                _ci_pending_row(
                    repo=QA_REPO,
                    pr_number=1,
                    run_id="66666666-6666-6666-6666-666666666666",
                )
            ]
        ]
        github = FakeGitHub()
        github.get_pull_results[(QA_REPO, 1)] = UnknownRepository(QA_REPO)
        now = [0.0]
        checks = WatchChecks(
            _ctx(api=api, github=github, catalog=_catalog_with_qa(), logger=logger),
            now=lambda: now[0],
        )

        checks.check_ci()
        now[0] = 20.0  # past the 15s fast interval: due again
        checks.check_ci()
        now[0] = 40.0
        checks.check_ci()

        self.assertEqual(api.ci_result_calls, [])
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        qa_events = [line for line in lines if line.get("event") == "watch_qa_repo_disabled"]
        self.assertEqual(len(qa_events), 1)  # logged once, not on every tick
        self.assertEqual(qa_events[0]["level"], "info")
        error_events = [line for line in lines if line.get("level") == "error"]
        self.assertEqual(error_events, [])  # never an error

    def test_qa_repo_does_not_block_other_records(self) -> None:
        api = FakeApi()
        api.ci_pending_pages = [
            [
                _ci_pending_row(
                    row_id=1,
                    repo=QA_REPO,
                    pr_number=1,
                    run_id="77777777-7777-7777-7777-777777777777",
                ),
                _ci_pending_row(row_id=2, pr_number=5),
            ]
        ]
        github = FakeGitHub()
        github.get_pull_results[(QA_REPO, 1)] = UnknownRepository(QA_REPO)
        github.get_pull_results[(PUBLIC_REPO, 5)] = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": PUBLIC_REPO}},
        }
        github.workflow_runs[(PUBLIC_REPO, "ci.yml", "pull_request", "a" * 40)] = [
            _completed_run(run_id=100, sha="a" * 40, branch="feature")
        ]
        github.jobs[(PUBLIC_REPO, 100)] = [{"name": "check", "conclusion": "success"}]
        checks = WatchChecks(_ctx(api=api, github=github, catalog=_catalog_with_qa()))

        checks.check_ci()  # must not raise despite the QA record's UnknownRepository

        self.assertEqual(len(api.ci_result_calls), 1)
        self.assertEqual(api.ci_result_calls[0][0], RUN_ID)


class BuildWatchChecksTests(unittest.TestCase):
    def test_returns_two_callables(self) -> None:
        api = FakeApi()
        github = FakeGitHub()
        checks = build_watch_checks(_ctx(api=api, github=github))
        self.assertEqual(len(checks), 2)
        for check in checks:
            check()  # must not raise on an empty backlog


if __name__ == "__main__":
    unittest.main()
