"""Exercise the server deploy script with fake Docker, without touching a host."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeployRollbackTests(unittest.TestCase):
    def test_unhealthy_new_image_restores_previous_sha(self) -> None:
        old_sha = "a" * 40
        bad_sha = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts" / "deploy.sh"
            script.parent.mkdir()
            (root / "stacks").mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / "ops/deploy.sh", script)
            script.chmod(0o755)
            state = root / "image-sha"
            state.write_text(old_sha)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = inspect ]; then\n"
                "  tag=$(cat \"$MOCK_STATE\")\n"
                "  case \"$4\" in\n"
                "    task-api) image=task-manager-api ;;\n"
                "    task-bot|task-worker) image=task-manager-bot ;;\n"
                "    task-frontend) image=task-manager-frontend ;;\n"
                "  esac\n"
                "  echo \"ghcr.io/asadtop4ik/$image:$tag\"\n"
                "elif [ \"$1\" = compose ]; then\n"
                "  case \" $* \" in\n"
                "    *' up '*)\n"
                "      if [ \"$IMAGE_TAG\" = \"$BAD_TAG\" ]; then exit 1; fi\n"
                "      echo \"$IMAGE_TAG\" > \"$MOCK_STATE\" ;;\n"
                "  esac\n"
                "fi\n"
            )
            docker.chmod(0o755)
            curl = bin_dir / "curl"
            curl.write_text("#!/bin/sh\necho '{\"status\":\"ok\",\"checks\":{\"postgres\":\"ok\",\"redis\":\"ok\"}}'\n")
            curl.chmod(0o755)
            result = subprocess.run(
                ["bash", str(script), "task-manager", bad_sha],
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "MOCK_STATE": str(state),
                    "BAD_TAG": bad_sha,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state.read_text().strip(), old_sha)
            self.assertIn("rollback verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
