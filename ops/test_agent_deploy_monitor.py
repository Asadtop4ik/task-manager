from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agent_deploy_monitor
from agent_deploy_monitor import (
    ExternalDeployMonitor,
    _ci_record,
    _record,
    ci_targets,
)

RUN_ID = "00000000-0000-0000-0000-000000000017"
RUN_ID_2 = "00000000-0000-0000-0000-000000000018"
SHA = "a" * 40
NEW_SHA = "b" * 40


class FakeResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.raw[:size]


def pending(status="pr_ready", *, notified=True):
    return {
        "id": 1,
        "run_id": RUN_ID,
        "repo_full_name": "muradjanov-dev/ketoshop",
        "base_branch": "master",
        "pr_url": "https://github.com/muradjanov-dev/ketoshop/pull/7",
        "status": status,
        "merged_sha": SHA if status == "merged" else None,
        "notified": notified,
    }


def ci_pending(*, row_id=1, sha=SHA, conclusion="pending"):
    return {
        "id": row_id,
        "run_id": RUN_ID,
        "repo_full_name": "muradjanov-dev/qurbot",
        "base_branch": "master",
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/8",
        "status": "pr_opened",
        "head_sha": sha,
        "ci_status": conclusion,
        "ci_verified_sha": None,
        "ci_url": None,
    }


def qa_ci_pending(*, sha=SHA, conclusion="pending"):
    return {
        "id": 1,
        "run_id": RUN_ID,
        "repo_full_name": "Asadtop4ik/agent-qa",
        "base_branch": "main",
        "pr_url": "https://github.com/Asadtop4ik/agent-qa/pull/3",
        "status": "pr_opened",
        "head_sha": sha,
        "ci_status": conclusion,
        "ci_verified_sha": None,
        "ci_url": None,
    }


class ExternalDeployMonitorTests(unittest.TestCase):
    def test_ci_catalog_rejects_unknown_repository(self):
        with self.assertRaises(ValueError):
            _ci_record(ci_pending() | {"repo_full_name": "other/repo"})

    def test_disabled_qa_row_does_not_block_existing_ci_targets(self):
        posts = []

        def opener(request, timeout):
            url = request.full_url
            if "/ci-pending?after_id=0" in url:
                return FakeResponse(
                    [qa_ci_pending(), ci_pending(row_id=2, conclusion="pending")]
                )
            if url.endswith("/pulls/8"):
                return FakeResponse(
                    {
                        "head": {
                            "sha": SHA,
                            "ref": "codex/task-28-demo",
                            "repo": {"full_name": "muradjanov-dev/qurbot"},
                        }
                    }
                )
            if "/workflows/ci.yml/runs?" in url:
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 122,
                                "head_sha": SHA,
                                "head_branch": "codex/task-28-demo",
                                "event": "pull_request",
                                "path": ".github/workflows/ci.yml",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ]
                    }
                )
            if url.endswith("/ci-result"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "pr_opened"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.check_ci_once(), 1)
        self.assertEqual(
            posts,
            [
                {
                    "sha": SHA,
                    "conclusion": "failure",
                    "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/122",
                }
            ],
        )

    def test_qa_ci_catalog_is_enabled_only_by_flag_and_uses_its_workflow(self):
        self.assertNotIn("Asadtop4ik/agent-qa", ci_targets(qa_enabled=False))
        target = ci_targets(qa_enabled=True)["Asadtop4ik/agent-qa"]
        self.assertEqual(target.branch, "main")
        self.assertEqual(target.jobs, frozenset({"PR CI"}))
        self.assertEqual(target.workflow_path, ".github/workflows/agent-qa.yml")
        with self.assertRaisesRegex(ValueError, "outside approved repositories"):
            _ci_record(qa_ci_pending(), ci_targets(qa_enabled=False))
        self.assertEqual(
            _ci_record(qa_ci_pending(), ci_targets(qa_enabled=True))["head_sha"], SHA
        )

    def test_qa_ci_success_posts_exact_verified_head_to_task_manager(self):
        posts = []

        def opener(request, timeout):
            url = request.full_url
            if "api.github.com/repos/Asadtop4ik/agent-qa/" in url:
                self.assertEqual(
                    request.unredirected_hdrs["Authorization"], "Bearer qa-read-token"
                )
            if "/ci-pending?" in url:
                return FakeResponse([qa_ci_pending()])
            if url.endswith("/pulls/3"):
                return FakeResponse(
                    {
                        "head": {
                            "sha": SHA,
                            "ref": "codex/task-30-00000000-0000-0000-0000-000000000017",
                            "repo": {"full_name": "Asadtop4ik/agent-qa"},
                        }
                    }
                )
            if "/workflows/agent-qa.yml/runs?" in url:
                self.assertIn(f"head_sha={SHA}", url)
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 36182380264,
                                "head_sha": SHA,
                                "head_branch": (
                                    "codex/task-30-00000000-0000-0000-0000-000000000017"
                                ),
                                "event": "pull_request",
                                "path": ".github/workflows/agent-qa.yml",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                )
            if url.endswith("/actions/runs/36182380264/jobs?per_page=100"):
                return FakeResponse(
                    {"jobs": [{"name": "PR CI", "conclusion": "success"}]}
                )
            if url.endswith("/ci-result"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "pr_opened"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback",
            github_token="github",
            opener=opener,
            qa_enabled=True,
            qa_github_token="qa-read-token",
        )
        self.assertEqual(monitor.check_ci_once(), 1)
        self.assertEqual(
            posts,
            [
                {
                    "sha": SHA,
                    "conclusion": "success",
                    "github_run_url": (
                        "https://github.com/Asadtop4ik/agent-qa/actions/runs/"
                        "36182380264"
                    ),
                }
            ],
        )

    def test_qa_monitor_requires_repository_scoped_read_token(self):
        with self.assertRaisesRegex(ValueError, "repository-scoped"):
            ExternalDeployMonitor(
                callback_token="callback",
                github_token="generic-public-token",
                qa_enabled=True,
            )

    def test_ci_failure_is_reported_for_exact_pr_head(self):
        posts = []

        def opener(request, timeout):
            url = request.full_url
            if "/ci-pending?" in url:
                return FakeResponse([ci_pending()])
            if url.endswith("/pulls/8"):
                return FakeResponse(
                    {
                        "head": {
                            "sha": SHA,
                            "ref": "codex/task-28-demo",
                            "repo": {"full_name": "muradjanov-dev/qurbot"},
                        }
                    }
                )
            if "/workflows/ci.yml/runs?" in url:
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 123,
                                "head_sha": SHA,
                                "head_branch": "codex/task-28-demo",
                                "event": "pull_request",
                                "path": ".github/workflows/ci.yml",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ]
                    }
                )
            if url.endswith("/ci-result"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "pr_opened"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.check_ci_once(), 1)
        self.assertEqual(
            posts,
            [
                {
                    "sha": SHA,
                    "conclusion": "failure",
                    "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/123",
                }
            ],
        )

    def test_new_pr_head_waits_for_its_own_ci_not_old_green_run(self):
        posts = []

        def opener(request, timeout):
            url = request.full_url
            if "/ci-pending?" in url:
                return FakeResponse(
                    [
                        ci_pending(sha=SHA, conclusion="success")
                        | {
                            "ci_verified_sha": SHA,
                            "ci_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/122",
                            "status": "pr_ready",
                        }
                    ]
                )
            if url.endswith("/pulls/8"):
                return FakeResponse(
                    {
                        "head": {
                            "sha": NEW_SHA,
                            "ref": "codex/task-28-demo",
                            "repo": {"full_name": "muradjanov-dev/qurbot"},
                        }
                    }
                )
            if "/workflows/ci.yml/runs?" in url:
                self.assertIn(f"head_sha={NEW_SHA}", url)
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 122,
                                "head_sha": SHA,
                                "head_branch": "codex/task-28-demo",
                                "event": "pull_request",
                                "path": ".github/workflows/ci.yml",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                )
            if url.endswith("/ci-result"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "pr_opened"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.check_ci_once(), 1)
        self.assertEqual(
            posts, [{"sha": NEW_SHA, "conclusion": "pending", "github_run_url": None}]
        )

    def test_ci_success_requires_the_named_job(self):
        for job_conclusion, expected in (
            ("failure", "failure"),
            ("success", "success"),
        ):
            with self.subTest(job_conclusion=job_conclusion):
                posts = []

                def opener(request, timeout):
                    url = request.full_url
                    if "/ci-pending?" in url:
                        return FakeResponse([ci_pending()])
                    if url.endswith("/pulls/8"):
                        return FakeResponse(
                            {
                                "head": {
                                    "sha": SHA,
                                    "ref": "codex/task-28-demo",
                                    "repo": {"full_name": "muradjanov-dev/qurbot"},
                                }
                            }
                        )
                    if "/workflows/ci.yml/runs?" in url:
                        return FakeResponse(
                            {
                                "workflow_runs": [
                                    {
                                        "id": 123,
                                        "head_sha": SHA,
                                        "head_branch": "codex/task-28-demo",
                                        "event": "pull_request",
                                        "path": ".github/workflows/ci.yml",
                                        "status": "completed",
                                        "conclusion": "success",
                                    }
                                ]
                            }
                        )
                    if url.endswith("/actions/runs/123/jobs?per_page=100"):
                        return FakeResponse(
                            {"jobs": [{"name": "check", "conclusion": job_conclusion}]}
                        )
                    if url.endswith("/ci-result"):
                        posts.append(json.loads(request.data))
                        return FakeResponse({"status": "pr_ready"})
                    raise AssertionError(url)

                monitor = ExternalDeployMonitor(
                    callback_token="callback", github_token="github", opener=opener
                )
                self.assertEqual(monitor.check_ci_once(), 1)
                self.assertEqual(posts[0]["conclusion"], expected)

    def test_unknown_repo_or_branch_is_rejected(self):
        for changed in (
            {"repo_full_name": "other/repo"},
            {"base_branch": "main"},
            {"pr_url": "https://github.com/other/repo/pull/7"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                _record(pending() | changed)

    def test_unnotified_pr_waits_so_bot_can_send_its_link_first(self):
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            return FakeResponse([pending(notified=False)])

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.run_once(), (0, 0))
        self.assertEqual(len(requests), 1)

    def test_merged_pr_is_reported_without_claiming_deployment(self):
        requests = []

        def opener(request, timeout):
            requests.append((request.full_url, request.data))
            if "/external-pending?" in request.full_url:
                return FakeResponse([pending()])
            if request.full_url.endswith("/pulls/7"):
                return FakeResponse({"merged": True, "merge_commit_sha": SHA})
            if request.full_url.endswith("/merged"):
                self.assertEqual(json.loads(request.data), {"sha": SHA})
                return FakeResponse({"status": "merged"})
            raise AssertionError(request.full_url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.run_once(), (1, 0))
        self.assertFalse(any("/deployed" in url for url, _ in requests))

    def test_stale_first_page_does_not_hide_newer_merged_pr(self):
        pages = []

        def opener(request, timeout):
            url = request.full_url
            if "/external-pending?after_id=0" in url:
                pages.append(0)
                return FakeResponse(
                    [pending(notified=False) | {"id": index} for index in range(1, 51)]
                )
            if "/external-pending?after_id=50" in url:
                pages.append(50)
                return FakeResponse([pending() | {"id": 51}])
            if url.endswith("/pulls/7"):
                return FakeResponse({"merged": True, "merge_commit_sha": SHA})
            if url.endswith("/merged"):
                return FakeResponse({"status": "merged"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.run_once(), (1, 0))
        self.assertEqual(pages, [0, 50])

    def test_exact_image_and_successful_workflow_are_both_required(self):
        for image_sha, expected in (("b" * 40, (0, 0)), (SHA, (0, 1))):
            with self.subTest(image_sha=image_sha):
                requests = []

                def opener(request, timeout):
                    requests.append((request.full_url, request.data))
                    if "/external-pending?" in request.full_url:
                        return FakeResponse([pending("merged")])
                    if "/workflows/deploy.yml/runs?" in request.full_url:
                        return FakeResponse(
                            {
                                "workflow_runs": [
                                    {
                                        "id": 123,
                                        "head_sha": SHA,
                                        "head_branch": "master",
                                        "event": "push",
                                        "status": "completed",
                                        "conclusion": "success",
                                    }
                                ]
                            }
                        )
                    if request.full_url.endswith("/actions/runs/123/jobs?per_page=100"):
                        return FakeResponse(
                            {
                                "jobs": [
                                    {"name": "ci / check", "conclusion": "success"},
                                    {"name": "deploy", "conclusion": "success"},
                                ]
                            }
                        )
                    if request.full_url.endswith("/deployed"):
                        self.assertEqual(json.loads(request.data)["sha"], SHA)
                        return FakeResponse({"status": "deployed"})
                    raise AssertionError(request.full_url)

                def docker(args, **kwargs):
                    self.assertEqual(args[-1], "ketoshop")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "Config": {
                                    "Image": f"ghcr.io/muradjanov-dev/ketoshop:{image_sha}"
                                },
                                "State": {
                                    "Running": True,
                                    "Health": {"Status": "healthy"},
                                },
                            }
                        ),
                    )

                monitor = ExternalDeployMonitor(
                    callback_token="callback",
                    github_token="github",
                    opener=opener,
                    command_runner=docker,
                )
                self.assertEqual(monitor.run_once(), expected)
                self.assertEqual(
                    any(url.endswith("/deployed") for url, _ in requests),
                    expected == (0, 1),
                )

    def test_all_containers_must_match_the_same_merge_sha(self):
        images = {
            "qurbot-web": f"ghcr.io/muradjanov-dev/qurbot:{SHA}",
            "qurbot-worker": f"ghcr.io/muradjanov-dev/qurbot:{'b' * 40}",
        }

        def docker(args, **kwargs):
            container = args[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Config": {"Image": images[container]},
                        "State": {"Running": True, "Health": {"Status": "healthy"}},
                    }
                ),
            )

        monitor = ExternalDeployMonitor(
            callback_token="callback",
            github_token="github",
            opener=lambda *args, **kwargs: FakeResponse({}),
            command_runner=docker,
        )
        from agent_deploy_monitor import TARGETS

        self.assertFalse(
            monitor._production_matches(TARGETS["muradjanov-dev/qurbot"], SHA)
        )

    def test_green_deploy_without_required_ci_job_does_not_count(self):
        def opener(request, timeout):
            if "/workflows/deploy.yml/runs?" in request.full_url:
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 123,
                                "head_sha": SHA,
                                "head_branch": "master",
                                "event": "push",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                )
            if request.full_url.endswith("/actions/runs/123/jobs?per_page=100"):
                return FakeResponse(
                    {"jobs": [{"name": "deploy", "conclusion": "success"}]}
                )
            raise AssertionError(request.full_url)

        from agent_deploy_monitor import TARGETS

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertIsNone(
            monitor._successful_deploy_run(
                "muradjanov-dev/ketoshop", TARGETS["muradjanov-dev/ketoshop"], SHA
            )
        )

    def test_check_ci_once_isolates_one_bad_record_from_the_next(self):
        posts = []

        def opener(request, timeout):
            url = request.full_url
            if "/ci-pending?" in url:
                return FakeResponse(
                    [
                        ci_pending(row_id=1),
                        ci_pending(row_id=2)
                        | {
                            "repo_full_name": "muradjanov-dev/ketoshop",
                            "base_branch": "master",
                            "pr_url": "https://github.com/muradjanov-dev/ketoshop/pull/9",
                        },
                    ]
                )
            if url.endswith("/pulls/8"):
                raise RuntimeError("github rate limited this PR lookup")
            if url.endswith("/pulls/9"):
                return FakeResponse(
                    {
                        "head": {
                            "sha": SHA,
                            "ref": "master",
                            "repo": {"full_name": "muradjanov-dev/ketoshop"},
                        }
                    }
                )
            if "/workflows/ci.yml/runs?" in url:
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 555,
                                "head_sha": SHA,
                                "head_branch": "master",
                                "event": "pull_request",
                                "path": ".github/workflows/ci.yml",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                )
            if url.endswith("/actions/runs/555/jobs?per_page=100"):
                return FakeResponse({"jobs": [{"name": "check", "conclusion": "success"}]})
            if url.endswith("/ci-result"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "pr_opened"})
            raise AssertionError(url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.check_ci_once(), 1)
        self.assertEqual(monitor.record_errors, 1)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["sha"], SHA)

    def test_run_once_isolates_one_bad_record_from_the_next(self):
        posts = []
        deploy_calls = 0

        def opener(request, timeout):
            nonlocal deploy_calls
            url = request.full_url
            if "/external-pending?" in url:
                return FakeResponse(
                    [
                        pending("merged") | {"id": 1, "run_id": RUN_ID},
                        pending("merged") | {"id": 2, "run_id": RUN_ID_2},
                    ]
                )
            if "/workflows/deploy.yml/runs?" in url:
                deploy_calls += 1
                if deploy_calls == 1:
                    raise RuntimeError("github is unavailable for this lookup")
                return FakeResponse(
                    {
                        "workflow_runs": [
                            {
                                "id": 123,
                                "head_sha": SHA,
                                "head_branch": "master",
                                "event": "push",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                )
            if url.endswith("/actions/runs/123/jobs?per_page=100"):
                return FakeResponse(
                    {
                        "jobs": [
                            {"name": "ci / check", "conclusion": "success"},
                            {"name": "deploy", "conclusion": "success"},
                        ]
                    }
                )
            if url.endswith(f"/{RUN_ID_2}/deployed"):
                posts.append(json.loads(request.data))
                return FakeResponse({"status": "deployed"})
            raise AssertionError(url)

        def docker(args, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Config": {"Image": f"ghcr.io/muradjanov-dev/ketoshop:{SHA}"},
                        "State": {"Running": True, "Health": {"Status": "healthy"}},
                    }
                ),
            )

        monitor = ExternalDeployMonitor(
            callback_token="callback",
            github_token="github",
            opener=opener,
            command_runner=docker,
        )
        self.assertEqual(monitor.run_once(), (0, 1))
        self.assertEqual(monitor.record_errors, 1)
        self.assertEqual(len(posts), 1)


class MainTests(unittest.TestCase):
    def test_main_prints_record_errors_and_exits_zero_when_the_pass_succeeds(self):
        def fake_check_ci_once(self):
            self.record_errors = 2
            return 3

        def fake_run_once(self):
            return (1, 0)

        with (
            patch.dict(
                os.environ,
                {"AGENT_CALLBACK_TOKEN": "callback", "GITHUB_AGENT_TOKEN": "github"},
            ),
            patch.object(ExternalDeployMonitor, "check_ci_once", fake_check_ci_once),
            patch.object(ExternalDeployMonitor, "run_once", fake_run_once),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                agent_deploy_monitor.main()
        self.assertIn("3 updated", output.getvalue())
        self.assertIn("1 merged", output.getvalue())
        self.assertIn("2 record errors", output.getvalue())

    def test_main_exits_nonzero_and_redacts_when_the_pass_itself_fails(self):
        token = "ghp_" + "b" * 30

        def failing_check_ci_once(self):
            raise ValueError(f"invalid pending CI list near token {token}")

        with (
            patch.dict(
                os.environ,
                {"AGENT_CALLBACK_TOKEN": "callback", "GITHUB_AGENT_TOKEN": "github"},
            ),
            patch.object(ExternalDeployMonitor, "check_ci_once", failing_check_ci_once),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                agent_deploy_monitor.main()
        self.assertEqual(ctx.exception.code, 1)
        printed = stderr.getvalue()
        self.assertIn("ValueError", printed)
        self.assertIn("invalid pending CI list", printed)
        self.assertNotIn(token, printed)


if __name__ == "__main__":
    unittest.main()
