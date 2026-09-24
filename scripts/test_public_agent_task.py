import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from public_agent_task import check_diff, prepare, task


PAYLOAD = {
    "repo_full_name": "muradjanov-dev/qurbot",
    "base_branch": "master",
    "mode": "pr",
    "task_id": 42,
    "run_id": "00000000-0000-0000-0000-000000000042",
    "task_revision": "a" * 64,
    "title": "Clarify the cart",
    "description": "Show the selected material",
}


class PublicAgentTaskTests(unittest.TestCase):
    def test_only_the_three_exact_repo_branch_pairs_are_accepted(self) -> None:
        for repository, branch in (
            ("muradjanov-dev/qurbot", "master"),
            ("muradjanov-dev/kans-shop", "main"),
            ("muradjanov-dev/ketoshop", "master"),
        ):
            with self.subTest(repository=repository), patch.dict(
                os.environ,
                {"TASK_JSON": json.dumps(PAYLOAD | {"repo_full_name": repository, "base_branch": branch})},
            ):
                self.assertEqual(task()["repo_full_name"], repository)
        for changed in (
            {"repo_full_name": "untrusted/repo"},
            {"base_branch": "main"},
            {"mode": "fast"},
            {"task_revision": "bad"},
        ):
            with self.subTest(changed=changed), patch.dict(
                os.environ, {"TASK_JSON": json.dumps(PAYLOAD | changed)}
            ), self.assertRaises(ValueError):
                task()

    def test_prepare_keeps_task_text_out_of_shell_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "TASK_JSON": json.dumps(PAYLOAD),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(Path(temp) / "github-env"),
            },
        ):
            prepare()
            environment = (Path(temp) / "github-env").read_text()
            prompt = (Path(temp) / "agent-prompt.txt").read_text()
            self.assertIn("AGENT_REPO=muradjanov-dev/qurbot", environment)
            self.assertNotIn("Show the selected material", environment)
            self.assertIn("Show the selected material", prompt)

    def test_public_patch_cannot_modify_workflows_or_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            for relative in (".github/workflows/ci.yml", "AGENTS.md"):
                candidate = root / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "protected path"):
                check_diff(temp)

    def test_normal_application_patch_is_allowed_without_public_push_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "app/main.py"
            source.parent.mkdir()
            source.write_text("LABEL = 'old'\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
                cwd=root, check=True,
            )
            source.write_text("LABEL = 'new'\n")
            with patch.dict(
                os.environ,
                {
                    "TASK_JSON": json.dumps(PAYLOAD),
                    "RUNNER_TEMP": temp,
                    "GITHUB_ENV": str(root / "github-env"),
                },
            ):
                check_diff(temp)


if __name__ == "__main__":
    unittest.main()
