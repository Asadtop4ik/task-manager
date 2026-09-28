"""ops/ops-allowlist.example.json must load cleanly through the real policy module.

This is the "feature off" default the installer puts at
`/etc/agent-svc/ops-allowlist.json` the first time (only if that path is absent --
see `install_agent_svc.sh`), so it must parse against the real, current
`backend/app/services/agent_repos.py` catalog with `agent_ops_policy.load_allowlist`
exactly as the root env-apply helper (`agentsvc/libexec/env_apply.py`) would load
it in production, not just as bare JSON. Loading the actual file through the
actual loader catches drift between this example and either module's schema, the
same regression class `ops/test_config_example.py` guards for `config.example.json`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "backend" / "app" / "services"))

from agent_ops_policy import Allowlist, load_allowlist  # noqa: E402
from agent_repos import REPOSITORIES  # noqa: E402

EXAMPLE_PATH = REPO_ROOT / "ops" / "ops-allowlist.example.json"


class OpsAllowlistExampleTests(unittest.TestCase):
    def test_loads_without_error_against_the_real_catalog(self) -> None:
        allowlist = load_allowlist(EXAMPLE_PATH, REPOSITORIES, require_root_owned=False)
        self.assertIsInstance(allowlist, Allowlist)

    def test_feature_is_off_by_default(self) -> None:
        # "off" means an empty project map: `validate_request` refuses every
        # project with `not_allowlisted` until an operator adds one by hand.
        allowlist = load_allowlist(EXAMPLE_PATH, REPOSITORIES, require_root_owned=False)
        self.assertEqual(dict(allowlist.projects), {})

    def test_version_is_1(self) -> None:
        allowlist = load_allowlist(EXAMPLE_PATH, REPOSITORIES, require_root_owned=False)
        self.assertEqual(allowlist.version, 1)

    def test_file_is_not_group_or_world_writable(self) -> None:
        # The installer copies this file to /etc/agent-svc/ops-allowlist.json
        # 0644 root:root; the loader itself independently refuses a
        # group/world-writable file regardless of what the installer does,
        # so this only guards the checked-in example's own permission bits.
        mode = EXAMPLE_PATH.stat().st_mode
        self.assertEqual(mode & 0o022, 0)


if __name__ == "__main__":
    unittest.main()
