import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from public_agent_task import approved_repositories, build_prompt, check_diff, prepare, task

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

    def test_private_qa_repository_requires_explicit_runtime_flag(self) -> None:
        qa_payload = PAYLOAD | {
            "repo_full_name": "Asadtop4ik/agent-qa",
            "base_branch": "main",
        }
        with patch.dict(
            os.environ, {"TASK_JSON": json.dumps(qa_payload)}, clear=True
        ), self.assertRaises(ValueError):
            task()
        with patch.dict(
            os.environ,
            {
                "TASK_JSON": json.dumps(qa_payload),
                "AGENT_QA_ENABLED": "true",
                "AGENT_QA_REPOSITORY": "Asadtop4ik/agent-qa",
            },
            clear=True,
        ):
            self.assertEqual(task()["repo_full_name"], "Asadtop4ik/agent-qa")

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
            self.assertIn(".env.example", prompt)
            self.assertIn("already approved implementation", prompt)

    def test_rejected_sample_env_path_writes_the_actual_callback_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".env.example").write_text("AGENT_MODEL=example\n")
            env = os.environ | {"RUNNER_TEMP": temp}
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("public_agent_task.py")), "check-diff"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(".env.example", (root / "agent-failure.txt").read_text())

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


class ParametrizedCheckDiffTests(unittest.TestCase):
    """The local-executor entry point: pass ``task`` and skip every env var."""

    def test_agents_md_is_rejected_without_any_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "AGENTS.md").write_text("changed\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "protected path"):
                    check_diff(root, task=PAYLOAD)

    def test_github_workflow_directory_is_rejected_without_any_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("changed\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "protected path"):
                    check_diff(root, task=PAYLOAD)

    def test_symlink_is_rejected_without_any_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            target = root / "real.txt"
            target.write_text("data\n")
            link = root / "link.txt"
            link.symlink_to(target)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "protected path"):
                    check_diff(root, task=PAYLOAD)

    def test_ordinary_patch_is_allowed_and_reaches_the_base_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "app/main.py"
            source.parent.mkdir()
            source.write_text("LABEL = 'new'\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(check_diff(root, task=PAYLOAD))

    def test_build_prompt_matches_the_prompt_prepare_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            environment = {
                "TASK_JSON": json.dumps(PAYLOAD),
                "RUNNER_TEMP": temp,
                "GITHUB_ENV": str(Path(temp) / "github-env"),
            }
            with patch.dict(os.environ, environment):
                prepare()
            written = (Path(temp) / "agent-prompt.txt").read_text(encoding="utf-8")
        normalized = PAYLOAD | {
            "title": PAYLOAD["title"].strip(),
            "description": PAYLOAD["description"].strip(),
        }
        self.assertEqual(written, build_prompt(normalized))

    def test_qa_enabled_argument_bypasses_the_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Asadtop4ik/agent-qa", approved_repositories())
            self.assertNotIn("Asadtop4ik/agent-qa", approved_repositories(False))
            self.assertIn("Asadtop4ik/agent-qa", approved_repositories(True))
            self.assertNotIn(
                "Asadtop4ik/agent-qa",
                approved_repositories(True, qa_repository="someone-else/other"),
            )


if __name__ == "__main__":
    unittest.main()
