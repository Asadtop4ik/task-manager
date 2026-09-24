import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_task import _changed_fragments, _task, callback, check_diff, fast_needs_pr, started, usage


class UsageTests(unittest.TestCase):
    def test_renamed_security_file_forces_fast_pr(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Move auth code",
            "description": "",
            "base_branch": "main",
            "mode": "fast",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", *args], cwd=root, check=True, capture_output=True
                )

            git("init")
            git("config", "user.email", "ci@example.test")
            git("config", "user.name", "CI Test")
            (root / "backend/app/core").mkdir(parents=True)
            (root / "backend/app/core/security.py").write_text("OLD = True\n")
            git("add", ".")
            git("commit", "-m", "base")
            (root / "backend/app").mkdir(parents=True, exist_ok=True)
            git("mv", "backend/app/core/security.py", "backend/app/ordinary.py")
            environment = {
                "TASK_JSON": json.dumps(task),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(root / "github-env"),
            }
            with patch.dict(os.environ, environment):
                check_diff(cwd=temp)
            self.assertIn("FAST_FALLBACK=true", (root / "github-env").read_text())

    def test_fast_policy_allows_app_code_but_escalates_sensitive_paths(self) -> None:
        self.assertFalse(fast_needs_pr(["frontend/src/pages/Board.tsx"]))
        for path in (
            "backend/app/api/v1/auth.py",
            "backend/app/api/v1/projects.py",
            "backend/app/schemas/project.py",
            "backend/alembic/versions/0010.py",
            ".github/workflows/deploy.yml",
            "bot/app/texts.py",
            "bot/app/parsing.py",
            "bot/app/handlers/new.py",
            "bot/app/worker.py",
            "frontend/src/components/Checkout.tsx",
            "frontend/src/lib/format.ts",
            "README.md",
            "docs/pilot.md",
            "frontend/package-lock.json",
            "billing/invoices.py",
        ):
            self.assertTrue(fast_needs_pr([path]), path)

    def test_fast_policy_detects_sensitive_logic_on_neutral_path(self) -> None:
        path = ["frontend/src/pages/Board.tsx"]
        permission_change = """diff --git a/Board.tsx b/Board.tsx
@@ -1 +1 @@
-const visible = user.is_owner && ready;
+const visible = user.is_owner || ready;
"""
        self.assertTrue(fast_needs_pr(path, _changed_fragments(permission_change)))
        presentation_change = """diff --git a/Board.tsx b/Board.tsx
@@ -1 +1 @@
-const link = user.is_owner && <Link>Trash</Link>;
+const link = user.is_owner && <Link title="Show hidden tasks">Trash</Link>;
"""
        self.assertFalse(fast_needs_pr(path, _changed_fragments(presentation_change)))
        self.assertTrue(fast_needs_pr(path, ["send_message(chat_id, customer_text)"]))

    def test_untracked_sensitive_code_falls_back_to_a_pr(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Pilot",
            "description": "",
            "base_branch": "main",
            "mode": "fast",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for args in (
                ("init",),
                ("config", "user.email", "ci@example.test"),
                ("config", "user.name", "CI Test"),
            ):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            (root / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
            source = root / "frontend/src/pages/Neutral.tsx"
            source.parent.mkdir(parents=True)
            source.write_text("send_message(chat_id, customer_text)\n")
            environment = {
                "TASK_JSON": json.dumps(task),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(root / "github-env"),
            }
            with patch.dict(os.environ, environment):
                check_diff(cwd=temp)
            self.assertIn("FAST_FALLBACK=true", (root / "github-env").read_text())

    def test_fast_mode_is_validated_in_dispatch_payload(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
            "mode": "fast",
        }
        with patch.dict(os.environ, {"TASK_JSON": json.dumps(task)}):
            self.assertEqual(_task()["mode"], "fast")

    def test_cancelled_run_stops_before_codex(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "GITHUB_OUTPUT": str(output),
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "cancelled"}
            ):
                started()
            self.assertEqual(output.read_text(), "cancelled=true\n")

    def test_failed_run_carries_agent_question(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "agent-result.txt").write_text(
                "Which menu label should I use?"
            )
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "JOB_STATUS": "failure",
                "FAILURE_PHASE": "implement",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            self.assertEqual(
                sent.call_args.args[0]["error"], "Which menu label should I use?"
            )

    def test_publisher_failure_does_not_report_successful_agent_summary(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "agent-result.txt").write_text("README change complete")
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "JOB_STATUS": "failure",
                "FAILURE_PHASE": "publish",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            self.assertEqual(
                sent.call_args.args[0]["error"],
                "Publisher failed before PR/deploy; inspect the GitHub run.",
            )

    def test_extracts_last_completed_turn_and_ignores_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events = Path(temp) / "agent-events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 6194,
                                    "cached_input_tokens": 4000,
                                    "output_tokens": 280,
                                },
                            }
                        ),
                        '{"type": "turn.completed",',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"RUNNER_TEMP": temp}):
                self.assertEqual(
                    usage(),
                    {
                        "input_tokens": 6194,
                        "cached_input_tokens": 4000,
                        "output_tokens": 280,
                    },
                )


if __name__ == "__main__":
    unittest.main()
