from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sync_agent_svc_credentials import GENERATED_ENV_NAME, sync_agent_svc_credentials


class SyncAgentSvcCredentialsTests(unittest.TestCase):
    def test_copies_mapped_values_and_generates_missing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(
                "SERVICE_TOKEN=unrelated\n"
                "AGENT_CALLBACK_TOKEN=cb-secret\n"
                "INTAKE_WORKER_TOKEN=iw-secret\n"
                "GITHUB_AGENT_TOKEN=gh-secret\n"
                "GITHUB_PUBLIC_AGENT_TOKEN=ghp-secret\n"
                "GITHUB_AGENT_QA_TOKEN=\n"
            )
            env_file.chmod(0o600)
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "0" * 64,
            )

            self.assertEqual(outcome, "created")
            self.assertEqual(
                (credentials_dir / "callback_token").read_text(), "cb-secret"
            )
            self.assertEqual(
                (credentials_dir / "intake_worker_token").read_text(), "iw-secret"
            )
            self.assertEqual(
                (credentials_dir / "github_agent_token").read_text(), "gh-secret"
            )
            self.assertEqual(
                (credentials_dir / "github_public_agent_token").read_text(),
                "ghp-secret",
            )
            self.assertEqual((credentials_dir / "github_qa_token").read_text(), "")
            self.assertEqual(
                (credentials_dir / "agent_svc_token").read_text(), "0" * 64
            )
            for name in (
                "callback_token",
                "intake_worker_token",
                "github_agent_token",
                "github_public_agent_token",
                "github_qa_token",
                "agent_svc_token",
            ):
                mode = (credentials_dir / name).stat().st_mode & 0o777
                self.assertEqual(mode, 0o600, name)

            self.assertIn(f"{GENERATED_ENV_NAME}=0000", env_file.read_text())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_returns_only_a_status_word_never_a_secret_value(self):
        # sync_agent_svc_credentials() returns "created"/"exists" only; the
        # __main__ block prints that same wording, never a token value.
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text("AGENT_CALLBACK_TOKEN=super-secret-value\n")
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "1" * 64,
            )

            self.assertIn(outcome, ("created", "exists"))

    def test_existing_token_is_preserved_not_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(f"{GENERATED_ENV_NAME}=already-here\n")
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("token_factory must not be called")
                ),
            )

            self.assertEqual(outcome, "exists")
            self.assertEqual(
                (credentials_dir / "agent_svc_token").read_text(), "already-here"
            )
            self.assertEqual(env_file.read_text().count(GENERATED_ENV_NAME), 1)

    def test_idempotent_rerun_keeps_a_single_generated_token_line(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text("AGENT_CALLBACK_TOKEN=cb-secret\n")
            credentials_dir = Path(directory) / "credentials"

            first = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "2" * 64,
            )
            second = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "should-not-be-used",
            )

            self.assertEqual(first, "created")
            self.assertEqual(second, "exists")
            self.assertEqual(env_file.read_text().count(GENERATED_ENV_NAME), 1)
            self.assertEqual(
                (credentials_dir / "agent_svc_token").read_text(), "2" * 64
            )

    def test_missing_env_file_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does-not-exist.env"
            with self.assertRaises(FileNotFoundError):
                sync_agent_svc_credentials(
                    task_manager_env_file=missing,
                    credentials_dir=Path(directory) / "credentials",
                )


if __name__ == "__main__":
    unittest.main()
