import unittest

from auto_merge import allowed_files, current_pr, latest_checks_pass


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
                ("backend", "bot", "frontend", "agent-policy"), 1
            )
        ]
        self.assertTrue(latest_checks_pass(checks))
        self.assertFalse(latest_checks_pass(checks[:-1]))
        self.assertFalse(
            latest_checks_pass(
                checks + [{"name": "backend", "id": 9, "conclusion": "failure"}]
            )
        )


if __name__ == "__main__":
    unittest.main()
