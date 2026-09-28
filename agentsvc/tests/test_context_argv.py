"""The production child argv must be exactly what ops/agent-svc.sudoers allows."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.config import build_settings, load_config
from agent_svc.context import _build_codex_runner

from .support import _SECRETS

SUDOERS = Path(__file__).resolve().parents[2] / "ops" / "agent-svc.sudoers"


def _allowed_commands() -> set[str]:
    """Every command string the sudoers rules grant, whitespace-normalised."""
    text = SUDOERS.read_text(encoding="utf-8")
    # Join continuation lines, drop comments.
    text = re.sub(r"\\\n", " ", text)
    commands: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "NOPASSWD:" not in line:
            continue
        for command in line.split("NOPASSWD:", 1)[1].split(","):
            commands.add(" ".join(command.split()))
    return commands


class ProductionArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A missing config file means production defaults.
        config = load_config(Path(tmp.name) / "absent.json")
        self.runner = _build_codex_runner(build_settings(config, _SECRETS))

    def test_each_subcommand_argv_matches_a_sudoers_rule_exactly(self) -> None:
        allowed = _allowed_commands()
        for sub in ("prepare", "exec", "package", "preflight", "cleanup"):
            argv = self.runner._argv(sub)
            self.assertEqual(
                argv[:6],
                ["/usr/bin/sudo", "-n", "-u", "agent-codex", "--", "/usr/bin/python3"],
            )
            self.assertEqual(argv.count("-I"), 1, argv)
            self.assertIn(" ".join(argv[5:]), allowed, argv)

    def test_discussion_argv_uses_the_diagnostics_group_rule(self) -> None:
        argv = self.runner._argv("discussion", group="task-diag-client")
        self.assertEqual(
            argv[:8],
            [
                "/usr/bin/sudo",
                "-n",
                "-u",
                "agent-codex",
                "-g",
                "task-diag-client",
                "--",
                "/usr/bin/python3",
            ],
        )
        self.assertEqual(argv.count("-I"), 1, argv)
        self.assertIn(" ".join(argv[7:]), _allowed_commands())


class ChildFailureReasonTests(unittest.TestCase):
    def test_sudo_refusal_reason_reaches_the_error(self) -> None:
        from agent_svc.codex import CodexChildError, CodexRunner

        runner = CodexRunner()
        with self.assertRaises(CodexChildError) as ctx:
            runner._parse_child_reply("prepare", 1, b"", b"sudo: a password is required\n")
        self.assertIn("sudo: a password is required", ctx.exception.reason)


if __name__ == "__main__":
    unittest.main()
