import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_pr_review
import agent_release


class AgentReleaseTests(unittest.TestCase):
    def test_qa_repo_is_absent_until_both_qa_flags_are_explicit(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Asadtop4ik/agent-qa", agent_release.approved_branches())
            self.assertNotIn(
                "Asadtop4ik/agent-qa", agent_pr_review.approved_repositories()
            )
        with patch.dict(
            os.environ,
            {
                "AGENT_QA_ENABLED": "true",
                "AGENT_QA_REPOSITORY": "Asadtop4ik/agent-qa",
            },
            clear=True,
        ):
            self.assertEqual(
                agent_release.approved_branches()["Asadtop4ik/agent-qa"], "main"
            )
            self.assertEqual(
                agent_pr_review.approved_repositories()["Asadtop4ik/agent-qa"],
                "main",
            )

    def test_release_rechecks_exact_branch_head_and_base(self) -> None:
        run = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "base_branch": "master",
        }
        pr = {
            "state": "open",
            "head": {
                "sha": "a" * 40,
                "ref": "codex/task-7-00000000-0000-0000-0000-000000000007",
                "repo": {"full_name": "muradjanov-dev/qurbot"},
            },
            "base": {"ref": "master"},
        }
        agent_release._verify_pr(
            pr, "muradjanov-dev/qurbot", run, "a" * 40, require_open=True
        )
        with self.assertRaises(ValueError):
            agent_release._verify_pr(
                pr, "muradjanov-dev/qurbot", run, "b" * 40, require_open=True
            )
        with self.assertRaises(ValueError):
            agent_release._verify_pr(
                {**pr, "base": {"ref": "main"}},
                "muradjanov-dev/qurbot",
                run,
                "a" * 40,
                require_open=True,
            )

    def test_qa_merge_uses_exact_workflow_run_and_pr_ci_job_without_check_runs(self) -> None:
        run_id = "0fb0df87-f3d5-4f36-b45e-223d69670a45"
        branch = f"codex/task-30-{run_id}"
        repo = "Asadtop4ik/agent-qa"
        sha = "a" * 40
        run = {
            "run_id": run_id,
            "task_id": 30,
            "repo_full_name": repo,
            "base_branch": "main",
            "status": "pr_ready",
            "ci_status": "success",
            "ci_verified_sha": sha,
            "review_status": "clean",
            "review_sha": sha,
            "pr_url": f"https://github.com/{repo}/pull/3",
        }
        action = {"status": "in_progress", "request": {"expected_head_sha": sha}}
        pr = {
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "head": {
                "sha": sha,
                "ref": branch,
                "repo": {"full_name": repo},
            },
            "base": {"ref": "main"},
        }
        workflow_run = {
            "id": 36182380264,
            "event": "pull_request",
            "path": ".github/workflows/agent-qa.yml",
            "head_sha": sha,
            "head_branch": branch,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [
                {"number": 3, "head": {"sha": sha, "ref": branch}}
            ],
        }
        calls = []

        with tempfile.TemporaryDirectory() as temp:
            def request(url, **kwargs):
                calls.append(url)
                if "/actions/workflows/agent-qa.yml/runs?" in url:
                    return {"workflow_runs": [workflow_run]}
                if url.endswith("/actions/runs/36182380264/jobs?per_page=100"):
                    return {
                        "jobs": [
                            {"id": 10, "name": "PR CI", "conclusion": "success"}
                        ]
                    }
                if url.endswith(f"/commits/{sha}/statuses"):
                    return [
                        {
                            "id": 11,
                            "context": "codex-review",
                            "state": "success",
                            "url": f"https://api.github.com/repos/{repo}/statuses/{sha}",
                        }
                    ]
                raise AssertionError(url)

            with (
                patch.dict(
                    os.environ,
                    {
                        "AGENT_QA_ENABLED": "true",
                        "AGENT_QA_REPOSITORY": repo,
                        "GH_TOKEN": "qa-token",
                        "GITHUB_OUTPUT": str(Path(temp, "output")),
                    },
                ),
                patch.object(
                    agent_release,
                    "_context",
                    return_value=({}, run, action, run_id, 3, sha),
                ),
                patch.object(agent_release, "_pr", return_value=pr),
                patch.object(agent_release, "_request", side_effect=request),
            ):
                agent_release.verify_merge()

            output = Path(temp, "output").read_text(encoding="utf-8")
        self.assertIn("pull_number=3", output)
        self.assertIn(f"head_sha={sha}", output)
        self.assertTrue(any(url.endswith(f"/commits/{sha}/statuses") for url in calls))
        self.assertFalse(any("/check-runs" in url for url in calls))

    def test_qa_merge_rejects_wrong_pr_or_failed_pr_ci_job(self) -> None:
        run_id = "0fb0df87-f3d5-4f36-b45e-223d69670a45"
        branch = f"codex/task-30-{run_id}"
        repo = "Asadtop4ik/agent-qa"
        sha = "a" * 40
        run = {
            "run_id": run_id,
            "task_id": 30,
            "repo_full_name": repo,
            "base_branch": "main",
            "status": "pr_ready",
            "ci_status": "success",
            "ci_verified_sha": sha,
            "review_status": "clean",
            "review_sha": sha,
            "pr_url": f"https://github.com/{repo}/pull/3",
        }
        action = {"status": "in_progress", "request": {"expected_head_sha": sha}}
        pr = {
            "state": "open",
            "merged": False,
            "draft": False,
            "mergeable_state": "clean",
            "head": {"sha": sha, "ref": branch, "repo": {"full_name": repo}},
            "base": {"ref": "main"},
        }

        def context(**kwargs):
            return ({}, run, action, run_id, 3, sha)

        with tempfile.TemporaryDirectory() as temp:
            cases = (
                (4, "success", ".github/workflows/agent-qa.yml"),
                (3, "failure", ".github/workflows/agent-qa.yml"),
                (3, "success", ".github/workflows/ci.yml"),
            )
            for pull_number, job_conclusion, workflow_path in cases:
                workflow_run = {
                    "id": 20,
                    "event": "pull_request",
                    "path": workflow_path,
                    "head_sha": sha,
                    "head_branch": branch,
                    "status": "completed",
                    "conclusion": "success",
                    "pull_requests": [
                        {"number": pull_number, "head": {"sha": sha, "ref": branch}}
                    ],
                }

                def request(url, **kwargs):
                    if "/actions/workflows/agent-qa.yml/runs?" in url:
                        return {"workflow_runs": [workflow_run]}
                    if url.endswith("/actions/runs/20/jobs?per_page=100"):
                        return {
                            "jobs": [
                                {
                                    "id": 10,
                                    "name": "PR CI",
                                    "conclusion": job_conclusion,
                                }
                            ]
                        }
                    raise AssertionError(url)

                with (
                    patch.dict(
                        os.environ,
                        {
                            "AGENT_QA_ENABLED": "true",
                            "AGENT_QA_REPOSITORY": repo,
                            "GH_TOKEN": "qa-token",
                            "GITHUB_OUTPUT": str(Path(temp, "output")),
                        },
                    ),
                    patch.object(agent_release, "_context", side_effect=context),
                    patch.object(agent_release, "_pr", return_value=pr),
                    patch.object(agent_release, "_request", side_effect=request),
                    self.assertRaises(ValueError),
                ):
                    agent_release.verify_merge()

    def test_merge_is_recorded_before_qa_deploy_dispatch(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        action = {
            "action_id": "00000000-0000-0000-0000-000000000008",
            "status": "in_progress",
        }
        run = {
            "repo_full_name": "Asadtop4ik/agent-qa",
            "base_branch": "main",
            "status": "pr_ready",
        }
        pr = {
            "merged": True,
            "head": {"sha": "a" * 40},
            "merge_commit_sha": "b" * 40,
        }
        calls: list[str] = []
        with (
            patch.dict(os.environ, {"AGENT_QA_ENABLED": "true", "GH_TOKEN": "test"}),
            patch.object(
                agent_release,
                "_merge_report_context",
                return_value=({}, run, action, run_id, 9, "a" * 40),
            ),
            patch.object(agent_release, "_pr", return_value=pr),
            patch.object(
                agent_release,
                "_task_callback",
                side_effect=lambda *args, **kwargs: calls.append("merge-recorded"),
            ),
            patch.object(
                agent_release.subprocess,
                "run",
                side_effect=lambda *args, **kwargs: calls.append("deploy-dispatch"),
            ),
            patch.object(
                agent_release,
                "_qa_dispatch_result",
                side_effect=lambda *args, **kwargs: calls.append("dispatch-recorded"),
            ),
        ):
            agent_release.report_merge()
        self.assertEqual(
            calls,
            ["merge-recorded", "deploy-dispatch", "dispatch-recorded"],
        )

    def test_qa_dispatch_failure_is_persisted_after_merge(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        action = {
            "action_id": "00000000-0000-0000-0000-000000000008",
            "status": "in_progress",
        }
        run = {
            "repo_full_name": "Asadtop4ik/agent-qa",
            "base_branch": "main",
            "status": "pr_ready",
        }
        pr = {
            "merged": True,
            "head": {"sha": "a" * 40},
            "merge_commit_sha": "b" * 40,
        }
        calls: list[tuple[str, str | None]] = []
        with (
            patch.dict(os.environ, {"AGENT_QA_ENABLED": "true", "GH_TOKEN": "test"}),
            patch.object(
                agent_release,
                "_merge_report_context",
                return_value=({}, run, action, run_id, 9, "a" * 40),
            ),
            patch.object(agent_release, "_pr", return_value=pr),
            patch.object(
                agent_release,
                "_task_callback",
                side_effect=lambda *args, **kwargs: calls.append(
                    ("merge-recorded", None)
                ),
            ),
            patch.object(
                agent_release.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "gh workflow run"),
            ),
            patch.object(
                agent_release,
                "_qa_dispatch_result",
                side_effect=lambda *args, **kwargs: calls.append(
                    ("dispatch-failed", args[3])
                ),
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            agent_release.report_merge()
        self.assertEqual(
            calls, [("merge-recorded", None), ("dispatch-failed", "failed")]
        )

    def test_failed_qa_dispatch_can_be_retried_after_merge_is_recorded(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        action = {
            "action_id": "00000000-0000-0000-0000-000000000008",
            "status": "completed",
            "result": {"head_sha": "a" * 40, "merge_sha": "b" * 40},
        }
        run = {
            "repo_full_name": "Asadtop4ik/agent-qa",
            "base_branch": "main",
            "status": "merged",
            "merged_sha": "b" * 40,
            "qa_deploy_dispatch_status": "failed",
        }
        pr = {
            "merged": True,
            "head": {"sha": "a" * 40},
            "merge_commit_sha": "b" * 40,
        }
        calls: list[str] = []
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_QA_ENABLED": "true",
                    "GH_TOKEN": "test",
                    "AGENT_QA_DEPLOY_WORKFLOW": ".github/workflows/agent-qa.yml",
                },
            ),
            patch.object(
                agent_release,
                "_merge_report_context",
                return_value=({}, run, action, run_id, 9, "a" * 40),
            ),
            patch.object(agent_release, "_pr", return_value=pr),
            patch.object(agent_release, "_task_callback") as task_callback,
            patch.object(
                agent_release.subprocess,
                "run",
                side_effect=lambda *args, **kwargs: calls.append("dispatch"),
            ),
            patch.object(
                agent_release,
                "_qa_dispatch_result",
                side_effect=lambda *args, **kwargs: calls.append(args[3]),
            ),
        ):
            agent_release.report_merge()
        task_callback.assert_not_called()
        self.assertEqual(calls, ["dispatch", "dispatched"])

    def test_same_head_correction_review_dispatch_includes_full_target_context(
        self,
    ) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        action_id = "00000000-0000-0000-0000-000000000008"
        expected = "a" * 40
        repo = "muradjanov-dev/qurbot"
        branch = f"codex/task-7-{run_id}"
        payload = {
            "run_id": run_id,
            "action_id": action_id,
            "repo_full_name": repo,
            "branch": branch,
            "expected_head_sha": expected,
            "instruction": "Reconsider this finding on the same head.",
        }
        run = {
            "run_id": run_id,
            "task_id": 7,
            "repo_full_name": repo,
            "base_branch": "master",
            "pr_url": f"https://github.com/{repo}/pull/9",
            "status": "correction_running",
        }
        action = {"action_id": action_id, "status": "in_progress"}
        pr = {
            "state": "open",
            "head": {
                "sha": expected,
                "ref": branch,
                "repo": {"full_name": repo},
            },
            "base": {"ref": "master"},
        }
        dispatches: list[dict] = []
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": temp,
                    "GH_TOKEN": "target-token",
                    "DISPATCH_TOKEN": "control-token",
                    "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                },
            ),
        ):
            Path(temp, "agent-correction-target.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "action_id": action_id,
                        "branch": branch,
                        "expected_head_sha": expected,
                        "instruction": payload["instruction"],
                    }
                ),
                encoding="utf-8",
            )
            Path(temp, "agent-correction.patch").write_text("", encoding="utf-8")

            def request(url: str, **kwargs):
                if url.endswith(f"git/ref/heads/{branch}"):
                    return {"object": {"sha": expected}}
                dispatches.append(kwargs["body"]["client_payload"])
                return None

            with (
                patch.object(
                    agent_release,
                    "_context",
                    return_value=(payload, run, action, run_id, 9, expected),
                ),
                patch.object(agent_release, "_pr", return_value=pr),
                patch.object(agent_release, "_request", side_effect=request),
                patch.object(agent_release, "_task_callback"),
            ):
                agent_release.publish_correction()

        self.assertEqual(
            dispatches,
            [
                {
                    "repo_full_name": repo,
                    "run_id": run_id,
                    "pull_number": 9,
                    "head_sha": expected,
                    "branch": branch,
                    "base_branch": "master",
                }
            ],
        )

    def test_failed_merge_verification_rejects_action_without_marking_merge(
        self,
    ) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        action = {
            "action_id": "00000000-0000-0000-0000-000000000008",
            "status": "in_progress",
        }
        run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "status": "pr_opened",
        }
        pr = {"merged": False, "head": {"sha": "c" * 40}}
        calls: list[dict] = []
        with (
            patch.dict(os.environ, {"GH_TOKEN": "test"}),
            patch.object(
                agent_release,
                "_merge_report_context",
                return_value=({}, run, action, run_id, 9, "a" * 40),
            ),
            patch.object(agent_release, "_pr", return_value=pr),
            patch.object(
                agent_release,
                "_task_callback",
                side_effect=lambda _run, _action, body: calls.append(body),
            ),
            self.assertRaises(ValueError),
        ):
            agent_release.report_merge()
        self.assertEqual(calls[0]["status"], "rejected")


class AgentReviewTests(unittest.TestCase):
    def test_p3_findings_are_advisory_and_p1_p2_block(self) -> None:
        p3 = {"severity": "P3"}
        p2 = {"severity": "P2"}
        self.assertEqual(
            agent_pr_review._review_decision([p3]),
            ("advisory", True, "0 blocking, 1 advisory finding(s)"),
        )
        state, ready, _ = agent_pr_review._review_decision([p3, p2])
        self.assertEqual(state, "findings")
        self.assertFalse(ready)

    def test_review_output_requires_structured_severity_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(
                '{"summary":"One issue","findings":[{"severity":"P2",'
                '"title":"Missing guard","evidence":"Null case crashes."}]}',
                encoding="utf-8",
            )
            summary, findings = agent_pr_review._parse_result(path)
            self.assertEqual(summary, "One issue")
            self.assertEqual(findings[0]["severity"], "P2")
            path.write_text(
                '{"summary":"Malformed","findings":[{"severity":"blocker",'
                '"title":"Bad","evidence":"Evidence"}]}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                agent_pr_review._parse_result(path)

    def test_review_requires_current_open_target_pr(self) -> None:
        repo = "Asadtop4ik/task-manager"
        pr = {
            "state": "open",
            "head": {"sha": "a" * 40, "repo": {"full_name": repo}},
        }
        self.assertTrue(agent_pr_review._current_pr(pr, repo, "a" * 40))
        self.assertFalse(agent_pr_review._current_pr(pr, repo, "b" * 40))
        self.assertFalse(
            agent_pr_review._current_pr({**pr, "state": "closed"}, repo, "a" * 40)
        )


class CorrectionPromptTests(unittest.TestCase):
    def test_matches_the_prompt_verify_correction_would_write(self) -> None:
        run = {
            "task_id": 12,
            "run_id": "00000000-0000-0000-0000-000000000012",
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/9",
        }
        expected_head_sha = "a" * 40
        instruction = "Rename the button to Submit."
        prompt = agent_release.correction_prompt(run, expected_head_sha, instruction)
        self.assertIn("Task #12: https://github.com/Asadtop4ik/task-manager/pull/9", prompt)
        self.assertIn(f"Current PR head: {expected_head_sha}", prompt)
        self.assertIn("Owner correction:\nRename the button to Submit.", prompt)
        self.assertIn("Do not push, open a PR, merge, deploy", prompt)

    def test_verify_correction_writes_exactly_what_correction_prompt_builds(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000012"
        action_id = "00000000-0000-0000-0000-000000000034"
        expected_sha = "a" * 40
        repo = "Asadtop4ik/task-manager"
        task_id = 9
        branch = f"codex/task-{task_id}-{run_id}"
        instruction = "Rename the button to Submit."
        run = {
            "run_id": run_id,
            "repo_full_name": repo,
            "base_branch": "main",
            "head_sha": expected_sha,
            "status": "pr_opened",
            "pr_url": f"https://github.com/{repo}/pull/9",
            "task_id": task_id,
        }
        action = {
            "action_id": action_id,
            "status": "accepted",
            "kind": "correction",
            "request": {"expected_head_sha": expected_sha, "instruction": instruction},
        }
        pr = {
            "state": "open",
            "merged": False,
            "head": {"sha": expected_sha, "ref": branch, "repo": {"full_name": repo}},
            "base": {"ref": "main"},
        }

        def fake_request(url, *, method="GET", token=None, body=None, callback=False):
            if url.endswith(f"/{run_id}/status"):
                return run
            if url.endswith(f"/{run_id}/actions/{action_id}"):
                return action
            if url.endswith(f"/repos/{repo}/pulls/9"):
                return pr
            raise AssertionError(f"unexpected request: {url}")

        event = {
            "client_payload": {
                "run_id": run_id,
                "action_id": action_id,
                "repo_full_name": repo,
                "expected_head_sha": expected_sha,
                "instruction": instruction,
                "branch": branch,
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            event_path = Path(temp) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            environment = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GH_TOKEN": "token",
                "AGENT_CALLBACK_TOKEN": "callback-token",
                "RUNNER_TEMP": temp,
                "GITHUB_OUTPUT": str(Path(temp) / "github-output"),
            }
            with patch.dict(os.environ, environment, clear=True), patch(
                "agent_release._request", side_effect=fake_request
            ):
                agent_release.verify_correction()
            written = (Path(temp) / "agent-correction-prompt.txt").read_text(
                encoding="utf-8"
            )
        self.assertEqual(
            written, agent_release.correction_prompt(run, expected_sha, instruction)
        )


if __name__ == "__main__":
    unittest.main()
