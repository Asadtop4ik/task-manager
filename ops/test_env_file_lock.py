import fcntl
import os
import tempfile
import unittest
from pathlib import Path

from env_file_lock import locked_env_file


class EnvFileLockTests(unittest.TestCase):
    def test_creates_a_root_only_sibling_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text("KEY=value\n")
            with locked_env_file(env_file):
                pass
            lock_file = Path(directory) / "task-manager.env.lock"
            self.assertTrue(lock_file.exists())
            self.assertEqual(lock_file.stat().st_mode & 0o777, 0o600)

    def test_lock_is_released_on_exit_even_after_an_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text("KEY=value\n")
            with self.assertRaises(RuntimeError), locked_env_file(env_file):
                raise RuntimeError("boom")

            lock_file = Path(directory) / "task-manager.env.lock"
            fd = os.open(lock_file, os.O_RDWR)
            try:
                # Would raise BlockingIOError if the previous holder never unlocked.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def test_a_second_concurrent_lock_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "task-manager.env"
            env_file.write_text("KEY=value\n")
            lock_file = Path(directory) / "task-manager.env.lock"

            with locked_env_file(env_file):
                other_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(other_fd)


if __name__ == "__main__":
    unittest.main()
