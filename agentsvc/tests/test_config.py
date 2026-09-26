from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.config import (
    SECRET_NAMES,
    ConfigError,
    build_settings,
    load_config,
    load_secrets,
    load_settings,
)


def _write_secrets(directory: Path, **values: str) -> None:
    for name, value in values.items():
        (directory / name).write_text(value, encoding="utf-8")


def _all_secrets(directory: Path) -> None:
    _write_secrets(
        directory,
        agent_svc_token="svc-token\n",
        callback_token="callback-token\n",
        intake_worker_token="intake-token\n",
        github_agent_token="agent-token\n",
        github_public_agent_token="public-token\n",
        github_qa_token="qa-token\n",
    )


class LoadConfigTests(unittest.TestCase):
    def test_defaults_when_file_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "does-not-exist.json")
            self.assertEqual(config["api_base_url"], "https://tasks.standart-eko.uz/api/v1")
            self.assertEqual(config["mirrors_dir"], str(Path(config["state_dir"]) / "mirrors"))
            self.assertEqual(config["runs_dir"], str(Path(config["state_dir"]) / "runs"))
            self.assertEqual(
                config["codex_child_prefix"],
                ("/usr/bin/sudo", "-n", "-u", "codex-runner", "--", "/usr/bin/python3"),
            )
            self.assertFalse(config["code_lane_enabled"])
            self.assertTrue(config["watch_enabled"])

    def test_missing_explicit_path_falls_back_to_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "missing.json")
            self.assertEqual(config["api_base_url"], "https://tasks.standart-eko.uz/api/v1")

    def test_overrides_merge_onto_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"state_dir": "/custom/state", "poll_interval_s": 2}))
            config = load_config(path)
            self.assertEqual(config["state_dir"], "/custom/state")
            self.assertEqual(config["mirrors_dir"], "/custom/state/mirrors")
            self.assertEqual(config["poll_interval_s"], 2.0)
            self.assertIsInstance(config["poll_interval_s"], float)

    def test_unknown_key_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"totally_unknown": 1}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_secret_key_in_config_file_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"agent_svc_token": "leaked"}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_bad_bool_type_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"code_lane_enabled": "yes"}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_positive_interval_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"poll_interval_s": 0}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_json_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            with self.assertRaises(ConfigError):
                load_config(path)


class LoadSecretsTests(unittest.TestCase):
    def test_all_present_and_trailing_newline_stripped(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            secrets = load_secrets(directory)
            self.assertEqual(secrets["agent_svc_token"], "svc-token")
            self.assertEqual(set(secrets), set(SECRET_NAMES))

    def test_missing_secret_lists_names_only_never_values(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            (directory / "github_qa_token").unlink()
            with self.assertRaises(ConfigError) as ctx:
                load_secrets(directory)
            message = str(ctx.exception)
            self.assertIn("github_qa_token", message)
            self.assertNotIn("qa-token", message)

    def test_empty_secret_file_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            (directory / "callback_token").write_text("")
            with self.assertRaises(ConfigError) as ctx:
                load_secrets(directory)
            self.assertIn("callback_token", str(ctx.exception))

    def test_whitespace_only_secret_is_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            (directory / "callback_token").write_text("   \n")
            with self.assertRaises(ConfigError):
                load_secrets(directory)

    def test_missing_credentials_directory_env_and_arg_raises(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CREDENTIALS_DIRECTORY", None)
            with self.assertRaises(ConfigError):
                load_secrets(None)

    def test_credentials_directory_from_env(self) -> None:
        import os
        from unittest.mock import patch

        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)}):
                secrets = load_secrets(None)
            self.assertEqual(set(secrets), set(SECRET_NAMES))

    def test_nonexistent_credentials_directory_raises(self) -> None:
        with self.assertRaises(ConfigError):
            load_secrets("/no/such/directory/at/all")


class BuildSettingsTests(unittest.TestCase):
    def test_missing_secret_key_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "absent.json")
        with self.assertRaises(ConfigError):
            build_settings(config, {"agent_svc_token": "x"})

    def test_secret_values_order_matches_secret_names(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            config = load_config(Path(tmp) / "absent.json")
            secrets = load_secrets(directory)
            settings = build_settings(config, secrets)
            self.assertEqual(
                settings.secret_values(), tuple(secrets[name] for name in SECRET_NAMES)
            )


class LoadSettingsIntegrationTests(unittest.TestCase):
    def test_full_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            config_path = directory / "config.json"
            config_path.write_text(json.dumps({"api_base_url": "https://example.test/api/v1"}))
            settings = load_settings(config_path, directory)
            self.assertEqual(settings.api_base_url, "https://example.test/api/v1")
            self.assertEqual(settings.agent_svc_token, "svc-token")
            self.assertEqual(len(settings.secret_values()), len(SECRET_NAMES))


if __name__ == "__main__":
    unittest.main()
