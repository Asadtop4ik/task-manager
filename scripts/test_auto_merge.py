import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_merge
from auto_merge import (
    agent_ready,
    agent_run_id,
    allowed_files,
    clean_review_status,
    current_pr,
    latest_checks_pass,
)


class PolicyTests(unittest.TestCase):
    def test_only_small_document_and_css_changes_are_eligible(self) -> None:
        self.assertTrue(
            allowed_files(["README.md", "docs/HELP.md", "frontend/src/index.css"])
        )
        for path in [
            "AGENTS.md",
            ".github/workflows/ci.yml",
            "backend/app/auth.py",
            "frontend/src/App.tsx",
        ]:
            self.assertFalse(allowed_files([path]))
        self.assertFalse(allowed_files([]))

    def test_outdated_or_foreign_pr_cannot_merge(self) -> None:
        repo = "Asadtop4ik/task-manager"
        pr = {
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
            "base": {"ref": "main"},
            "head": {"sha": "a" * 40, "repo": {"full_name": repo}},
        }
        run = {"head_sha": "a" * 40}
        self.assertTrue(current_pr(pr, run, repo))
        self.assertFalse(current_pr(pr, {"head_sha": "b" * 40}, repo))
        self.assertFalse(current_pr({**pr, "draft": True}, run, repo))
        self.assertFalse(
            current_pr(
                {**pr, "head": {**pr["head"], "repo": {"full_name": "other/repo"}}},
                run,
                repo,
            )
        )

    def test_every_latest_required_check_must_pass(self) -> None:
        checks = [
            {"name": name, "id": index, "conclusion": "success"}
            for index, name in enumerate(
                ("gate", "agent-policy"), 1
            )
        ]
        self.assertTrue(latest_checks_pass(checks))
        self.assertFalse(latest_checks_pass(checks[:-1]))
        self.assertFalse(
            latest_checks_pass(
                checks + [{"name": "gate", "id": 9, "conclusion": "failure"}]
            )
        )

    def test_review_status_must_be_latest_and_clean_on_the_requested_commit(self) -> None:
        sha = "a" * 40
        statuses = [
            {
                "id": 1,
                "context": "codex-review",
                "state": "success",
                "url": f"https://api.github.com/repos/Asadtop4ik/task-manager/statuses/{sha}",
            },
            {
                "id": 2,
                "context": "codex-review",
                "state": "failure",
                "url": f"https://api.github.com/repos/Asadtop4ik/task-manager/statuses/{sha}",
            },
        ]
        self.assertFalse(clean_review_status(statuses))
        self.assertTrue(clean_review_status(statuses[:1]))

    def test_cancelled_agent_pr_cannot_auto_merge(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000007"
        url = "https://github.com/Asadtop4ik/task-manager/pull/7"
        pr = {
            "html_url": url,
            "head": {"ref": f"codex/task-7-{run_id}", "sha": "a" * 40},
        }
        self.assertEqual(agent_run_id(pr), run_id)
        self.assertEqual(
            agent_run_id({"head": {"ref": f"codex/fast/task-7-{run_id}"}}), run_id
        )
        ready = {
            "status": "pr_ready",
            "pr_url": url,
            "head_sha": "a" * 40,
            "ci_status": "success",
            "ci_verified_sha": "a" * 40,
            "review_status": "clean",
            "review_sha": "a" * 40,
        }
        self.assertTrue(agent_ready(pr, ready))
        self.assertTrue(agent_ready(pr, ready | {"review_status": "advisory"}))
        self.assertFalse(agent_ready(pr, ready | {"status": "cancelled"}))
        self.assertFalse(agent_ready(pr, ready | {"head_sha": "b" * 40}))
        self.assertEqual(
            agent_run_id({"head": {"ref": "codex/task-7-invalid"}}), "invalid"
        )


class DispatchPathTests(unittest.TestCase):
    def test_repository_dispatch_checks_the_dispatched_sha(self) -> None:
        sha = "a" * 40
        repo = "Asadtop4ik/task-manager"
        calls: list[str] = []

        def fake_github(path: str) -> object:
            calls.append(path)
            if path == f"repos/{repo}/pulls/5":
                return {
                    "state": "open",
                    "draft": False,
                    "mergeable_state": "clean",
                    "base": {"ref": "main"},
                    "head": {"sha": sha, "ref": "docs-fix", "repo": {"full_name": repo}},
                }
            if "/files" in path:
                return [{"filename": "README.md"}]
            if path.startswith(f"repos/{repo}/commits/{sha}/check-runs"):
                return {
                    "check_runs": [
                        {"id": 1, "name": "gate", "conclusion": "success"},
                        {"id": 2, "name": "agent-policy", "conclusion": "success"},
                    ]
                }
            if path == f"repos/{repo}/commits/{sha}/statuses":
                return [{"id": 1, "context": "codex-review", "state": "success"}]
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(
                json.dumps({"client_payload": {"pull_number": 5, "head_sha": sha}})
            )
            output = Path(tmp) / "output"
            env = {
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REPOSITORY": repo,
                "GITHUB_OUTPUT": str(output),
            }
            with (
                patch.dict(os.environ, env),
                patch.object(auto_merge, "_github", fake_github),
            ):
                auto_merge.main()
            self.assertIn(f"eligible=true\nnumber=5\nsha={sha}", output.read_text())
        self.assertTrue(any("check-runs" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
