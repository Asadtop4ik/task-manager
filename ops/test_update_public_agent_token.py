import tempfile
import unittest
from pathlib import Path

from update_public_agent_token import update_env


class PublicTokenUpdateTests(unittest.TestCase):
    def test_updates_only_the_public_token_key_and_preserves_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-manager.env"
            path.write_text("AGENT_PUBLIC_ENABLED=false\nGITHUB_PUBLIC_AGENT_TOKEN=old\nSERVICE_TOKEN=keep\n")
            path.chmod(0o600)
            update_env(path, "github_pat_" + "a" * 24)
            contents = path.read_text()
            self.assertEqual(contents.count("GITHUB_PUBLIC_AGENT_TOKEN="), 1)
            self.assertIn("SERVICE_TOKEN=keep", contents)
            self.assertIn("AGENT_PUBLIC_ENABLED=false", contents)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_incorrect_token_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-manager.env"
            path.write_text("AGENT_PUBLIC_ENABLED=false\n")
            with self.assertRaises(ValueError):
                update_env(path, "not-a-fine-grained-token")
            self.assertEqual(path.read_text(), "AGENT_PUBLIC_ENABLED=false\n")


if __name__ == "__main__":
    unittest.main()
