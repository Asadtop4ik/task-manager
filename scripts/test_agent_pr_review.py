import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pr_review import (
    _review_report,
    _safe_feedback_text,
    finalize,
)


def finalize_result(result):
    sha = "a" * 40
    pr = {
        "state": "open",
        "merged": False,
        "head": {"sha": sha, "repo": {"full_name": "Asadtop4ik/task-manager"}},
        "html_url": "https://github.com/Asadtop4ik/task-manager/pull/99",
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "agent-review-target.json").write_text(
            json.dumps(
                {
                    "repo": "Asadtop4ik/task-manager",
                    "pull_number": 99,
                    "head_sha": sha,
                    "run_id": "",
                }
            ),
            encoding="utf-8",
        )
        (root / "agent-review-result.json").write_text(
            json.dumps(result), encoding="utf-8"
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
        return report, log, set_status.call_args, task_api.call_count


class AgentPrReviewTests(unittest.TestCase):
    def test_finalize_publishes_safe_finding_details_for_infrastructure_pr(self):
        sha = "a" * 40
        finding = {
            "severity": "P2",
            "title": "Unsafe <script>alert(1)</script> evidence",
            "evidence": "token=ghp_" + "x" * 30 + " ::error:: do not run",
            "file": "scripts/check.py",
            "line": 17,
        }
        report, log, status_call, task_api_calls = finalize_result(
            {"summary": "One issue", "findings": [finding]}
        )

        self.assertIn(f"Reviewed SHA: {sha}", report)
        self.assertIn("Finding 1 [P2]", report)
        self.assertIn("scripts/check.py:17", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertIn("[sensitive content omitted]", report)
        self.assertNotIn("ghp_", report)
        self.assertIn("Codex review report", log)
        self.assertEqual(
            status_call.args,
            ("Asadtop4ik/task-manager", sha, "failure", "1 blocking finding(s)"),
        )
        self.assertEqual(task_api_calls, 0)

    def test_finalize_redacts_full_pem_block_before_evidence_limit(self):
        secret_body = "PRIVATE_KEY_MATERIAL_7f3a\n" * 40
        evidence = (
            "Observed key:\n-----BEGIN PRIVATE KEY-----\n"
            + secret_body
            + "-----END PRIVATE KEY-----"
        )
        self.assertGreater(evidence.index("-----END PRIVATE KEY-----"), 600)
        report, log, _, _ = finalize_result(
            {
                "summary": "Private key included",
                "findings": [
                    {
                        "severity": "P1",
                        "title": "A private key is included",
                        "evidence": evidence,
                        "file": "config/key.pem",
                        "line": 2,
                    }
                ],
            }
        )

        self.assertIn("[sensitive content omitted]", report)
        self.assertNotIn("PRIVATE_KEY_MATERIAL_7f3a", report)
        self.assertNotIn("PRIVATE_KEY_MATERIAL_7f3a", log)
        unterminated = _safe_feedback_text(
            "-----BEGIN PRIVATE KEY-----\n" + secret_body, 600
        )
        self.assertIn("[sensitive content omitted]", unterminated)
        self.assertNotIn("PRIVATE_KEY_MATERIAL_7f3a", unterminated)

    def test_finalize_omits_sensitive_fields_with_quoted_or_json_credentials(self):
        summary = (
            "password='correct horse battery staple' "
            'api_key="multi word api key" '
            "secret='unfinished credential value "
            '{"password": "json credential value"}\n'
            "-----BEGIN PRIVATE KEY-----\nUNTERMINATED_SUMMARY_KEY"
        )
        finding = {
            "severity": "P1",
            "title": (
                'JSON key "password": "title credential value"\n'
                "second line"
            ),
            "evidence": (
                '{"password": "evidence credential value"}\n'
                "second line evidence"
            ),
            "file": "app/secrets.py",
            "line": 42,
        }
        report, log, _, _ = finalize_result(
            {"summary": summary, "findings": [finding]}
        )

        for secret_tail in (
            "correct horse battery staple",
            "multi word api key",
            "unfinished credential value",
            "json credential value",
            "title credential value",
            "evidence credential value",
            "UNTERMINATED_SUMMARY_KEY",
        ):
            self.assertNotIn(secret_tail, report)
            self.assertNotIn(secret_tail, log)
        self.assertIn("[sensitive content omitted]", report)
        self.assertIn("app/secrets.py:42", report)
        self.assertIn("Finding 1 [P1]: [sensitive content omitted]", report)
        self.assertEqual(
            _safe_feedback_text("token=abc trailing text", 100),
            "[sensitive content omitted]",
        )

    def test_finalize_omits_basic_authorization_from_all_free_text_fields(self):
        credential = "Authorization: Basic dXNlcjpwYXNz"
        finding = {
            "severity": "P2",
            "title": f"Header contains {credential}",
            "evidence": f"Observed {credential}",
            "file": "backend/auth.py",
            "line": 51,
        }
        report, log, _, _ = finalize_result(
            {"summary": f"Review found {credential}", "findings": [finding]}
        )

        self.assertNotIn("dXNlcjpwYXNz", report)
        self.assertNotIn("dXNlcjpwYXNz", log)
        self.assertGreaterEqual(report.count("[sensitive content omitted]"), 3)
        self.assertIn("backend/auth.py:51", report)

    def test_finalize_omits_standalone_base64_like_blob(self):
        blob = "dXNlcjpwYXNz" * 4
        report, log, _, _ = finalize_result({"summary": blob, "findings": []})

        self.assertNotIn(blob, report)
        self.assertNotIn(blob, log)
        self.assertIn("[sensitive content omitted]", report)

    def test_report_prioritizes_blockers_and_counts_omitted_severities(self):
        findings = [
            {"severity": "P3", "title": f"advisory {index}", "evidence": "note"}
            for index in range(12)
        ]
        findings.extend(
            [
                {"severity": "P2", "title": "blocker P2", "evidence": "evidence"},
                {"severity": "P1", "title": "blocker P1", "evidence": "evidence"},
            ]
        )
        report = _review_report(
            "Asadtop4ik/task-manager",
            45,
            "b" * 40,
            "Two blocking issues",
            findings,
            "findings",
            False,
        )

        self.assertLess(report.index("Finding 1 [P1]"), report.index("Finding 2 [P2]"))
        self.assertLess(report.index("Finding 2 [P2]"), report.index("Finding 3 [P3]"))
        self.assertIn("Additional findings omitted: 2 (P3: 2)", report)

    def test_report_limit_keeps_omitted_severity_counts_visible(self):
        findings = [
            {
                "severity": "P3",
                "title": "A_" * 90,
                "evidence": "B_" * 300,
                "file": "C_" * 120,
                "line": 1,
            }
            for _ in range(12)
        ]
        findings.append(
            {
                "severity": "P1",
                "title": "blocking finding",
                "evidence": "D" * 600,
                "file": "critical.py",
                "line": 2,
            }
        )
        report = _review_report(
            "Asadtop4ik/task-manager",
            45,
            "b" * 40,
            "Summary",
            findings,
            "findings",
            False,
        )

        self.assertGreater(len(report), 11_000)
        self.assertIn("Finding 1 [P1]", report)
        self.assertIn("Additional findings omitted: 1 (P3: 1)", report)


if __name__ == "__main__":
    unittest.main()
