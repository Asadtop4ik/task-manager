import unittest

from find_deployed_agent import find_run

REPO = "Asadtop4ik/task-manager"
SHA = "a" * 40
RUN_ID = "00000000-0000-0000-0000-000000000011"


def pr(ref: str = f"codex/task-11-{RUN_ID}", **overrides: object) -> dict:
    row = {
        "head": {"ref": ref, "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
        "merged_at": "2026-01-01T00:00:00Z",
        "merge_commit_sha": SHA,
    }
    return row | overrides


class FindDeployedAgentTests(unittest.TestCase):
    def test_merged_agent_pr_identifies_run(self) -> None:
        self.assertEqual(find_run([pr()], SHA, REPO), RUN_ID)

    def test_commit_message_trailer_is_not_trusted(self) -> None:
        self.assertIsNone(find_run([], SHA, REPO))

    def test_fast_branch_unmerged_and_foreign_prs_are_ignored(self) -> None:
        self.assertIsNone(find_run([pr(f"codex/fast/task-11-{RUN_ID}")], SHA, REPO))
        self.assertIsNone(find_run([pr(merged_at=None)], SHA, REPO))
        self.assertIsNone(find_run([pr(merge_commit_sha="b" * 40)], SHA, REPO))
        self.assertIsNone(find_run([pr()], SHA, "someone/else"))
