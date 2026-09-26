import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_pr_review
from agent_pr_review import build_review_prompt, prepare


def _pr(branch: str, sha: str) -> dict:
    return {
        "state": "open",
        "merged": False,
        "html_url": "https://github.com/Asadtop4ik/task-manager/pull/5",
        "head": {
            "sha": sha,
            "ref": branch,
            "repo": {"full_name": "Asadtop4ik/task-manager"},
        },
    }


def _event(run_id: str, sha: str, branch: str) -> dict:
    return {
        "client_payload": {
            "pull_number": 5,
            "head_sha": sha,
            "repo_full_name": "Asadtop4ik/task-manager",
            "run_id": run_id,
            "branch": branch,
        }
    }


class PrepareLocalExecutorSkipTests(unittest.TestCase):
    """``prepare`` must not double-review a run the local executor already owns."""

    def _run_prepare(self, active_run: dict, *, diff_response: str = "diff --git a/x b/x\n+1\n"):
        run_id = "00000000-0000-0000-0000-000000000009"
        sha = "a" * 40
        branch = f"codex/task-9-{run_id}"
        pr = _pr(branch, sha)
        with tempfile.TemporaryDirectory() as temp:
            event_path = Path(temp) / "event.json"
            event_path.write_text(
                json.dumps(_event(run_id, sha, branch)), encoding="utf-8"
            )
            output_path = Path(temp) / "github-output"
            environment = {
                "GH_TOKEN": "token",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(output_path),
                "RUNNER_TEMP": temp,
            }

            def fake_github(path, *, token, accept="application/vnd.github+json"):
                if accept.endswith(".diff"):
                    return diff_response
                return pr

            with patch.dict(os.environ, environment, clear=True), patch(
                "agent_pr_review._github", side_effect=fake_github
            ) as github, patch(
                "agent_pr_review._task_api", return_value=active_run
            ) as task_api:
                prepare()
            output = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            prompt_written = (Path(temp) / "agent-review-prompt.txt").exists()
            return run_id, github, task_api, output, prompt_written

    def test_exits_cleanly_and_writes_skip_when_executor_is_local(self) -> None:
        active_run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "head_sha": "a" * 40,
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/5",
            "status": "pr_opened",
            "executor": "local",
        }
        run_id, github, task_api, output, prompt_written = self._run_prepare(active_run)
        # Only the PR-head recheck happened; the diff was never fetched, so no
        # Codex review minutes are spent reviewing a run the local service owns.
        self.assertEqual(github.call_count, 1)
        task_api.assert_called_once_with("GET", f"{run_id}/status")
        self.assertIn("skip=true", output)
        self.assertIn("pull_number=5", output)
        self.assertIn(f"run_id={run_id}", output)
        self.assertFalse(prompt_written)

    def test_behaves_as_today_when_the_executor_key_is_absent(self) -> None:
        active_run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "head_sha": "a" * 40,
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/5",
            "status": "pr_opened",
        }
        run_id, github, _task_api, output, prompt_written = self._run_prepare(active_run)
        self.assertEqual(github.call_count, 2)  # PR head recheck, then the diff
        self.assertNotIn("skip=true", output)
        self.assertIn(f"run_id={run_id}", output)
        self.assertTrue(prompt_written)

    def test_behaves_as_today_when_executor_is_explicitly_not_local(self) -> None:
        active_run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "head_sha": "a" * 40,
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/5",
            "status": "pr_opened",
            "executor": "github",
        }
        _run_id, github, _task_api, output, prompt_written = self._run_prepare(active_run)
        self.assertEqual(github.call_count, 2)
        self.assertNotIn("skip=true", output)
        self.assertTrue(prompt_written)

    def test_executor_local_still_fails_closed_on_a_branch_mismatch(self) -> None:
        # The skip is checked last -- after the branch-identity checks -- so
        # a stale/mismatched run can never be silently "skipped" instead of
        # rejected just because its status happens to say executor=local.
        run_id = "00000000-0000-0000-0000-000000000009"
        sha = "a" * 40
        actual_branch = f"codex/task-9-{run_id}"
        pr = _pr(actual_branch, sha)
        active_run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "head_sha": sha,
            "pr_url": pr["html_url"],
            "status": "pr_opened",
            "executor": "local",
        }
        with tempfile.TemporaryDirectory() as temp:
            event_path = Path(temp) / "event.json"
            # The dispatch event claims a different branch than the PR's
            # actual head ref.
            event = _event(run_id, sha, "codex/task-9-mismatched")
            event_path.write_text(json.dumps(event), encoding="utf-8")
            environment = {
                "GH_TOKEN": "token",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(Path(temp) / "github-output"),
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment, clear=True), patch(
                "agent_pr_review._github", return_value=pr
            ) as github, patch("agent_pr_review._task_api", return_value=active_run):
                with self.assertRaisesRegex(ValueError, "branch changed"):
                    prepare()
            self.assertEqual(github.call_count, 1)  # never reached the diff fetch
            self.assertFalse((Path(temp) / "github-output").exists())


class BuildReviewPromptTests(unittest.TestCase):
    def test_matches_the_prompt_prepare_writes_to_disk(self) -> None:
        active_run = {
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "head_sha": "a" * 40,
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/5",
            "status": "pr_opened",
        }
        run_id = "00000000-0000-0000-0000-000000000009"
        sha = "a" * 40
        branch = f"codex/task-9-{run_id}"
        pr = _pr(branch, sha)
        diff = "diff --git a/x b/x\n+1\n"
        with tempfile.TemporaryDirectory() as temp:
            event_path = Path(temp) / "event.json"
            event_path.write_text(
                json.dumps(_event(run_id, sha, branch)), encoding="utf-8"
            )
            environment = {
                "GH_TOKEN": "token",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(Path(temp) / "github-output"),
                "RUNNER_TEMP": temp,
            }

            def fake_github(path, *, token, accept="application/vnd.github+json"):
                if accept.endswith(".diff"):
                    return diff
                return pr

            with patch.dict(os.environ, environment, clear=True), patch(
                "agent_pr_review._github", side_effect=fake_github
            ), patch("agent_pr_review._task_api", return_value=active_run):
                prepare()
            written = (Path(temp) / "agent-review-prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(
            written, build_review_prompt("Asadtop4ik/task-manager", 5, sha, diff)
        )


class SetReviewStatusTargetUrlTests(unittest.TestCase):
    def test_omitting_target_url_uses_the_current_github_actions_run(self) -> None:
        environment = {
            "GH_TOKEN": "token",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
            "GITHUB_RUN_ID": "77",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "agent_pr_review.urllib.request.urlopen"
        ) as urlopen, patch("agent_pr_review.urllib.request.Request") as request:
            agent_pr_review._set_review_status(
                "Asadtop4ik/task-manager", "a" * 40, "success", "clean"
            )
        body = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(
            body["target_url"],
            "https://github.com/Asadtop4ik/task-manager/actions/runs/77",
        )
        urlopen.assert_called_once()

    def test_an_explicit_target_url_overrides_the_github_actions_default(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "token"}, clear=True), patch(
            "agent_pr_review.urllib.request.urlopen"
        ), patch("agent_pr_review.urllib.request.Request") as request:
            agent_pr_review._set_review_status(
                "Asadtop4ik/task-manager",
                "a" * 40,
                "success",
                "clean",
                target_url="https://agent-svc.local/runs/9",
            )
        body = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(body["target_url"], "https://agent-svc.local/runs/9")


if __name__ == "__main__":
    unittest.main()
