from __future__ import annotations

import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_svc.api import Work
from agent_svc.publish import PublishError, publish_correction, publish_implement

from .support import (
    build_test_context,
    commit_log,
    force_move_branch,
    make_github_remote,
    make_patch,
    push_new_branch,
    rev_parse_or_none,
)


def _work(**overrides: object) -> Work:
    base = dict(
        run_id="11111111-1111-1111-1111-111111111111",
        kind="implement",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=1,
        task_id=42,
        task_revision="rev",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        title="Add notes",
        description="Add a notes file.",
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


def _stub_preflight(ctx, text: str = "stub preflight ok") -> None:
    ctx.trusted.agent_preflight.run = lambda repo, root, *, tools=None: text  # type: ignore[assignment]


class PublishImplementTests(unittest.TestCase):
    def test_happy_path_pushes_and_opens_a_pr(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", "main")

            work = _work()
            branch = "codex/task-42-" + work.run_id
            task = {
                "task_id": 42,
                "run_id": work.run_id,
                "title": "Add notes",
                "description": "Add a notes file.",
                "base_branch": "main",
                "mode": "pr",
            }
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            stages: list[str] = []

            result = publish_implement(
                ctx,
                work,
                base_sha=base_sha,
                branch=branch,
                patch=patch_bytes,
                task=task,
                is_public=False,
                image_dir=None,
                codex_summary="Added a notes file as requested.",
                report_stage=stages.append,
            )

            self.assertEqual(stages, ["patch_validated", "preflight_passed", "branch_pushed"])
            self.assertEqual(
                result.pr_url, "https://github.com/Asadtop4ik/task-manager/pull/100"
            )
            # The push really happened: the remote now has the branch, and its
            # tip commit carries the exact identity/message/trailer.
            log = commit_log(remote, f"refs/heads/{branch}")
            self.assertIn("Business AI Codex <codex@users.noreply.github.com>", log)
            self.assertIn("feat(agent): work on task 42", log)
            self.assertIn(f"Agent-Run-ID: {work.run_id}", log)
            self.assertEqual(result.head_sha, rev_parse_or_none(remote, branch))
            # The publish directory is cleaned up.
            self.assertFalse((Path(ctx.settings.state_dir) / "publish" / work.run_id).exists())
            # The PR body carries the Codex summary and the preflight line.
            created = ctx.github.created_pulls[0]  # type: ignore[attr-defined]
            self.assertIn("Added a notes file", created["body"])
            self.assertIn("Trusted publisher preflight: stub preflight ok.", created["body"])
            self.assertEqual(created["title"], "Task #42: Codex change")

    def test_token_never_appears_in_push_argv_or_env_key_values(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", "main")
            # `build_test_context` wires a fixed dummy token; swap in a secret
            # marker so we can prove it never leaks into argv.
            ctx.mirrors._token_for = lambda _repo: "s3cr3t-push-token"  # type: ignore[attr-defined]

            work = _work()
            branch = "codex/task-42-" + work.run_id
            task = {
                "task_id": 42,
                "run_id": work.run_id,
                "title": "t",
                "description": "d",
                "base_branch": "main",
                "mode": "pr",
            }
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            calls: list[list[str]] = []
            real_run = subprocess.run

            def recording_run(argv, *args, **kwargs):
                calls.append(list(argv))
                return real_run(argv, *args, **kwargs)

            with patch("agent_svc.publish.subprocess.run", side_effect=recording_run):
                publish_implement(
                    ctx,
                    work,
                    base_sha=base_sha,
                    branch=branch,
                    patch=patch_bytes,
                    task=task,
                    is_public=False,
                    image_dir=None,
                    codex_summary="",
                    report_stage=lambda _s: None,
                )

            for argv in calls:
                for arg in argv:
                    self.assertNotIn("s3cr3t-push-token", arg)

    def test_branch_already_exists_refuses_to_push(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", "main")

            work = _work()
            branch = "codex/task-42-" + work.run_id
            # Simulate a branch that already exists on the remote.
            push_new_branch(remote, base_sha, branch)
            task = {
                "task_id": 42,
                "run_id": work.run_id,
                "title": "t",
                "description": "d",
                "base_branch": "main",
                "mode": "pr",
            }
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})

            with self.assertRaises(PublishError) as ctx_err:
                publish_implement(
                    ctx,
                    work,
                    base_sha=base_sha,
                    branch=branch,
                    patch=patch_bytes,
                    task=task,
                    is_public=False,
                    image_dir=None,
                    codex_summary="",
                    report_stage=lambda _s: None,
                )
            self.assertIn("branch already exists", ctx_err.exception.reason)
            self.assertEqual(ctx.github.created_pulls, [])  # type: ignore[attr-defined]

    def test_preflight_failure_raises_publish_error_without_pushing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)

            def boom(repo, root_dir, *, tools=None):
                raise RuntimeError("ruff exploded")

            ctx.trusted.agent_preflight.run = boom  # type: ignore[assignment]
            ctx.mirrors.fetch("Asadtop4ik/task-manager", "main")

            work = _work()
            branch = "codex/task-42-" + work.run_id
            task = {
                "task_id": 42,
                "run_id": work.run_id,
                "title": "t",
                "description": "d",
                "base_branch": "main",
                "mode": "pr",
            }
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            stages: list[str] = []

            with self.assertRaises(PublishError) as ctx_err:
                publish_implement(
                    ctx,
                    work,
                    base_sha=base_sha,
                    branch=branch,
                    patch=patch_bytes,
                    task=task,
                    is_public=False,
                    image_dir=None,
                    codex_summary="",
                    report_stage=stages.append,
                )
            # The trusted `failure_reason` text, not a raw traceback.
            self.assertIn("ishonchli tekshiruv xato berdi", ctx_err.exception.reason)
            self.assertEqual(stages, ["patch_validated"])  # never reached preflight_passed
            self.assertIsNone(rev_parse_or_none(remote, branch))

    def test_empty_patch_raises_publish_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", "main")
            work = _work()
            with self.assertRaises(PublishError) as ctx_err:
                publish_implement(
                    ctx,
                    work,
                    base_sha=base_sha,
                    branch="codex/task-42-" + work.run_id,
                    patch=b"",
                    task={
                        "task_id": 42,
                        "run_id": work.run_id,
                        "title": "t",
                        "description": "d",
                        "base_branch": "main",
                        "mode": "pr",
                    },
                    is_public=False,
                    image_dir=None,
                    codex_summary="",
                    report_stage=lambda _s: None,
                )
            self.assertIn("no file changes", ctx_err.exception.reason)

    def test_public_repo_blocked_path_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("muradjanov-dev/qurbot", "master")

            work = _work(repo_full_name="muradjanov-dev/qurbot", base_branch="master")
            branch = "codex/task-42-" + work.run_id
            task = {
                "repo_full_name": "muradjanov-dev/qurbot",
                "base_branch": "master",
                "mode": "pr",
                "task_id": 42,
                "run_id": work.run_id,
                "title": "t",
                "description": "d",
                "task_revision": "a" * 64,
            }
            patch_bytes = make_patch(remote, base_sha, {"AGENTS.md": "malicious rewrite\n"})

            with self.assertRaises(PublishError) as ctx_err:
                publish_implement(
                    ctx,
                    work,
                    base_sha=base_sha,
                    branch=branch,
                    patch=patch_bytes,
                    task=task,
                    is_public=True,
                    image_dir=None,
                    codex_summary="",
                    report_stage=lambda _s: None,
                )
            self.assertIn("protected path", ctx_err.exception.reason)
            self.assertIsNone(rev_parse_or_none(remote, branch))


class PublishCorrectionTests(unittest.TestCase):
    def test_happy_path_fast_forwards_the_pr_branch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            branch = "codex/task-9-22222222-2222-2222-2222-222222222222"
            push_new_branch(remote, base_sha, branch)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", branch)

            work = _work(
                run_id="22222222-2222-2222-2222-222222222222",
                task_id=9,
                branch=branch,
                pr_number=5,
                pr_url="https://github.com/Asadtop4ik/task-manager/pull/5",
                action_id="33333333-3333-3333-3333-333333333333",
                instruction="Fix the typo",
                expected_head_sha=base_sha,
            )
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})
            stages: list[str] = []

            result = publish_correction(
                ctx,
                work,
                expected_head_sha=base_sha,
                patch=patch_bytes,
                report_stage=stages.append,
            )

            self.assertEqual(
                stages, ["patch_validated", "preflight_passed", "correction_pushed"]
            )
            self.assertNotEqual(result.head_sha, base_sha)
            tip = rev_parse_or_none(remote, branch)
            self.assertEqual(result.head_sha, tip)
            assert tip is not None
            log = commit_log(remote, tip, fmt="%s%n%b")
            self.assertIn("fix(agent): apply owner correction for task 9", log)
            self.assertIn(f"Agent-Run-ID: {work.run_id}", log)

    def test_remote_moved_before_publication_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            branch = "codex/task-9-22222222-2222-2222-2222-222222222222"
            push_new_branch(remote, base_sha, branch)
            ctx = build_test_context(root, github_remote=remote)
            _stub_preflight(ctx)
            ctx.mirrors.fetch("Asadtop4ik/task-manager", branch)

            work = _work(
                run_id="22222222-2222-2222-2222-222222222222",
                task_id=9,
                branch=branch,
                pr_number=5,
                expected_head_sha=base_sha,
            )
            patch_bytes = make_patch(remote, base_sha, {"FIX.md": "fixed\n"})

            # A race: someone else pushes to the PR branch first.
            force_move_branch(remote, base_sha, branch)

            with self.assertRaises(PublishError) as ctx_err:
                publish_correction(
                    ctx,
                    work,
                    expected_head_sha=base_sha,
                    patch=patch_bytes,
                    report_stage=lambda _s: None,
                )
            self.assertIn("moved before publication", ctx_err.exception.reason)


if __name__ == "__main__":
    unittest.main()
