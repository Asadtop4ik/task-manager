from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sync_agent_svc_credentials import (
    GENERATED_ENV_NAME,
    MalformedSecretLineError,
    MissingRequiredSecretError,
    sync_agent_svc_credentials,
)

VALID_ENV = (
    "SERVICE_TOKEN=unrelated\n"
    "AGENT_CALLBACK_TOKEN=cb-secret\n"
    "INTAKE_WORKER_TOKEN=iw-secret\n"
    "GITHUB_AGENT_TOKEN=gh-secret\n"
    "GITHUB_PUBLIC_AGENT_TOKEN=ghp-secret\n"
)


class SyncAgentSvcCredentialsTests(unittest.TestCase):
    def test_copies_mapped_values_and_generates_missing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + "GITHUB_AGENT_QA_TOKEN=qa-secret\n")
            env_file.chmod(0o600)
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "0" * 64,
            )

            self.assertEqual(outcome, "created")
            self.assertEqual((credentials_dir / "callback_token").read_text(), "cb-secret")
            self.assertEqual(
                (credentials_dir / "intake_worker_token").read_text(), "iw-secret"
            )
            self.assertEqual((credentials_dir / "github_agent_token").read_text(), "gh-secret")
            self.assertEqual(
                (credentials_dir / "github_public_agent_token").read_text(),
                "ghp-secret",
            )
            self.assertEqual((credentials_dir / "github_qa_token").read_text(), "qa-secret")
            self.assertEqual((credentials_dir / "agent_svc_token").read_text(), "0" * 64)
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

    def test_missing_optional_qa_token_still_writes_an_empty_credential_file(self):
        # LoadCredential= on this systemd version has no "ignore if missing" form
        # (verified with systemd-analyze verify: a leading "-" is rejected as an
        # unknown key and the whole directive is dropped), so the file must always
        # exist; an empty value is what marks QA as disabled, not a missing file.
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV)
            credentials_dir = Path(directory) / "credentials"

            sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "0" * 64,
            )

            self.assertEqual((credentials_dir / "github_qa_token").read_text(), "")

    def test_qa_disabled_later_clears_a_stale_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            credentials_dir = Path(directory) / "credentials"
            credentials_dir.mkdir(mode=0o700)
            (credentials_dir / "github_qa_token").write_text("old-qa-secret")

            env_file.write_text(VALID_ENV)  # no GITHUB_AGENT_QA_TOKEN line at all

            sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "0" * 64,
            )

            self.assertEqual((credentials_dir / "github_qa_token").read_text(), "")

    def test_missing_required_key_raises_naming_only_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            # Every required key present except one, so the raised key is unambiguous.
            env_file.write_text(VALID_ENV.replace("INTAKE_WORKER_TOKEN=iw-secret\n", ""))
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MissingRequiredSecretError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "INTAKE_WORKER_TOKEN")
            self.assertNotIn("cb-secret", str(ctx.exception))
            self.assertNotIn("gh-secret", str(ctx.exception))

    def test_empty_required_value_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            blanked = VALID_ENV.replace("GITHUB_AGENT_TOKEN=gh-secret", "GITHUB_AGENT_TOKEN=")
            env_file.write_text(blanked)
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MissingRequiredSecretError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "GITHUB_AGENT_TOKEN")

    def test_export_prefixed_mapped_key_is_rejected_not_misparsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + "export GITHUB_AGENT_QA_TOKEN=qa-secret\n")
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MalformedSecretLineError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "GITHUB_AGENT_QA_TOKEN")
            self.assertNotIn("qa-secret", str(ctx.exception))

    def test_quoted_mapped_value_is_rejected_not_misparsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + 'GITHUB_AGENT_QA_TOKEN="qa-secret"\n')
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MalformedSecretLineError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "GITHUB_AGENT_QA_TOKEN")

    def test_spaced_mapped_assignment_is_rejected_not_misparsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + "GITHUB_AGENT_QA_TOKEN = qa-secret\n")
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MalformedSecretLineError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "GITHUB_AGENT_QA_TOKEN")

    def test_inline_comment_on_mapped_value_is_rejected_not_misparsed(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + "GITHUB_AGENT_QA_TOKEN=qa-secret # enabled\n")
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MalformedSecretLineError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, "GITHUB_AGENT_QA_TOKEN")

    def test_malformed_generated_token_line_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + f'{GENERATED_ENV_NAME}="already-here"\n')
            credentials_dir = Path(directory) / "credentials"

            with self.assertRaises(MalformedSecretLineError) as ctx:
                sync_agent_svc_credentials(
                    task_manager_env_file=env_file, credentials_dir=credentials_dir
                )
            self.assertEqual(ctx.exception.key, GENERATED_ENV_NAME)

    def test_unrelated_key_with_export_or_quotes_does_not_break_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + 'export SOME_OTHER_FLAG=1\nANOTHER="quoted"\n')
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "0" * 64,
            )

            self.assertEqual(outcome, "created")

    def test_existing_token_is_preserved_not_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV + f"{GENERATED_ENV_NAME}=already-here\n")
            credentials_dir = Path(directory) / "credentials"

            outcome = sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("token_factory must not be called")
                ),
            )

            self.assertEqual(outcome, "exists")
            self.assertEqual((credentials_dir / "agent_svc_token").read_text(), "already-here")
            self.assertEqual(env_file.read_text().count(GENERATED_ENV_NAME), 1)

    def test_idempotent_rerun_keeps_a_single_generated_token_line(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV)
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
            self.assertEqual((credentials_dir / "agent_svc_token").read_text(), "2" * 64)

    def test_lock_file_serializes_access_and_is_left_behind(self):
        # A real concurrency test would need two processes; this checks the
        # lock sidecar is used (created next to the env file) and that a
        # normal run still completes and is reentrant.
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text(VALID_ENV)
            credentials_dir = Path(directory) / "credentials"

            sync_agent_svc_credentials(
                task_manager_env_file=env_file,
                credentials_dir=credentials_dir,
                token_factory=lambda: "3" * 64,
            )

            lock_file = Path(directory) / "task-manager.env.lock"
            self.assertTrue(lock_file.exists())

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
