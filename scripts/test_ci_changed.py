import unittest

from ci_changed import selected_jobs


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


if __name__ == "__main__":
    unittest.main()
