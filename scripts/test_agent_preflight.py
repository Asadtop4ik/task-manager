import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_preflight import changed_python, ensure_tools, failure_reason, run


class AgentPreflightTests(unittest.TestCase):
    def make_agent_qa_repo(self, root: Path, changed_source: str) -> tuple[Path, Path]:
        changed = root / "changed.py"
        unchanged = root / "unchanged.py"
        changed.write_text("VALUE = 0\n", encoding="utf-8")
        unchanged.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
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
        changed.write_text(changed_source, encoding="utf-8")
        subprocess.run(["git", "add", "changed.py"], cwd=root, check=True)
        return changed, unchanged

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

    def test_agent_qa_formats_and_stages_only_changed_python_files(self):
        with patch(
            "agent_preflight.changed_python", return_value=["changed.py"]
        ), patch("agent_preflight.ensure_tools"), patch(
            "agent_preflight.subprocess.run"
        ) as command:
            run("Asadtop4ik/agent-qa", Path("/tmp/target"))

        calls = [item.args[0] for item in command.call_args_list]
        self.assertIn(["ruff", "format", "--", "changed.py"], calls)
        self.assertIn(["git", "add", "--", "changed.py"], calls)
        self.assertNotIn(["ruff", "format", "--", "unchanged.py"], calls)
        self.assertNotIn(["git", "add", "--", "unchanged.py"], calls)

    def test_agent_qa_config_only_patch_still_checks_without_mutating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "pyproject.toml"
            unchanged = root / "unchanged.py"
            config.write_text("[tool.ruff]\nline-length = 88\n", encoding="utf-8")
            unchanged.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
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
            config.write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
            subprocess.run(["git", "add", "pyproject.toml"], cwd=root, check=True)
            original_unchanged = unchanged.read_bytes()
            original_run = subprocess.run
            calls = []

            def run_command(command, *args, **kwargs):
                calls.append(command)
                if command[0] == "ruff":
                    return subprocess.CompletedProcess(command, 0)
                return original_run(command, *args, **kwargs)

            with patch("agent_preflight.ensure_tools") as install, patch(
                "agent_preflight.subprocess.run", side_effect=run_command
            ):
                result = run("Asadtop4ik/agent-qa", root)

            install.assert_called_once_with("ruff==0.7.4")
            self.assertIn(["ruff", "check", "."], calls)
            self.assertIn(["ruff", "format", "--check", "."], calls)
            self.assertFalse(any("--fix" in command for command in calls))
            self.assertNotIn(["git", "add", "--", "pyproject.toml"], calls)
            self.assertIn("passed", result)
            self.assertEqual(unchanged.read_bytes(), original_unchanged)
            staged = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=root, text=True
            ).splitlines()
            self.assertEqual(staged, ["pyproject.toml"])

    @unittest.skipUnless(shutil.which("ruff"), "Ruff is installed by QA CI")
    def test_agent_qa_fixes_f401_and_leaves_unmodified_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed, unchanged = self.make_agent_qa_repo(
                root, "import os\n\nVALUE = 1\n"
            )
            original_unchanged = unchanged.read_bytes()

            result = run("Asadtop4ik/agent-qa", root)

            self.assertIn("Ruff 0.7.4", result)
            self.assertEqual(changed.read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertEqual(unchanged.read_bytes(), original_unchanged)
            staged = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=root, text=True
            ).splitlines()
            self.assertEqual(staged, ["changed.py"])
            self.assertEqual(
                subprocess.check_output(
                    ["git", "show", ":changed.py"], cwd=root, text=True
                ),
                "VALUE = 1\n",
            )

    @unittest.skipUnless(shutil.which("ruff"), "Ruff is installed by QA CI")
    def test_agent_qa_fails_on_non_f401_lint_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed, _ = self.make_agent_qa_repo(
                root, "import os\n\nVALUE = missing_name\n"
            )

            with self.assertRaises(subprocess.CalledProcessError):
                run("Asadtop4ik/agent-qa", root)

            self.assertEqual(
                changed.read_text(encoding="utf-8"), "VALUE = missing_name\n"
            )
            staged = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"], cwd=root, text=True
            ).splitlines()
            self.assertEqual(staged, ["changed.py"])
            self.assertIn(
                "import os",
                subprocess.check_output(
                    ["git", "show", ":changed.py"], cwd=root, text=True
                ),
            )

    def test_unknown_repository_cannot_install_or_run_a_tool(self):
        with patch("agent_preflight.changed_python", return_value=["x.py"]), patch(
            "agent_preflight.subprocess.run"
        ) as command, self.assertRaises(ValueError):
            run("attacker/repository", Path("/tmp/target"))
        command.assert_not_called()

    @staticmethod
    def _fake_versioned_run(version_lines: dict[str, str]):
        def fake_run(command, **kwargs):
            if len(command) >= 2 and command[1] == "--version":
                name = Path(command[0]).name
                return subprocess.CompletedProcess(command, 0, stdout=version_lines[name])
            return subprocess.CompletedProcess(command, 0)

        return fake_run

    def test_local_executor_tools_mapping_uses_the_given_executable_and_never_calls_pip(self):
        tools = {"ruff": "/opt/agent-svc/tools/ruff-0.7.4/bin/ruff"}
        with patch(
            "agent_preflight.changed_python",
            return_value=["app/bot/keyboards/inline.py"],
        ), patch(
            "agent_preflight.subprocess.run",
            side_effect=self._fake_versioned_run({"ruff": "ruff 0.7.4\n"}),
        ) as command:
            result = run("muradjanov-dev/qurbot", Path("/tmp/target"), tools=tools)
        calls = [item.args[0] for item in command.call_args_list]
        self.assertIn([tools["ruff"], "--version"], calls)
        self.assertIn(
            [tools["ruff"], "check", "--fix", "--select", "F401", "--", "app/bot/keyboards/inline.py"],
            calls,
        )
        self.assertIn([tools["ruff"], "format", "--", "app/bot/keyboards/inline.py"], calls)
        self.assertIn([tools["ruff"], "check", "."], calls)
        self.assertTrue(
            all(command[0] != sys.executable or "pip" not in command for command in calls),
            "the local executor must never invoke pip",
        )
        self.assertIn("passed", result)

    def test_local_executor_tools_mapping_refuses_a_version_mismatch(self):
        tools = {"ruff": "/opt/agent-svc/tools/ruff-0.7.4/bin/ruff"}
        with patch(
            "agent_preflight.changed_python", return_value=["x.py"]
        ), patch(
            "agent_preflight.subprocess.run",
            side_effect=self._fake_versioned_run({"ruff": "ruff 9.9.9\n"}),
        ) as command:
            with self.assertRaises(RuntimeError):
                run("muradjanov-dev/qurbot", Path("/tmp/target"), tools=tools)
        calls = [item.args[0] for item in command.call_args_list]
        self.assertEqual(calls, [[tools["ruff"], "--version"]])
        self.assertTrue(
            all(sys.executable not in command for command in calls),
            "the local executor must never invoke pip",
        )

    def test_local_executor_tools_mapping_refuses_a_missing_executable(self):
        with patch("agent_preflight.changed_python", return_value=["x.py"]), patch(
            "agent_preflight.subprocess.run"
        ) as command:
            with self.assertRaises(RuntimeError):
                run("muradjanov-dev/qurbot", Path("/tmp/target"), tools={})
        command.assert_not_called()

    def test_ensure_tools_with_a_mapping_never_touches_shutil_which_or_pip(self):
        with patch("agent_preflight.shutil.which") as which, patch(
            "agent_preflight.subprocess.run",
            side_effect=self._fake_versioned_run({"black": "black, 26.5.1 (compiled: yes)\n"}),
        ):
            ensure_tools("black==26.5.1", tools={"black": "/opt/tools/black"})
        which.assert_not_called()
