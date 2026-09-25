import unittest
from pathlib import Path
from unittest.mock import patch

from agent_preflight import run


class AgentPreflightTests(unittest.TestCase):
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
            calls[0], ["ruff", "format", "--", "app/bot/keyboards/inline.py"]
        )
        self.assertEqual(calls[1], ["ruff", "check", "."])
        self.assertEqual(calls[2], ["ruff", "format", "--check", "."])
        self.assertEqual(calls[3], ["git", "add", "--", "app/bot/keyboards/inline.py"])
        self.assertIn("passed", result)

    def test_unknown_repository_cannot_install_or_run_a_tool(self):
        with patch("agent_preflight.changed_python", return_value=["x.py"]), patch(
            "agent_preflight.subprocess.run"
        ) as command, self.assertRaises(ValueError):
            run("attacker/repository", Path("/tmp/target"))
        command.assert_not_called()
