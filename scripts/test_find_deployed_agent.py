import unittest

from find_deployed_agent import find_run


class FindDeployedAgentTests(unittest.TestCase):
    def test_direct_fast_commit_trailer_identifies_run(self) -> None:
        run_id = "00000000-0000-0000-0000-000000000011"
        message = f"feat(agent): task 11\n\nAgent-Run-ID: {run_id}\n"
        self.assertEqual(find_run([], "a" * 40, "Asadtop4ik/task-manager", message), run_id)
        self.assertIsNone(find_run([], "a" * 40, "Asadtop4ik/task-manager", "no trailer"))
