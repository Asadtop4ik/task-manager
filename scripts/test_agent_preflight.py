import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_preflight import changed_python, failure_reason, run


class AgentPreflightTests(unittest.TestCase):
    def test_ruff_failure_message_identifies_the_lint_boundary(self):
        error = subprocess.CalledProcessError(1, ["ruff", "check", "."])
        self.assertIn("F401", failure_reason(error))

    def test_deleted_python_file_is_not_sent_to_formatter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "old.py"
            source.write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "old.py"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=root,
                check=True,
            )
            source.unlink()
            subprocess.run(["git", "add", "-u"], cwd=root, check=True)
            self.assertEqual(changed_python(root), [])

    def test_qurbot_formats_then_checks_before_publication(self):
        with patch(
            "agent_preflight.changed_python",
            return_value=["app/bot/keyboards/inline.py"],
        ), patch("agent_preflight.ensure_tools") as install, patch(
            "agent_preflight.subprocess.run"
        ) as command:
            result = run("muradjanov-dev/qurbot", Path("/tmp/target"))
        calls = [item.args[0] for item in command.call_args_list]
        install.assert_called_once_with("ruff==0.7.4")
        self.assertEqual(
            calls[0],
            [
                "ruff",
                "check",
                "--fix",
                "--select",
                "F401",
                "--",
                "app/bot/keyboards/inline.py",
            ],
        )
        self.assertEqual(
            calls[1], ["ruff", "format", "--", "app/bot/keyboards/inline.py"]
        )
        self.assertEqual(calls[2], ["ruff", "check", "."])
        self.assertEqual(calls[3], ["ruff", "format", "--check", "."])
        self.assertEqual(calls[4], ["git", "add", "--", "app/bot/keyboards/inline.py"])
        self.assertIn("passed", result)

    def test_task_manager_fixes_unused_imports_only_in_changed_python(self):
        with patch(
            "agent_preflight.changed_python",
            return_value=["backend/app/changed.py", "bot/app/handlers/changed.py"],
        ), patch("agent_preflight.ensure_tools"), patch(
            "agent_preflight.subprocess.run"
        ) as command:
            run("Asadtop4ik/task-manager", Path("/tmp/target"))
        calls = [item.args[0] for item in command.call_args_list]
        self.assertIn(
            ["ruff", "check", "--fix", "--select", "F401", "--", "app/changed.py"],
            calls,
        )
        self.assertIn(
            [
                "ruff",
                "check",
                "--fix",
                "--select",
                "F401",
                "--",
                "app/handlers/changed.py",
            ],
            calls,
        )

    def test_unknown_repository_cannot_install_or_run_a_tool(self):
        with patch("agent_preflight.changed_python", return_value=["x.py"]), patch(
            "agent_preflight.subprocess.run"
        ) as command, self.assertRaises(ValueError):
            run("attacker/repository", Path("/tmp/target"))
        command.assert_not_called()
