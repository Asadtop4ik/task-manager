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


if __name__ == "__main__":
    unittest.main()
