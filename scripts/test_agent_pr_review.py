import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pr_review import finalize


class AgentPrReviewTests(unittest.TestCase):
    def test_finalize_publishes_safe_finding_details_for_infrastructure_pr(self):
        sha = "a" * 40
        pr = {
            "state": "open",
            "merged": False,
            "head": {"sha": sha, "repo": {"full_name": "Asadtop4ik/task-manager"}},
            "html_url": "https://github.com/Asadtop4ik/task-manager/pull/44",
        }
        finding = {
            "severity": "P2",
            "title": "Unsafe <script>alert(1)</script> evidence",
            "evidence": "token=ghp_" + "x" * 30 + " ::error:: do not run",
            "file": "scripts/check.py",
            "line": 17,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent-review-target.json").write_text(
                json.dumps(
                    {
                        "repo": "Asadtop4ik/task-manager",
                        "pull_number": 44,
                        "head_sha": sha,
                        "run_id": "",
                    }
                ),
                encoding="utf-8",
            )
            (root / "agent-review-result.json").write_text(
                json.dumps({"summary": "One issue", "findings": [finding]}),
                encoding="utf-8",
            )
            summary_path = root / "step-summary.md"
            environment = {
                "RUNNER_TEMP": directory,
                "GITHUB_STEP_SUMMARY": str(summary_path),
                "GH_TOKEN": "test-token",
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "123",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "agent_pr_review._github", return_value=pr
            ), patch("agent_pr_review._task_api") as task_api, patch(
                "agent_pr_review._set_review_status"
            ) as set_status, patch("urllib.request.urlopen"), contextlib.redirect_stdout(
                io.StringIO()
            ) as stdout:
                finalize()
                report = summary_path.read_text(encoding="utf-8")
                log = stdout.getvalue()

        self.assertIn(f"Reviewed SHA: {sha}", report)
        self.assertIn("Finding 1 [P2]", report)
        self.assertIn("scripts/check.py:17", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertIn("[redacted]", report)
        self.assertNotIn("ghp_", report)
        self.assertIn("Codex review report", log)
        set_status.assert_called_once_with(
            "Asadtop4ik/task-manager", sha, "failure", "1 blocking finding(s)"
        )
        task_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
