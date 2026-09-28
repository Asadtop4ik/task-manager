from __future__ import annotations

import base64
import json
import stat
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from agent_svc.api import Work
from agent_svc.codex import CodexChildError, CodexResult
from agent_svc.http import HttpError
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


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _FakeImageResponse:
    """A minimal stand-in for `http.client.HTTPResponse`: a context manager
    with `.headers.get(...)` and one-shot `.read(n)`, exactly what
    `agent_images.download_images` needs from `urllib.request.urlopen`."""

    def __init__(self, *, content_type: str, body: bytes) -> None:
        self.headers = {"Content-Type": content_type}
        self._body = body
        self._read = False

    def __enter__(self) -> _FakeImageResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


def _fake_urlopen_for_one_image(run_id: str, image_bytes: bytes):
    listing_suffix = f"/agent-runs/{run_id}/images"
    image_suffix = f"/agent-runs/{run_id}/images/1"

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeImageResponse:
        url = request.full_url  # type: ignore[attr-defined]
        if url.endswith(listing_suffix):
            body = json.dumps(
                [{"id": 1, "mime": "image/png", "size": len(image_bytes)}]
            ).encode()
            return _FakeImageResponse(content_type="application/json", body=body)
        if url.endswith(image_suffix):
            return _FakeImageResponse(content_type="image/png", body=image_bytes)
        raise AssertionError(f"unexpected image URL in test: {url}")

    return fake_urlopen


class HandleImplementHappyPathTests(unittest.TestCase):
    def test_private_repo_happy_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)

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

            # Journal entry removed, codex workspace cleaned up (once on
            # enter, once on exit).
            self.assertIsNone(ctx.journal.read(RUN_ID))
            self.assertEqual(len(ctx.codex.cleanup_calls), 2)  # type: ignore[attr-defined]

    def test_public_repo_happy_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)

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

    def test_agent_qa_uses_the_public_validator_path_like_legacy(self) -> None:
        # agent-qa is `private=True` in the catalog (its own GitHub token is
        # private), but legacy `public_agent_task.approved_repositories`
        # treats it as PUBLIC for validator/prompt purposes when QA is
        # enabled -- the same `AGENTS.md`/`.codex/` path blocks the 3
        # genuinely public repos get. Proven here by a patch that touches
        # `AGENTS.md`, which the PRIVATE `agent_task.check_diff` would allow
        # (it is not a credential path) but the PUBLIC validator refuses.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)

            work = _work(
                repo_full_name="Asadtop4ik/agent-qa",
                base_branch="main",
                task_revision="a" * 64,
            )
            patch_bytes = make_patch(remote, base_sha, {"AGENTS.md": "malicious rewrite\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": _b64(patch_bytes),
                    "changed_paths": ["AGENTS.md"],
                    "bytes": len(patch_bytes),
                }
            )

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertIn("protected path", payload["error"])

    def test_downloaded_images_are_readable_by_agent_codex_and_reach_exec(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)

            work = _work(image_count=1)
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {
                    "patch_b64": _b64(patch_bytes),
                    "changed_paths": ["NOTES.md"],
                    "bytes": len(patch_bytes),
                }
            )

            png_bytes = b"\x89PNG\r\n\x1a\nfake-png-data"
            captured: dict[str, list] = {}
            original_run_exec = ctx.codex.run_exec  # type: ignore[attr-defined]

            def spying_run_exec(request, **kwargs):
                images = [Path(p) for p in request.get("images", [])]
                captured["images"] = images
                captured["modes"] = [
                    (
                        stat.S_IMODE(path.stat().st_mode),
                        stat.S_IMODE(path.parent.stat().st_mode),
                    )
                    for path in images
                ]
                return original_run_exec(request, **kwargs)

            ctx.codex.run_exec = spying_run_exec  # type: ignore[method-assign,attr-defined]

            with patch(
                "urllib.request.urlopen",
                side_effect=_fake_urlopen_for_one_image(RUN_ID, png_bytes),
            ):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(len(captured["images"]), 1)
            image_path = captured["images"][0]
            self.assertEqual(image_path.name, "image-1.png")
            self.assertIn("agent-images", image_path.parts)
            file_mode, dir_mode = captured["modes"][0]
            self.assertEqual(file_mode, 0o640)
            self.assertEqual(dir_mode, 0o2750)
            # Downloaded attachments are removed once the run finishes.
            images_dir = Path(ctx.settings.work_root) / RUN_ID / "images"
            self.assertFalse(images_dir.exists())

    def test_is_public_repo_treats_agent_qa_as_public(self) -> None:
        from agent_svc.implement import is_public_repo

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            self.assertTrue(is_public_repo(ctx, "Asadtop4ik/agent-qa"))
            self.assertTrue(is_public_repo(ctx, "muradjanov-dev/ketoshop"))
            self.assertFalse(is_public_repo(ctx, "Asadtop4ik/task-manager"))
            self.assertFalse(is_public_repo(ctx, "unknown/repo"))


class HandleImplementFailurePathTests(unittest.TestCase):
    def test_preflight_failure_reports_failed_with_no_push(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            ctx.codex.queue_preflight_result(  # type: ignore[attr-defined]
                CodexChildError(
                    3, "trusted preflight failed", body={"preflight_failure": "boom"}
                )
            )

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
            self.assertIn("boom", payload["error"])
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
                "head": {
                    "sha": head_sha,
                    "ref": branch,
                    "repo": {"full_name": "Asadtop4ik/task-manager"},
                },
                "base": {"ref": "main"},
            }

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(
                payload["pr_url"], "https://github.com/Asadtop4ik/task-manager/pull/55"
            )
            self.assertEqual(payload["head_sha"], head_sha)
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]

    def test_opens_the_pr_when_the_push_succeeded_but_no_pr_was_ever_created(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            branch = "codex/task-7-" + RUN_ID

            # The branch was pushed by this exact run (matching trailer),
            # but agent-svc crashed (or was killed) before opening the PR.
            push_run_commit(remote, branch, RUN_ID)
            head_sha = rev_parse_or_none(remote, branch)
            assert head_sha is not None
            # No entry in ctx.github.open_pull_by_head: no open PR exists yet.

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(payload["head_sha"], head_sha)
            created = ctx.github.created_pulls  # type: ignore[attr-defined]
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["head"]["ref"], branch)
            self.assertEqual(created[0]["base"]["ref"], "main")
            self.assertEqual(payload["pr_url"], created[0]["html_url"])
            self.assertEqual(len(ctx.codex.run_exec_calls), 0)  # type: ignore[attr-defined]

    def test_fails_when_the_found_pr_does_not_match_this_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote)
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            branch = "codex/task-7-" + RUN_ID

            push_run_commit(remote, branch, RUN_ID)
            head_sha = rev_parse_or_none(remote, branch)
            assert head_sha is not None
            # A PR was found for this head branch, but its recorded base
            # does not match this run's base branch -- must not be trusted.
            ctx.github.open_pull_by_head[("Asadtop4ik/task-manager", branch)] = {  # type: ignore[attr-defined]
                "number": 55,
                "html_url": "https://github.com/Asadtop4ik/task-manager/pull/55",
                "head": {
                    "sha": head_sha,
                    "ref": branch,
                    "repo": {"full_name": "Asadtop4ik/task-manager"},
                },
                "base": {"ref": "some-other-branch"},
            }

            handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertIn("branch already exists", payload["error"])
            self.assertEqual(ctx.github.created_pulls, [])  # type: ignore[attr-defined]

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


QURBOT_ALLOWLIST_DATA = {
    "version": 1,
    "projects": {
        "qurbot": {
            "repo_full_name": "muradjanov-dev/qurbot",
            "stack": "qurbot",
            "env_file": "/srv/stack/env/qurbot.env",
            "services": ["qurbot-web", "qurbot-worker"],
            "containers": [
                ["qurbot-web", "ghcr.io/muradjanov-dev/qurbot"],
                ["qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"],
            ],
            "ready_url": None,
            "keys": {
                "ADMIN_TG_IDS": {
                    "format": "json_int_list",
                    "ops": ["list_add", "list_remove"],
                    "item_re": "[1-9][0-9]{4,14}",
                    "max_items": 50,
                    "protected_items": [917456291],
                    "description": "Telegram admin IDs",
                }
            },
        }
    },
}

_OPS_MARKER = (
    'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS","op":"list_add",'
    '"value":"5339875840","reason":"owner asked to add a new admin"}]'
)
_OPS_MARKER_DENIED = (
    'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"FEATURE_TOGGLE","op":"replace",'
    '"value":"x","reason":"owner asked"}]'
)


def _qurbot_work(**overrides: object) -> Work:
    base: dict[str, object] = dict(
        repo_full_name="muradjanov-dev/qurbot", base_branch="master", task_revision="a" * 64
    )
    base.update(overrides)
    return _work(**base)


def _install_qurbot_allowlist(ctx):
    """Patch `implement._load_ops_allowlist` to return a real `Allowlist`
    parsed from `QURBOT_ALLOWLIST_DATA` against the real trusted catalog --
    `load_allowlist` itself always requires a root-owned file (per the
    spec), which no test process can satisfy, so tests substitute this
    private loader instead of writing a real file on disk."""
    allowlist = ctx.trusted.agent_ops_policy.parse_allowlist(
        QURBOT_ALLOWLIST_DATA, ctx.trusted.agent_repos.REPOSITORIES
    )
    return patch("agent_svc.implement._load_ops_allowlist", return_value=allowlist)


class OpsRequestsFlowTests(unittest.TestCase):
    def test_pr_body_never_contains_the_ops_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="Added the requested notes file.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            body = ctx.github.created_pulls[0]["body"]  # type: ignore[attr-defined]
            self.assertNotIn("AGENT_OPS_REQUESTS", body)
            self.assertNotIn("5339875840", body)
            self.assertIn("Added the requested notes file.", body)

    def test_patch_with_allowed_ops_reports_pr_opened_with_ops_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="Added the notes file.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(len(payload["ops_requests"]), 1)
            proposal = payload["ops_requests"][0]
            self.assertEqual(proposal["key"], "ADMIN_TG_IDS")
            self.assertEqual(proposal["policy"], "allowed")
            self.assertEqual(proposal["restart_services"], ["qurbot-web", "qurbot-worker"])

    def test_no_patch_with_allowed_ops_reports_ops_pending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="No code change needed.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "ops_pending")
            self.assertEqual(len(payload["ops_requests"]), 1)
            self.assertEqual(payload["ops_requests"][0]["policy"], "allowed")
            self.assertEqual(ctx.github.created_pulls, [])  # type: ignore[attr-defined]

    def test_no_patch_with_denied_ops_only_reports_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="No code change needed.\n" + _OPS_MARKER_DENIED)
            )
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertIn("agent produced no file changes", payload["error"])
            self.assertEqual(len(payload["ops_requests"]), 1)
            self.assertEqual(payload["ops_requests"][0]["policy"], "denied")
            self.assertEqual(payload["ops_requests"][0]["policy_reason"], "not_allowlisted")

    def test_missing_allowlist_means_no_prompt_rules_and_denied_no_allowlist(self) -> None:
        # No `_install_qurbot_allowlist` patch here: the real `load_allowlist`
        # runs against `settings.ops_allowlist_path`, which does not exist in
        # the test environment -- exactly the "missing file" production case.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="No code change needed.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            handle_implement(ctx, work, threading.Event())

            prompt = ctx.codex.run_exec_calls[0]["prompt"]  # type: ignore[attr-defined]
            self.assertNotIn("Ops requests:", prompt)
            self.assertNotIn("AGENT_OPS_REQUESTS", prompt)

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(len(payload["ops_requests"]), 1)
            self.assertEqual(payload["ops_requests"][0]["policy"], "denied")
            self.assertEqual(payload["ops_requests"][0]["policy_reason"], "no_allowlist")

    def test_prompt_gets_ops_rules_only_for_an_allowlisted_project(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            prompt = ctx.codex.run_exec_calls[0]["prompt"]  # type: ignore[attr-defined]
            self.assertIn("Ops requests:", prompt)
            self.assertIn("ADMIN_TG_IDS", prompt)
            self.assertIn("AGENT_OPS_REQUESTS:", prompt)
            # Never a value, current or example.
            self.assertNotIn("5339875840", prompt)
            self.assertNotIn("917456291", prompt)

    def test_ambiguous_trailer_sets_ops_note_even_with_zero_proposals(self) -> None:
        # P3-12 regression: a trailer was present (two marker lines) but
        # never trusted enough to parse -- the callback must still say so,
        # even though there is nothing in `ops_requests` to show.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(
                    final_message="No code change needed.\n" + _OPS_MARKER + "\n" + _OPS_MARKER
                )
            )
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["ops_requests"], [])
            self.assertEqual(payload["ops_note"], "ambiguous")

    def test_invalid_trailer_json_sets_ops_note_even_with_zero_proposals(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(
                    final_message="No code change needed.\nAGENT_OPS_REQUESTS: {not json"
                )
            )
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["ops_requests"], [])
            self.assertEqual(payload["ops_note"], "invalid_trailer")

    def test_missing_agent_ops_policy_module_never_crashes_the_run(self) -> None:
        # P1-2 regression: `ctx.trusted.agent_ops_policy` is accessed a
        # SECOND time after Codex has already run (validate/build_ops_note);
        # if that raises (e.g. an older installer without the trusted
        # module deployed yet), the run's own real outcome must still be
        # reported -- never lost to an uncaught exception.
        class _RaisingOnAgentOpsPolicy:
            def __init__(self, real_trusted: Any) -> None:
                self._real = real_trusted

            @property
            def agent_ops_policy(self) -> Any:
                raise FileNotFoundError("agent_ops_policy.py")

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="Added the notes file.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )
            # The prompt-time allowlist load still succeeds normally
            # (`_load_ops_allowlist` catches it too, but we want to isolate
            # the POST-Codex access specifically): only swap `ctx.trusted`
            # in AFTER the prompt has already been composed.
            original_run_exec = ctx.codex.run_exec  # type: ignore[attr-defined]

            def swap_trusted_then_run(request, **kwargs):
                object.__setattr__(ctx, "trusted", _RaisingOnAgentOpsPolicy(ctx.trusted))
                return original_run_exec(request, **kwargs)

            ctx.codex.run_exec = swap_trusted_then_run  # type: ignore[method-assign,attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(payload["ops_requests"], [])
            self.assertEqual(payload["ops_note"], "ops_unavailable")

    def test_repeated_value_in_prose_is_redacted_from_the_pr_body(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(
                    final_message=(
                        "I added 5339875840 as a new admin per the request.\n" + _OPS_MARKER
                    )
                )
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            body = ctx.github.created_pulls[0]["body"]  # type: ignore[attr-defined]
            self.assertNotIn("5339875840", body)
            self.assertIn("[ops qiymati]", body)

    def test_short_values_are_never_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            marker = (
                'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS","op":"list_add",'
                '"value":"11111","reason":"x"}]'
            )
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="The value 111 stays in this sentence.\n" + marker)
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            body = ctx.github.created_pulls[0]["body"]  # type: ignore[attr-defined]
            self.assertIn("111", body)
            self.assertNotIn("[ops qiymati]", body)

    def test_ops_callback_rejected_with_422_is_resent_without_ops_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            base_sha = make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _qurbot_work()
            patch_bytes = make_patch(remote, base_sha, {"NOTES.md": "hello\n"})
            ctx.codex.queue_exec_result(  # type: ignore[attr-defined]
                _exec_result(final_message="Added the notes file.\n" + _OPS_MARKER)
            )
            ctx.codex.queue_package_result(  # type: ignore[attr-defined]
                {"patch_b64": _b64(patch_bytes), "changed_paths": ["NOTES.md"]}
            )
            ctx.api.queue_callback_effects(HttpError(422, "ops_requests: value error"))  # type: ignore[attr-defined]

            with _install_qurbot_allowlist(ctx):
                handle_implement(ctx, work, threading.Event())

            # The retried, stripped payload made it through.
            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            payload = ctx.api.callbacks[0]  # type: ignore[attr-defined]
            self.assertEqual(payload["status"], "pr_opened")
            self.assertNotIn("ops_requests", payload)
            self.assertNotIn("ops_note", payload)

    def test_non_ops_callback_rejection_is_not_retried_here(self) -> None:
        # A 422 on a payload that never carried ops fields at all must be
        # left entirely to `run.deliver`'s own retry/backoff, unchanged.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            make_github_remote(remote, branch="master")
            ctx = build_test_context(root, github_remote=remote)
            work = _work()
            ctx.codex.queue_exec_result(_exec_result())  # type: ignore[attr-defined]
            ctx.codex.queue_package_result({"patch_b64": "", "changed_paths": []})  # type: ignore[attr-defined]
            ctx.api.queue_callback_effects(HttpError(422, "unrelated error"))  # type: ignore[attr-defined]

            handle_implement(ctx, work, threading.Event())

            # `run.deliver` retried the exact same payload and it succeeded
            # the second time (no ops fields involved at all).
            self.assertEqual(len(ctx.api.callbacks), 1)  # type: ignore[attr-defined]
            self.assertEqual(ctx.api.callbacks[0]["status"], "failed")  # type: ignore[attr-defined]


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
