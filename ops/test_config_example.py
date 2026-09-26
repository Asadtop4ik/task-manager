"""agentsvc/config.example.json must load cleanly through the real config loader.

This is a regression guard for the schema mismatch caught in review: the example
file once used nested `paths`/`lanes`/`models`/`timeouts_s`/`heartbeat_interval_s`
wrapper objects that `agent_svc.config.load_config` rejects outright (it only
accepts the flat keys in `DEFAULT_CONFIG`). Loading the actual file through the
actual loader catches that class of drift; a hand-maintained key list would not.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agentsvc"))
from agent_svc.config import load_config

EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "agentsvc" / "config.example.json"


class ConfigExampleTests(unittest.TestCase):
    def test_loads_without_error(self):
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config["api_base_url"], "https://tasks.standart-eko.uz/api/v1")

    def test_every_lane_is_disabled_by_default(self):
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertFalse(config["code_lane_enabled"])
        self.assertFalse(config["chat_lane_enabled"])
        self.assertFalse(config["watch_enabled"])

    def test_chat_sandbox_is_read_only(self):
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config["model_matrix"]["chat"]["sandbox"], "read-only")

    def test_uses_revision_2_agent_codex_paths(self):
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config["mirrors_dir"], "/srv/agent-svc/mirrors")
        self.assertEqual(config["codex_home_code"], "/home/agent-codex/.codex-code")
        self.assertEqual(config["codex_home_chat"], "/home/agent-codex/.codex-chat")

    def test_derives_runs_dir_and_codex_child_prefix(self):
        config = load_config(EXAMPLE_CONFIG_PATH)
        self.assertEqual(config["runs_dir"], "/var/lib/agent-svc/runs")
        self.assertIn("agent-codex", config["codex_child_prefix"])


if __name__ == "__main__":
    unittest.main()
