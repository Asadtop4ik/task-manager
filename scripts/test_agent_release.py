import os
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


class AgentReviewTests(unittest.TestCase):
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
