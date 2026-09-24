import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fast_publish import _report, _wait_for_ci


class FastPublishTests(unittest.TestCase):
    def test_deploy_completion_can_beat_publisher_callback(self) -> None:
        sha = "a" * 40
        task = {"run_id": "00000000-0000-0000-0000-000000000011"}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "123",
            }))
            stack.enter_context(patch("fast_publish.usage", return_value={}))
            stack.enter_context(patch("fast_publish._send_status", return_value={
                "status": "deployed", "deployed_sha": sha,
            }))
            _report(task, "deploying", sha)

    def test_failing_exact_commit_never_passes_validation(self) -> None:
        task = {"run_id": "00000000-0000-0000-0000-000000000011"}
        sha = "a" * 40
        run = {"name": "CI", "head_sha": sha, "status": "completed", "conclusion": "failure"}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"GITHUB_REPOSITORY": "Asadtop4ik/task-manager"}))
            stack.enter_context(patch("fast_publish._run_state", return_value={"status": "validating", "head_sha": sha}))
            stack.enter_context(patch("fast_publish._github", return_value={"workflow_runs": [run]}))
            with self.assertRaisesRegex(RuntimeError, "short CI failed"):
                _wait_for_ci(task, "codex/fast/task-11-example", sha)

    def test_successful_check_must_match_exact_head(self) -> None:
        task = {"run_id": "00000000-0000-0000-0000-000000000011"}
        sha = "a" * 40
        runs = [
            {"name": "CI", "head_sha": "b" * 40, "status": "completed", "conclusion": "success"},
            {"name": "CI", "head_sha": sha, "status": "completed", "conclusion": "success"},
        ]
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"GITHUB_REPOSITORY": "Asadtop4ik/task-manager"}))
            stack.enter_context(patch("fast_publish._run_state", return_value={"status": "validating", "head_sha": sha}))
            stack.enter_context(patch("fast_publish._github", return_value={"workflow_runs": runs}))
            _wait_for_ci(task, "codex/fast/task-11-example", sha)
