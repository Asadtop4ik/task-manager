from __future__ import annotations

import base64
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.api import Work
from agent_svc.codex import CodexResult
from agent_svc.implement import handle_implement

from .support import (
    build_test_context,
    commit_log,
    make_github_remote,
    make_patch,
    push_run_commit,
    rev_parse_or_none,
)

RUN_ID = "11111111-1111-1111-1111-111111111111"


def _work(**overrides: object) -> Work:
    base = dict(
        run_id=RUN_ID,
        kind="implement",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=7,
        task_revision="rev",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        title="Add notes",
        description="Add a notes file to the repo.",
        image_count=0,
        complexity="simple",
        relevant_files=(),
        branch=None,
        pr_url=None,
        pr_number=None,
        head_sha=None,
        action_id=None,
        instruction=None,
        expected_head_sha=None,
    )
    base.update(overrides)
    return Work(**base)  # type: ignore[arg-type]


def _exec_result(**overrides: object) -> CodexResult:
    base = dict(
        exit_code=0,
        timed_out=False,
        cancelled=False,
        idle_killed=False,
        final_message="Added the requested notes file.",
        usage={"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20},
        stderr_tail=[],
        frame=None,
    )
    base.update(overrides)
    return CodexResult(**base)  # type: ignore[arg-type]


def _stub_preflight(ctx, text: str = "stub preflight ok") -> None:
    ctx.trusted.agent_preflight.run = lambda repo, root, *, tools=None: text  # type: ignore[assignment]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class HandleImplementHappyPathTests(unittest.TestCase):
    def test_private_repo_happy_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)

            work = _work(relevant_files=["NOTES.md"])
            branch = "codex/task-7-" + RUN_ID
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": _b64(patch_bytes),
                    "changed_paths": ["NOTES.md"],
                    "bytes": len(patch_bytes),
                }
            )

            handle_implement(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(
                payload["pr_url"], "https://github.com/Asadtop4ik/task-manager/pull/100"
            )
            self.assertEqual(payload["input_tokens"], 100)
            self.assertEqual(payload["cached_input_tokens"], 10)
            self.assertEqual(payload["output_tokens"], 20)
            self.assertEqual(payload["head_sha"], rev_parse_or_none(remote, branch))

            log = commit_log(remote, f"refs/heads/{branch}")
            self.assertIn("Business AI Codex <codex@users.noreply.github.com>", log)
            self.assertIn("feat(agent): work on task 7", log)
            self.assertIn(f"Agent-Run-ID: {RUN_ID}", log)

            # Routing and the relevant-files hint reached the actual exec request.
            request = ctx.codex.run_exec_calls[0]  # type: ignore[attr-defined]
            self.assertEqual(request["model"], "gpt-6-luna")
            self.assertFalse(request["multi_agent"])
            self.assertIn("Start from these files:", request["prompt"])
            self.assertIn("- NOTES.md", request["prompt"])

            # Journal entry removed, codex workspace cleaned up.
            self.assertIsNone(ctx.journal.read(RUN_ID))
            self.assertEqual(len(ctx.codex.cleanup_calls), 1)  # type: ignore[attr-defined]

    def test_public_repo_happy_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)

            work = _work(
                repo_full_name="muradjanov-dev/ketoshop",
                base_branch="master",
                task_revision="a" * 64,
            )
            branch = "codex/task-7-" + RUN_ID
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": _b64(patch_bytes),
                    "changed_paths": ["NOTES.md"],
                    "bytes": len(patch_bytes),
                }
            )

            handle_implement(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            created = ctx.github.created_pulls[0]  # type: ignore[attr-defined]
            self.assertEqual(created["title"], "Task #7: Codex change")
            self.assertIn("in muradjanov-dev/ketoshop", created["body"])
            self.assertIsNotNone(rev_parse_or_none(remote, branch))


class HandleImplementFailurePathTests(unittest.TestCase):
    def test_preflight_failure_reports_failed_with_no_push(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)

            def boom(repo, root_dir, *, tools=None):
                raise RuntimeError("boom")

            ctx.trusted.agent_preflight.run = boom  # type: ignore[assignment]

            work = _work()
            branch = "codex/task-7-" + RUN_ID
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": _b64(patch_bytes),
                    "changed_paths": ["NOTES.md"],
                    "bytes": len(patch_bytes),
                }
            )

            handle_implement(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failure_phase"], "publish")
            self.assertIn("ishonchli tekshiruv xato berdi", payload["error"])
            self.assertIsNone(rev_parse_or_none(remote, branch))

    def test_empty_patch_reports_no_file_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            ctx.codex.queue_exec_result(_exec_result(final_message="Nothing to change."))  # type: ignore[attr-defined]
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failure_phase"], "implement")
            self.assertIn("agent produced no file changes", payload["error"])
            self.assertIn("Codex izohi", payload["error"])

    def test_child_refusal_during_packaging_reports_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.package_error = RuntimeError("codex child refused: symlink in patch")  # type: ignore[attr-defined]

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failure_phase"], "implement")
            self.assertIn("symlink in patch", payload["error"])

    def test_codex_timeout_reports_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(exit_code=-1, timed_out=True, final_message="")
            )

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failure_phase"], "implement")

    def test_fast_mode_is_never_handled_here(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work(mode="fast")

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]


class BranchExistsRecoveryTests(unittest.TestCase):
    def test_recovers_pr_opened_when_trailer_matches(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            branch = "codex/task-7-" + RUN_ID

            # A branch already exists on the remote, with a commit that
            # carries this exact run's trailer.
            push_run_commit(remote, branch, RUN_ID)
            head_sha = rev_parse_or_none(remote, branch)
            assert head_sha is not None
            ctx.github.open_pull_by_head[("Asadtop4ik/task-manager", branch)] = {  # type: ignore[attr-defined]
                "number": 55,
                "html_url": "https://github.com/Asadtop4ik/task-manager/pull/55",
                "head": {"sha": head_sha},
            }

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(
                payload["pr_url"], "https://github.com/Asadtop4ik/task-manager/pull/55"
            )
            self.assertEqual(payload["head_sha"], head_sha)
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]

    def test_fails_when_no_matching_trailer_is_found(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            branch = "codex/task-7-" + RUN_ID
            # A branch exists, but with a commit for a DIFFERENT run.
            push_run_commit(remote, branch, "99999999-9999-9999-9999-999999999999")
            head_sha = rev_parse_or_none(remote, branch)
            assert head_sha is not None
            ctx.github.open_pull_by_head[("Asadtop4ik/task-manager", branch)] = {  # type: ignore[attr-defined]
                "number": 55,
                "html_url": "https://github.com/Asadtop4ik/task-manager/pull/55",
                "head": {"sha": head_sha},
            }

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertIn("branch already exists", payload["error"])


class HeartbeatCancellationTests(unittest.TestCase):
    def test_a_lost_lease_sends_no_callback(self) -> None:
        """`RunScaffold`'s heartbeat setting `cancel` (proven directly in
        `test_runctx.py`) must make the handler stop without any callback:
        this drives that same `cancel` Event pre-set, exactly as it would be
        the instant a real heartbeat thread discovers `LeaseLost`."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            cancel = threading.Event()
            cancel.set()
            handle_implement(ctx, work, cancel)

            self.assertEqual(ctx.api.callbacks, [])  # type: ignore[attr-defined]
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
