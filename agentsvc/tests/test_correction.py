from __future__ import annotations

import base64
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_svc.api import HeadNotSettled, Work
from agent_svc.codex import CodexResult
from agent_svc.correction import handle_correction

from .support import (
    build_test_context,
    force_move_branch,
    make_github_remote,
    make_patch,
    push_new_branch,
    rev_parse_or_none,
    run_git,
)

RUN_ID = "22222222-2222-2222-2222-222222222222"
TASK_ID = 9
BRANCH = f"codex/task-{TASK_ID}-{RUN_ID}"


def _work(**overrides: object) -> Work:
    base = dict(
        run_id=RUN_ID,
        kind="correction",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=TASK_ID,
        task_revision="rev",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        title="t",
        description="d",
        image_count=0,
        complexity="simple",
        relevant_files=(),
        branch=BRANCH,
        pr_url="https://github.com/Asadtop4ik/task-manager/pull/5",
        pr_number=5,
        head_sha=None,
        action_id="33333333-3333-3333-3333-333333333333",
        instruction="Fix the typo",
        expected_head_sha=None,
    )
    base.update(overrides)
    return Work(**base)  # type: ignore[arg-type]


def _open_pr(expected_head_sha: str, *, base_branch: str = "main") -> dict:
    return {
        "state": "open",
        "merged": False,
        "head": {
            "sha": expected_head_sha,
            "ref": BRANCH,
            "repo": {"full_name": "Asadtop4ik/task-manager"},
        },
        "base": {"ref": base_branch},
    }


def _exec_result(**overrides: object) -> CodexResult:
    base = dict(
        exit_code=0,
        timed_out=False,
        cancelled=False,
        idle_killed=False,
        final_message="Fixed the typo.",
        usage={"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 10},
        stderr_tail=[],
        frame=None,
    )
    base.update(overrides)
    return CodexResult(**base)  # type: ignore[arg-type]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class HandleCorrectionHappyPathTests(unittest.TestCase):
    def test_ff_only_happy_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]

            work = _work(expected_head_sha=base_sha)
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["FIX.md"]}
            )

            handle_correction(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.action_results), 1)  # type: ignore[attr-defined]
            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["action_id"], work.action_id)
            new_tip = rev_parse_or_none(remote, BRANCH)
            self.assertEqual(result["head_sha"], new_tip)
            self.assertNotEqual(new_tip, base_sha)

    def test_result_is_held_back_until_github_reports_the_pushed_head(self) -> None:
        """Production 2026-09-30: the PR API kept the old head for a few
        seconds after the push and the backend rejected the (correct) report."""
        from agent_svc import publish as publish_module

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]
            work = _work(expected_head_sha=base_sha)
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["FIX.md"]}
            )
            state = {"reads_after_push": 0}
            real_get_pull = ctx.github.get_pull  # type: ignore[attr-defined]
            reads_at_report: list[int] = []

            def lagging_get_pull(repo: str, number: int) -> dict:
                pr = real_get_pull(repo, number)
                tip = rev_parse_or_none(remote, BRANCH)
                if tip == base_sha:
                    return pr  # before the push: the normal stale-head check
                state["reads_after_push"] += 1
                if state["reads_after_push"] <= 3:
                    return pr  # GitHub still shows the old head
                return _open_pr(tip)  # type: ignore[arg-type]

            ctx.github.get_pull = lagging_get_pull  # type: ignore[attr-defined,method-assign]
            real_action_result = ctx.api.action_result  # type: ignore[attr-defined]

            def recording_action_result(run_id: str, lease_id: str, payload: dict) -> None:
                reads_at_report.append(state["reads_after_push"])
                real_action_result(run_id, lease_id, payload)

            ctx.api.action_result = recording_action_result  # type: ignore[attr-defined,method-assign]

            with patch.object(publish_module, "PR_HEAD_WAIT_BACKOFF_S", (0.0,) * 10):
                handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["head_sha"], rev_parse_or_none(remote, BRANCH))
            self.assertEqual(reads_at_report, [4])  # only after the 4th poll saw the new head

    def test_head_not_settled_409_is_retried_not_dropped(self) -> None:
        from agent_svc import runctx

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]
            work = _work(expected_head_sha=base_sha)
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["FIX.md"]}
            )
            ctx.api.queue_action_result_effects(  # type: ignore[attr-defined]
                HeadNotSettled("correction PR head changed before recording"),
                HeadNotSettled("correction PR head changed before recording"),
            )

            with patch.object(runctx, "HEAD_LAG_RETRY_BACKOFF_S", (0.0, 0.0, 0.0)):
                handle_correction(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.action_results), 1)  # type: ignore[attr-defined]
            self.assertEqual(ctx.api.action_results[0]["status"], "completed")  # type: ignore[attr-defined]
            self.assertIsNone(ctx.journal.read(work.run_id))

    def test_empty_correction_patch_keeps_current_head(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]

            work = _work(expected_head_sha=base_sha)
            ctx.codex.queue_exec_result(_exec_result(final_message="Nothing needed changing."))  # type: ignore[attr-defined]
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["head_sha"], base_sha)
            # No push happened; the remote branch tip is unchanged.
            self.assertEqual(rev_parse_or_none(remote, BRANCH), base_sha)


class HandleCorrectionRefusalTests(unittest.TestCase):
    def test_stale_head_is_refused_without_touching_codex(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            # The PR's real head has moved past what the lease still believes.
            moved_sha = _advance(remote, BRANCH)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(moved_sha)  # type: ignore[attr-defined]

            work = _work(expected_head_sha=base_sha)  # stale: base_sha, not moved_sha

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertIn("PR head changed", result["message"])
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]

    def test_unexpected_publish_exception_uses_the_legacy_text_with_redacted_detail(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]
            work = _work(expected_head_sha=base_sha)
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": base64.b64encode(patch_bytes).decode(),
                    "changed_paths": ["FIX.md"],
                }
            )
            secret_token = ctx.settings.github_agent_token

            with patch(
                "agent_svc.correction.publish_correction",
                side_effect=RuntimeError(f"boom near token {secret_token}"),
            ):
                handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(
                result["message"].startswith("Trusted correction publisher failed:")
            )
            self.assertNotIn(secret_token, result["message"])
            self.assertIn("REDACTED", result["message"])

    def test_missing_fields_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work(instruction=None)

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")

    def test_branch_not_matching_this_run_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work(branch="codex/task-999-not-this-run", expected_head_sha="a" * 40)

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertIn("does not match this run", result["message"])

    def test_codex_failure_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]
            work = _work(expected_head_sha=base_sha)
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(exit_code=1, final_message="I could not apply the fix.")
            )

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertIn("could not apply", result["message"])

    def test_ops_trailer_is_stripped_from_the_rejection_message(self) -> None:
        # Correction never acts on an ops-request trailer (v1) -- it must
        # still never let one reach the owner via `action_result`.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]
            work = _work(expected_head_sha=base_sha)
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(
                    exit_code=1,
                    final_message=(
                        "I could not apply the fix.\n"
                        'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS",'
                        '"op":"list_add","value":"5339875840","reason":"x"}]'
                    ),
                )
            )

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertIn("could not apply", result["message"])
            self.assertNotIn("AGENT_OPS_REQUESTS", result["message"])
            self.assertNotIn("5339875840", result["message"])

    def test_push_race_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            push_new_branch(remote, base_sha, BRANCH)
            ctx = build_test_context(root, github_remote=remote)
            ctx.github.pulls[("Asadtop4ik/task-manager", 5)] = _open_pr(base_sha)  # type: ignore[attr-defined]

            work = _work(expected_head_sha=base_sha)
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["FIX.md"]}
            )

            # After the mirror fetch/PR verification, but before publication,
            # someone else pushes to the branch first.
            real_fetch = ctx.mirrors.fetch

            def fetch_then_race(repo: str, branch: str) -> str:
                sha = real_fetch(repo, branch)
                force_move_branch(remote, base_sha, BRANCH)
                return sha

            ctx.mirrors.fetch = fetch_then_race  # type: ignore[method-assign]

            handle_correction(ctx, work, threading.Event())

            result = ctx.api.action_results[0]  # type: ignore[attr-defined]
            self.assertEqual(result["status"], "rejected")
            self.assertIn("moved before publication", result["message"])


def _advance(remote: Path, branch: str) -> str:
    with TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str) / "scratch"
        run_git(["git", "clone", "--quiet", "--branch", branch, str(remote), str(scratch)])
        (scratch / "ADVANCE.md").write_text("advanced\n", encoding="utf-8")
        run_git(["git", "add", "-A"], cwd=scratch)
        run_git(["git", "commit", "--quiet", "-m", "advance"], cwd=scratch)
        run_git(
            ["git", "push", "--quiet", str(remote), f"HEAD:refs/heads/{branch}"], cwd=scratch
        )
        return run_git(["git", "rev-parse", "HEAD"], cwd=scratch).stdout.strip()


if __name__ == "__main__":
    unittest.main()
