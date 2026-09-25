import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pr_review import (
    VISIBLE_REPORT_LIMIT,
    _pull_request_files,
    _review_report,
    finalize,
)


def finalize_result(result, run_id="", file_pages=None):
    sha = "a" * 40
    pr = {
        "state": "open",
        "merged": False,
        "head": {"sha": sha, "repo": {"full_name": "Asadtop4ik/task-manager"}},
        "html_url": "https://github.com/Asadtop4ik/task-manager/pull/99",
    }
    trusted_files = {
        "scripts/check.py",
        "config/key.pem",
        "app/auth.py",
        "backend/auth.py",
        "critical.py",
    }
    if file_pages is None:
        file_pages = [[{"filename": path} for path in trusted_files]]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "agent-review-target.json").write_text(
            json.dumps(
                {
                    "repo": "Asadtop4ik/task-manager",
                    "pull_number": 99,
                    "head_sha": sha,
                    "run_id": run_id,
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
            "agent_pr_review._github",
            side_effect=[
                pr,
                *file_pages,
                pr,
            ],
        ), patch("agent_pr_review._task_api") as task_api, patch(
            "agent_pr_review._set_review_status"
        ) as set_status, patch("urllib.request.urlopen"), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout:
            if run_id:
                task_api.side_effect = [
                    {
                        "head_sha": sha,
                        "status": "pr_opened",
                        "pr_url": pr["html_url"],
                    },
                    None,
                ]
            finalize()
            report = summary_path.read_text(encoding="utf-8")
            log = stdout.getvalue()
        return report, log, set_status.call_args, task_api.call_args_list


class AgentPrReviewTests(unittest.TestCase):
    def test_finalize_publishes_only_review_metadata_for_infrastructure_pr(self):
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
        self.assertNotIn("<script>", report)
        self.assertNotIn("Unsafe", report)
        self.assertNotIn("do not run", report)
        self.assertNotIn("One issue", report)
        self.assertNotIn("ghp_", report)
        self.assertIn("Codex review report", log)
        self.assertEqual(
            status_call.args,
            ("Asadtop4ik/task-manager", sha, "failure", "1 blocking finding(s)"),
        )
        self.assertEqual(task_api_calls, [])

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

        self.assertNotIn("PRIVATE_KEY_MATERIAL_7f3a", report)
        self.assertNotIn("PRIVATE_KEY_MATERIAL_7f3a", log)
        self.assertNotIn("Private key included", report)

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
            "file": "app/auth.py",
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
        self.assertIn("app/auth.py:42", report)
        self.assertNotIn("[sensitive content omitted]", report)

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
        self.assertIn("backend/auth.py:51", report)
        self.assertNotIn("Header contains", report)
        self.assertNotIn("Observed", report)

    def test_finalize_omits_standalone_base64_like_blob(self):
        blob = "dXNlcjpwYXNz" * 4
        report, log, _, _ = finalize_result({"summary": blob, "findings": []})

        self.assertNotIn(blob, report)
        self.assertNotIn(blob, log)

    def test_model_text_and_untrusted_location_cannot_leak_through_report(self):
        finding = {
            "severity": "P1",
            "title": "Hardcoded password",
            "evidence": "The value is hunter2",
            "file": "hunter2.py",
            "line": 42,
        }
        report, log, _, _ = finalize_result(
            {
                "summary": "The password is hunter2",
                "findings": [finding],
            }
        )

        self.assertNotIn("hunter2", report)
        self.assertNotIn("hunter2", log)
        self.assertIn("Finding 1 [P1]: [location omitted]", report)

    def test_path_with_credential_assignment_is_omitted(self):
        report, _, _, _ = finalize_result(
            {
                "summary": "No details in report",
                "findings": [
                    {
                        "severity": "P2",
                        "title": "Header issue",
                        "evidence": "Sensitive value omitted",
                        "file": "config/password=hunter2",
                        "line": 17,
                    }
                ],
            }
        )

        self.assertIn("Finding 1 [P2]: [location omitted]", report)

    def test_finalize_trusts_changed_path_from_second_file_page(self):
        page_one = [
            {"filename": f"src/first-{index}.py"} for index in range(100)
        ]
        page_two = [{"filename": "backend/only-page-two.py"}]
        report, _, _, _ = finalize_result(
            {
                "summary": "Location is on page two",
                "findings": [
                    {
                        "severity": "P2",
                        "title": "Finding title is withheld",
                        "evidence": "Evidence is withheld",
                        "file": "backend/only-page-two.py",
                        "line": 23,
                    }
                ],
            },
            file_pages=[page_one, page_two],
        )

        self.assertIn("backend/only-page-two.py:23", report)

    def test_pull_request_file_pagination_stops_at_github_limit(self):
        pages = [
            [
                {"filename": f"page-{page}/file-{index}.py"}
                for index in range(100)
            ]
            for page in range(1, 31)
        ]
        with patch("agent_pr_review._github", side_effect=pages) as github:
            files = _pull_request_files("Asadtop4ik/task-manager", 45, "token")

        self.assertEqual(len(files), 3_000)
        self.assertEqual(github.call_count, 30)
        self.assertTrue(github.call_args_list[0].args[0].endswith("page=1"))
        self.assertTrue(github.call_args_list[-1].args[0].endswith("page=30"))

    def test_agent_run_callback_keeps_full_structured_review_data(self):
        run_id = "11111111-1111-4111-8111-111111111111"
        summary = "Hardcoded password hunter2"
        finding = {
            "severity": "P2",
            "title": "Hardcoded password",
            "evidence": "The value is hunter2",
            "file": "backend/auth.py",
            "line": 51,
        }
        report, log, _, calls = finalize_result(
            {"summary": summary, "findings": [finding]}, run_id=run_id
        )

        self.assertNotIn("hunter2", report)
        self.assertNotIn("hunter2", log)
        self.assertIn("backend/auth.py:51", report)
        self.assertEqual(calls[0].args, ("GET", f"{run_id}/status"))
        self.assertEqual(calls[1].args, ("POST", f"{run_id}/review-result"))
        self.assertEqual(
            calls[1].kwargs["body"],
            {
                "sha": "a" * 40,
                "state": "findings",
                "summary": summary,
                "findings": [finding],
            },
        )

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
            findings,
            "findings",
            False,
            {"advisory.py", "blocker.py"},
        )

        self.assertLess(report.index("Finding 1 [P1]"), report.index("Finding 2 [P2]"))
        self.assertLess(report.index("Finding 2 [P2]"), report.index("Finding 3 [P3]"))
        self.assertIn("Additional findings omitted: 2 (P3: 2)", report)
        self.assertIn("Finding counts: P1=1, P2=1, P3=12", report)
        self.assertNotIn("blocker P1", report)
        self.assertNotIn("evidence", report)

    def test_report_is_bounded_with_maximum_visible_findings(self):
        findings = []
        for index in range(40):
            findings.append(
                {
                    "severity": "P1" if index == 39 else "P3",
                    "title": f"ignored {index}",
                    "evidence": "ignored",
                    "file": "docs/readme.md",
                    "line": index + 1,
                }
            )
        report = _review_report(
            "Asadtop4ik/task-manager",
            45,
            "b" * 40,
            findings,
            "findings",
            False,
            {"docs/readme.md"},
        )

        self.assertLess(len(report), VISIBLE_REPORT_LIMIT + 100)
        self.assertIn("Finding 1 [P1]", report)
        self.assertIn("Additional findings omitted: 28 (P3: 28)", report)
        self.assertNotIn("ignored", report)


if __name__ == "__main__":
    unittest.main()
