import unittest
import subprocess
import tempfile
from pathlib import Path

from ci_changed import changed_paths, selected_jobs


class ChangedJobsTests(unittest.TestCase):
    def test_only_changed_service_runs(self) -> None:
        self.assertEqual(
            selected_jobs(["frontend/src/pages/Board.tsx"]),
            {"backend": False, "bot": False, "frontend": True},
        )
        self.assertEqual(
            selected_jobs(["backend/app/main.py", "bot/app/main.py"]),
            {"backend": True, "bot": True, "frontend": False},
        )

    def test_unknown_or_shared_changes_run_everything(self) -> None:
        full = {"backend": True, "bot": True, "frontend": True}
        self.assertEqual(selected_jobs(None), full)
        self.assertEqual(selected_jobs([]), full)
        self.assertEqual(selected_jobs([".github/workflows/ci.yml"]), full)
        self.assertEqual(selected_jobs(["scripts/ci_changed.py"]), full)

    def test_docs_do_not_start_service_jobs(self) -> None:
        self.assertEqual(
            selected_jobs(["README.md", "docs/USAGE.md"]),
            {"backend": False, "bot": False, "frontend": False},
        )

    def test_agentsvc_only_changes_rely_on_agent_policy(self) -> None:
        self.assertEqual(
            selected_jobs(["agentsvc/agent_svc/main.py", "agentsvc/tests/test_main.py"]),
            {"backend": False, "bot": False, "frontend": False},
        )
        self.assertEqual(
            selected_jobs(["agentsvc/agent_svc/main.py", "backend/app/main.py"]),
            {"backend": True, "bot": False, "frontend": False},
        )

    def test_rename_out_of_backend_still_runs_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init")
            git("config", "user.email", "ci@example.test")
            git("config", "user.name", "CI Test")
            (root / "backend").mkdir()
            (root / "backend" / "old.py").write_text("print('old')\n")
            git("add", ".")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", base)
            (root / "docs").mkdir()
            git("mv", "backend/old.py", "docs/old.py")
            git("commit", "-m", "move")
            head = git("rev-parse", "HEAD")

            paths = changed_paths({"before": base}, "push", head, cwd=directory)
            self.assertEqual(paths, ["backend/old.py", "docs/old.py"])
            self.assertTrue(selected_jobs(paths)["backend"])
            fast_paths = changed_paths(
                {"ref": "refs/heads/codex/fast/task-1-example", "before": head},
                "push",
                head,
                cwd=directory,
            )
            self.assertEqual(fast_paths, paths)


if __name__ == "__main__":
    unittest.main()
