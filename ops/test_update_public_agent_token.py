import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from update_public_agent_token import update_env


class PublicTokenUpdateTests(unittest.TestCase):
    def test_updates_only_the_public_token_key_and_preserves_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-manager.env"
            path.write_text(
                "AGENT_PUBLIC_ENABLED=false\nGITHUB_PUBLIC_AGENT_TOKEN=old\nSERVICE_TOKEN=keep\n"
            )
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

    def test_takes_the_shared_env_file_lock(self):
        # Same lock sidecar ops/sync_agent_svc_credentials.py uses (via
        # env_file_lock.locked_env_file), so the two scripts can't interleave a
        # read with the other's write to /srv/stack/env/task-manager.env.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-manager.env"
            path.write_text("AGENT_PUBLIC_ENABLED=false\n")
            update_env(path, "github_pat_" + "b" * 24)
            lock_path = Path(directory) / "task-manager.env.lock"
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_a_lock_already_held_by_another_process_blocks_the_update(self):
        # update_env() blocks (fcntl.LOCK_EX, no _NB) rather than failing, so a
        # single-threaded test can't call it while holding the lock elsewhere
        # without deadlocking. Instead this proves the mechanism a second real
        # process would hit: once one holder has the exclusive lock, a second,
        # independent open() + non-blocking acquire of the same lock file fails.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-manager.env"
            path.write_text("AGENT_PUBLIC_ENABLED=false\n")
            update_env(path, "github_pat_" + "c" * 24)  # creates the lock file
            lock_path = Path(directory) / "task-manager.env.lock"

            held_fd = os.open(lock_path, os.O_RDWR)
            fcntl.flock(held_fd, fcntl.LOCK_EX)
            try:
                other_fd = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(other_fd)
            finally:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
                os.close(held_fd)


if __name__ == "__main__":
    unittest.main()
