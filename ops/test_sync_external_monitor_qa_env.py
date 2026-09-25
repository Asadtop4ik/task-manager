from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sync_external_monitor_qa_env import sync_qa_environment


class SyncExternalMonitorQaEnvTests(unittest.TestCase):
    def test_enabled_qa_syncs_only_the_feature_flag_and_scoped_token(self):
        token = "github_pat_qa_read_only_token"
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "external-monitor.env"
            env_file.write_text("GITHUB_AGENT_TOKEN=generic\nOTHER=value\n")
            env_file.chmod(0o640)

            sync_qa_environment(
                env_file=env_file,
                task_api_env={
                    "AGENT_QA_ENABLED": "true",
                    "GITHUB_AGENT_QA_TOKEN": token,
                    "AGENT_CALLBACK_TOKEN": "must-not-copy",
                },
            )

            values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
            self.assertEqual(values["AGENT_QA_ENABLED"], "true")
            self.assertEqual(values["GITHUB_AGENT_QA_TOKEN"], token)
            self.assertEqual(values["GITHUB_AGENT_TOKEN"], "generic")
            self.assertEqual(values["OTHER"], "value")
            self.assertNotIn("AGENT_CALLBACK_TOKEN", values)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o640)

    def test_disabled_qa_clears_a_stale_scoped_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "external-monitor.env"
            env_file.write_text(
                "GITHUB_AGENT_TOKEN=generic\n"
                "AGENT_QA_ENABLED=true\n"
                "GITHUB_AGENT_QA_TOKEN=old_token\n"
            )

            sync_qa_environment(env_file=env_file, task_api_env={"AGENT_QA_ENABLED": "false"})

            values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
            self.assertEqual(values["AGENT_QA_ENABLED"], "false")
            self.assertEqual(values["GITHUB_AGENT_QA_TOKEN"], "")

    def test_enabled_qa_requires_scoped_token_without_changing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "external-monitor.env"
            original = "GITHUB_AGENT_TOKEN=generic\n"
            env_file.write_text(original)

            with self.assertRaisesRegex(ValueError, "repository-scoped"):
                sync_qa_environment(
                    env_file=env_file,
                    task_api_env={"AGENT_QA_ENABLED": "true"},
                )

            self.assertEqual(env_file.read_text(), original)


if __name__ == "__main__":
    unittest.main()
