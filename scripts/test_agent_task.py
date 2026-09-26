import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_task import (
    _changed_fragments,
    _safe_fast_patch,
    _task,
    branch_name,
    build_prompt,
    callback,
    check_diff,
    failure_reason,
    fast_needs_pr,
    prepare,
    started,
    usage,
)


class UsageTests(unittest.TestCase):
    def test_successful_publisher_reports_pr_opened_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            task = {
                "task_id": 28,
                "run_id": "00000000-0000-0000-0000-000000000028",
                "title": "Page products",
                "description": "",
                "base_branch": "master",
                "repo_full_name": "muradjanov-dev/qurbot",
                "mode": "pr",
            }
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "99",
                "RUNNER_TEMP": temp,
                "JOB_STATUS": "success",
                "PR_URL": "https://github.com/muradjanov-dev/qurbot/pull/8",
                "HEAD_SHA": "a" * 40,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "pr_opened"}
            ) as sent:
                callback()
            self.assertEqual(sent.call_args.args[0]["status"], "pr_opened")

    def test_private_validator_records_no_change_failure_for_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("agent_task.py")), "check-diff"],
                cwd=repo,
                env=os.environ | {"RUNNER_TEMP": temp},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("agent produced no file changes", (Path(temp) / "agent-failure.txt").read_text())

    def test_reference_image_cannot_be_committed_even_if_renamed(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Screenshot task",
            "description": "",
            "base_branch": "main",
            "mode": "pr",
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            image_dir = root / "agent-images"
            image_dir.mkdir()
            image = b"\x89PNG\r\n\x1a\nreference"
            (image_dir / "source.png").write_bytes(image)
            copied = repo / "frontend/public/renamed.png"
            copied.parent.mkdir(parents=True)
            copied.write_bytes(image)
            environment = {
                "TASK_JSON": json.dumps(task),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(root / "github-env"),
            }
            with patch.dict(os.environ, environment), self.assertRaisesRegex(
                ValueError, "reference images"
            ):
                check_diff(cwd=str(repo))

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
            "frontend/src/pages/login.tsx",
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
            (root / "github-env").write_text("")
            subprocess.run(
                ["git", "add", str(source.relative_to(root))],
                cwd=root, check=True, capture_output=True,
            )
            with patch.dict(os.environ, environment):
                check_diff(cwd=temp)
            self.assertIn("FAST_FALLBACK=true", (root / "github-env").read_text())

    def test_positive_fast_gate_rejects_logic_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

            git("init")
            git("config", "user.email", "ci@example.test")
            git("config", "user.name", "CI Test")
            board = root / "frontend/src/pages/Board.tsx"
            board.parent.mkdir(parents=True)
            board.write_text('const link = user.is_owner && <Link>Trash</Link>;\n')
            git("add", ".")
            git("commit", "-m", "base")

            board.write_text('const link = user.is_owner && <Link title="Show hidden tasks">Trash</Link>;\n')
            self.assertTrue(_safe_fast_patch(cwd=temp, untracked=[]))
            board.write_text('const link = user.is_owner || <Link>Trash</Link>;\n')
            self.assertFalse(_safe_fast_patch(cwd=temp, untracked=[]))
            board.write_text('const amount = 1;\n')
            self.assertFalse(_safe_fast_patch(cwd=temp, untracked=[]))
            board.write_text('const link = user.is_owner && <Link>Trash</Link>;\n')
            styles = root / "frontend/src/styles.css"
            styles.write_text(".tag { color: red; }\n")
            git("add", ".")
            git("commit", "-m", "baseline styles")
            styles.write_text(".tag { color: blue; }\n")
            self.assertTrue(_safe_fast_patch(cwd=temp, untracked=[]))
            styles.write_text('.tag { content: "$100"; }\n')
            self.assertFalse(_safe_fast_patch(cwd=temp, untracked=[]))

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
                "CODEX_STEP_OUTCOME": "failure",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            self.assertEqual(
                sent.call_args.args[0]["error"], "Which menu label should I use?"
            )
            self.assertEqual(sent.call_args.args[0]["failure_phase"], "implement")

    def test_validator_error_overrides_agent_success_prose(self) -> None:
        task = {
            "task_id": 24,
            "run_id": "00000000-0000-0000-0000-000000000024",
            "title": "Model update",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "agent-result.txt").write_text("Model update complete")
            (Path(temp) / "agent-failure.txt").write_text(
                "public agent task rejected: public agent cannot publish protected path: .env.example"
            )
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "JOB_STATUS": "failure",
                "FAILURE_PHASE": "implement",
                "CODEX_STEP_OUTCOME": "success",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            self.assertIn(".env.example", sent.call_args.args[0]["error"])
            self.assertNotIn("complete", sent.call_args.args[0]["error"])

    def test_no_change_failure_includes_labelled_agent_explanation(self) -> None:
        task = {
            "task_id": 27,
            "run_id": "00000000-0000-0000-0000-000000000027",
            "title": "Model update",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "agent-result.txt").write_text(
                "Could not verify the model price without an official source."
            )
            (Path(temp) / "agent-failure.txt").write_text(
                "public agent task rejected: agent produced no file changes"
            )
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "JOB_STATUS": "failure",
                "FAILURE_PHASE": "implement",
                "CODEX_STEP_OUTCOME": "success",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            error = sent.call_args.args[0]["error"]
            self.assertIn("agent produced no file changes", error)
            self.assertIn("Codex izohi (tasdiqlanmagan)", error)
            self.assertIn("official source", error)

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
            self.assertEqual(sent.call_args.args[0]["failure_phase"], "publish")

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


class ParametrizedCheckDiffTests(unittest.TestCase):
    """The local-executor entry point: pass ``task`` and skip every env var."""

    @staticmethod
    def _git_repo(root: Path) -> None:
        for args in (
            ("init", "-q"),
            ("config", "user.email", "ci@example.test"),
            ("config", "user.name", "CI Test"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    def test_blocked_credential_path_is_rejected_without_any_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            (root / ".env").write_text("SECRET=1\n")
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "pr",
            }
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "credential files"):
                    check_diff(cwd=root, task=task)

    def test_no_change_fails_closed_without_any_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "pr",
            }
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "no file changes"):
                    check_diff(cwd=root, task=task)

    def test_pr_mode_never_needs_a_fast_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            (root / "README.md").write_text("hello\n")
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "pr",
            }
            calls: list[str] = []
            with patch.dict(os.environ, {}, clear=True):
                result = check_diff(cwd=root, task=task, on_fallback=calls.append)
            self.assertFalse(result)
            self.assertEqual(calls, [])

    def test_fast_fallback_calls_on_fallback_without_writing_github_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            (root / "README.md").write_text("base\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True
            )
            (root / "backend/app/core").mkdir(parents=True)
            (root / "backend/app/core/security.py").write_text("OLD = True\n")
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "fast",
            }
            reasons: list[str] = []
            with patch.dict(os.environ, {}, clear=True):
                result = check_diff(cwd=root, task=task, on_fallback=reasons.append)
            self.assertTrue(result)
            self.assertEqual(len(reasons), 1)

    def test_fast_mode_with_a_safe_edit_needs_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            board = root / "frontend/src/pages/Board.tsx"
            board.parent.mkdir(parents=True)
            board.write_text("const link = user.is_owner && <Link>Trash</Link>;\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True
            )
            board.write_text(
                'const link = user.is_owner && <Link title="Show hidden tasks">Trash</Link>;\n'
            )
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "fast",
            }
            calls: list[str] = []
            with patch.dict(os.environ, {}, clear=True):
                result = check_diff(cwd=root, task=task, on_fallback=calls.append)
            self.assertFalse(result)
            self.assertEqual(calls, [])

    def test_reference_image_digest_is_rejected_without_runner_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._git_repo(root)
            image_bytes = b"\x89PNG\r\n\x1a\nreference-bytes"
            digest = hashlib.sha256(image_bytes).hexdigest()
            copied = root / "frontend/public/renamed.png"
            copied.parent.mkdir(parents=True)
            copied.write_bytes(image_bytes)
            task = {
                "task_id": 7,
                "run_id": "00000000-0000-0000-0000-000000000007",
                "title": "T",
                "description": "",
                "base_branch": "main",
                "mode": "pr",
            }
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "reference images"):
                    check_diff(cwd=root, task=task, image_digests=[digest])


class BranchNameAndPromptTests(unittest.TestCase):
    def test_branch_name_uses_the_fast_prefix_only_for_fast_mode(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000003"
        self.assertEqual(
            branch_name({"task_id": 3, "run_id": run_id, "mode": "pr"}),
            f"codex/task-3-{run_id}",
        )
        self.assertEqual(
            branch_name({"task_id": 3, "run_id": run_id, "mode": "fast"}),
            f"codex/fast/task-3-{run_id}",
        )

    def test_build_prompt_matches_the_prompt_prepare_writes(self) -> None:
        task = {
            "task_id": 9,
            "run_id": "00000000-0000-0000-0000-000000000009",
            "title": "Fix menu",
            "description": "Do the thing",
            "base_branch": "main",
            "mode": "pr",
        }
        with tempfile.TemporaryDirectory() as temp:
            environment = {
                "TASK_JSON": json.dumps(task),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(Path(temp) / "github-env"),
            }
            with patch.dict(os.environ, environment):
                prepare()
            written = (Path(temp) / "agent-prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(written, build_prompt(task))


class FailureReasonTests(unittest.TestCase):
    def test_prefers_fast_error_text_over_everything_else(self) -> None:
        self.assertEqual(
            failure_reason(
                failure_phase=None,
                codex_step_outcome=None,
                result_text=None,
                policy_error_text="ignored",
                fast_error_text="boom",
            ),
            "boom",
        )

    def test_falls_back_to_a_phase_specific_default_message(self) -> None:
        self.assertEqual(
            failure_reason(
                failure_phase="publish",
                codex_step_outcome=None,
                result_text=None,
                policy_error_text=None,
                fast_error_text=None,
            ),
            "Publisher failed before PR/deploy; inspect the GitHub run.",
        )
        self.assertEqual(
            failure_reason(
                failure_phase="implement",
                codex_step_outcome=None,
                result_text=None,
                policy_error_text=None,
                fast_error_text=None,
            ),
            "Agent workflow failed; inspect the GitHub run.",
        )

    def test_labels_the_no_change_explanation_from_the_agent_result(self) -> None:
        reason = failure_reason(
            failure_phase="implement",
            codex_step_outcome="success",
            result_text="Could not verify the price.",
            policy_error_text="agent produced no file changes",
            fast_error_text=None,
        )
        self.assertIn("agent produced no file changes", reason)
        self.assertIn("Codex izohi (tasdiqlanmagan)", reason)
        self.assertIn("Could not verify the price", reason)


if __name__ == "__main__":
    unittest.main()
