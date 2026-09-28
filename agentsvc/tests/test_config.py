from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.config import (
    DEFAULT_MODEL_MATRIX,
    DEFAULT_TIMEOUTS,
    OPTIONAL_SECRET_NAMES,
    REQUIRED_SECRET_NAMES,
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


def _required_secrets_only(directory: Path) -> None:
    """Every required secret, but no optional (`github_qa_token`) file at all."""
    _write_secrets(
        directory,
        agent_svc_token="svc-token\n",
        callback_token="callback-token\n",
        intake_worker_token="intake-token\n",
        github_agent_token="agent-token\n",
        github_public_agent_token="public-token\n",
    )


def _all_secrets(directory: Path) -> None:
    _required_secrets_only(directory)
    _write_secrets(directory, github_qa_token="qa-token\n")


class LoadConfigTests(unittest.TestCase):
    def test_defaults_when_file_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "does-not-exist.json")
            self.assertEqual(config["api_base_url"], "https://tasks.standart-eko.uz/api/v1")
            # REVISION 2: mirrors live under the agentwork work tree, not under
            # state_dir, so agent-codex (not agent-svc) can clone them.
            self.assertEqual(config["mirrors_dir"], "/srv/agent-svc/mirrors")
            self.assertEqual(config["runs_dir"], str(Path(config["state_dir"]) / "runs"))
            self.assertEqual(
                config["codex_child_prefix"],
                ("/usr/bin/sudo", "-n", "-u", "agent-codex", "--", "/usr/bin/python3", "-I"),
            )
            self.assertEqual(config["codex_home_code"], "/home/agent-codex/.codex-code")
            self.assertEqual(config["codex_home_chat"], "/home/agent-codex/.codex-chat")
            self.assertFalse(config["code_lane_enabled"])
            self.assertFalse(config["watch_enabled"])

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
            self.assertEqual(config["runs_dir"], "/custom/state/runs")
            # mirrors_dir no longer derives from state_dir (REVISION 2): it
            # keeps its own fixed default unless explicitly overridden.
            self.assertEqual(config["mirrors_dir"], "/srv/agent-svc/mirrors")
            self.assertEqual(config["poll_interval_s"], 2.0)
            self.assertIsInstance(config["poll_interval_s"], float)

    def test_mirrors_dir_can_be_overridden_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mirrors_dir": "/custom/mirrors"}))
            config = load_config(path)
            self.assertEqual(config["mirrors_dir"], "/custom/mirrors")

    def test_empty_mirrors_dir_override_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mirrors_dir": "  "}))
            with self.assertRaises(ConfigError):
                load_config(path)

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

    def test_ops_lane_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "absent.json")
            self.assertFalse(config["ops_lane_enabled"])
            self.assertEqual(config["ops_allowlist_path"], "/etc/agent-svc/ops-allowlist.json")
            self.assertEqual(config["ops_request_dir"], "/run/agent-svc/ops")
            self.assertEqual(config["ops_results_dir"], "/var/lib/agent-ops/results")

    def test_ops_lane_enabled_can_be_overridden(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"ops_lane_enabled": True}))
            config = load_config(path)
            self.assertTrue(config["ops_lane_enabled"])


class ModelMatrixTimeoutsDeepMergeTests(unittest.TestCase):
    """The production bug this fixes: a config written before a lane
    existed (e.g. before the chat lane added `model_matrix.chat`/
    `timeouts.chat`) used to lose that lane's entry ENTIRELY -- the old
    loader did `{**DEFAULT_CONFIG, **raw}`, which replaces `model_matrix`/
    `timeouts` wholesale rather than merging per key -- and every `/suhbat`
    call crashed with `KeyError: 'chat'` the moment it read
    `settings.model_matrix["chat"]`."""

    def test_old_style_config_missing_a_whole_entry_still_gets_its_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            # Simulates a config saved before the chat lane existed: only
            # the lanes that existed at the time are present.
            path.write_text(
                json.dumps(
                    {
                        "model_matrix": {
                            "intake": {
                                "model": "gpt-6-luna",
                                "effort": "high",
                                "multi_agent": False,
                                "sandbox": "read-only",
                            }
                        },
                        "timeouts": {"intake": 180},
                    }
                )
            )
            config = load_config(path)
            self.assertEqual(config["model_matrix"]["chat"], DEFAULT_MODEL_MATRIX["chat"])
            self.assertEqual(config["timeouts"]["chat"], DEFAULT_TIMEOUTS["chat"])
            # Every other shipped lane/timeout is still there too.
            for name in DEFAULT_MODEL_MATRIX:
                self.assertIn(name, config["model_matrix"])
            for name in DEFAULT_TIMEOUTS:
                self.assertIn(name, config["timeouts"])

    def test_partial_entry_override_merges_per_field(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model_matrix": {"chat": {"effort": "high"}}}))
            config = load_config(path)
            self.assertEqual(config["model_matrix"]["chat"]["effort"], "high")
            # Every other field of THIS entry still comes from the default.
            self.assertEqual(
                config["model_matrix"]["chat"]["model"], DEFAULT_MODEL_MATRIX["chat"]["model"]
            )
            self.assertEqual(
                config["model_matrix"]["chat"]["sandbox"],
                DEFAULT_MODEL_MATRIX["chat"]["sandbox"],
            )

    def test_timeouts_partial_override_keeps_the_rest(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"timeouts": {"review": 999}}))
            config = load_config(path)
            self.assertEqual(config["timeouts"]["review"], 999)
            self.assertEqual(config["timeouts"]["chat"], DEFAULT_TIMEOUTS["chat"])

    def test_bad_multi_agent_type_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {"model_matrix": {"chat": {"multi_agent": "definitely-not-a-bool"}}}
                )
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_bad_model_type_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model_matrix": {"chat": {"model": ""}}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_unknown_field_inside_a_model_matrix_entry_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model_matrix": {"chat": {"bogus_field": 1}}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_unknown_timeouts_field_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"timeouts": {"not_a_real_lane": 10}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_positive_timeout_value_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"timeouts": {"chat": 0}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_new_model_matrix_entry_beyond_the_shipped_defaults_is_rejected(self) -> None:
        # A future lane's name isn't guessable here; a config referencing
        # one the current code does not know about must fail at startup
        # (loudly), not be silently accepted and then ignored everywhere.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "model_matrix": {
                            "future_lane": {
                                "model": "x",
                                "effort": "y",
                                "multi_agent": False,
                                "sandbox": "read-only",
                            }
                        }
                    }
                )
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_dict_model_matrix_entry_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model_matrix": {"chat": "not-an-object"}}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_dict_top_level_model_matrix_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model_matrix": "nope"}))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_dict_top_level_timeouts_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"timeouts": "nope"}))
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

    def test_missing_required_secret_lists_names_only_never_values(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _all_secrets(directory)
            (directory / "github_agent_token").unlink()
            with self.assertRaises(ConfigError) as ctx:
                load_secrets(directory)
            message = str(ctx.exception)
            self.assertIn("github_agent_token", message)
            self.assertNotIn("agent-token", message)

    def test_missing_optional_qa_secret_means_disabled_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _required_secrets_only(directory)
            self.assertFalse((directory / "github_qa_token").exists())
            secrets = load_secrets(directory)
            self.assertEqual(secrets["github_qa_token"], "")
            self.assertEqual(set(secrets), set(SECRET_NAMES))

    def test_present_but_empty_optional_qa_secret_means_disabled(self) -> None:
        # The installer always writes the QA credential (LoadCredential= has no
        # optional form); an empty file must disable QA, not stop the service.
        for content in ("", "\n", "  \n"):
            with TemporaryDirectory() as tmp:
                directory = Path(tmp)
                _required_secrets_only(directory)
                (directory / "github_qa_token").write_text(content)
                self.assertEqual(load_secrets(directory)["github_qa_token"], "")

    def test_required_and_optional_secret_names_partition_secret_names(self) -> None:
        self.assertEqual(
            set(REQUIRED_SECRET_NAMES) | set(OPTIONAL_SECRET_NAMES), set(SECRET_NAMES)
        )
        self.assertEqual(set(REQUIRED_SECRET_NAMES) & set(OPTIONAL_SECRET_NAMES), set())
        self.assertEqual(OPTIONAL_SECRET_NAMES, ("github_qa_token",))

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

    def test_round_trip_without_qa_secret_disables_qa_without_erroring(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _required_secrets_only(directory)
            settings = load_settings(Path(tmp) / "absent-config.json", directory)
            self.assertEqual(settings.github_qa_token, "")
            self.assertIn("", settings.secret_values())


if __name__ == "__main__":
    unittest.main()
