"""The public workflow copies trusted scripts into RUNNER_TEMP before validation."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CatalogStagingTests(unittest.TestCase):
    def test_trusted_copies_load_the_same_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for source in (
                ROOT / "scripts" / "public_agent_task.py",
                ROOT / "scripts" / "agent_task.py",
                ROOT / "backend" / "app" / "services" / "agent_repos.py",
            ):
                shutil.copyfile(source, target / source.name)
            result = subprocess.run(
                [sys.executable, "-c", "from public_agent_task import APPROVED_REPOS; print(len(APPROVED_REPOS))"],
                cwd=target,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "3")
