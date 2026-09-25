import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from intake_worker import IntakeWorker


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=None):
        return json.dumps(self.payload).encode()


class DiscussionWorkerTests(unittest.TestCase):
    def test_owner_qa_discussion_uses_qa_snapshot_allowlist(self):
        lease = {
            "id": 28,
            "revision": 1,
            "lease_id": "lease-id",
            "repo_full_name": "Asadtop4ik/agent-qa",
            "base_branch": "main",
            "project_key": "agent-qa",
            "diagnostics_enabled": False,
            "thread_id": None,
            "text": "Summarize the README.",
            "images": [],
        }
        posted = []
        snapshots = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            if request.full_url.endswith("/result"):
                posted.append(json.loads(request.data))
                return Response({})
            raise AssertionError(request.full_url)

        def child(command, **kwargs):
            payload = json.loads(kwargs["input"])
            (Path(payload["session_dir"]) / "discussion-result.json").write_text(
                json.dumps({"thread_id": "thr_qa", "response": "README qisqacha."})
            )
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret",
                github_token="test-github-secret",
                temp_root=directory,
                opener=opener,
                command_runner=child,
            )

            def fetch_snapshot(target, repository, branch, *, approved_repositories):
                snapshots.append((repository, branch, approved_repositories))
                target.mkdir()

            with patch.object(worker, "_fetch_snapshot", side_effect=fetch_snapshot):
                self.assertEqual(worker.poll_discussion_once(), "answered")

        repository, branch, approved_repositories = snapshots[0]
        self.assertEqual((repository, branch), ("Asadtop4ik/agent-qa", "main"))
        self.assertEqual(approved_repositories[repository], branch)
        self.assertEqual(posted[0]["response"], "README qisqacha.")

    def test_unknown_private_repository_is_rejected_before_snapshot_or_codex(self):
        lease = {
            "id": 29,
            "revision": 1,
            "lease_id": "lease-id",
            "repo_full_name": "example/private-repo",
            "base_branch": "main",
            "project_key": "unknown",
            "diagnostics_enabled": False,
            "thread_id": None,
            "text": "Savol",
            "images": [],
        }
        posted = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            posted.append(json.loads(request.data))
            return Response({})

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret",
                github_token="test-github-secret",
                temp_root=directory,
                opener=opener,
                command_runner=lambda *_args, **_kwargs: self.fail(
                    "Codex was started for an unapproved repository"
                ),
            )
            with patch.object(
                worker, "_fetch_snapshot", side_effect=AssertionError("snapshot fetched")
            ):
                self.assertEqual(worker.poll_discussion_once(), "failed")
        self.assertIn("error", posted[0])

    def test_private_lease_uses_tokenless_child_and_posts_answer(self):
        lease = {
            "id": 8,
            "revision": 2,
            "lease_id": "lease-id",
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "main",
            "project_key": "task-manager",
            "diagnostics_enabled": False,
            "thread_id": "thr_previous",
            "text": "Buyurtma qanday?",
            "images": [],
        }
        posted = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            if request.full_url.endswith("/result"):
                posted.append(json.loads(request.data))
                return Response({})
            raise AssertionError(request.full_url)

        def child(command, **kwargs):
            payload = json.loads(kwargs["input"])
            self.assertEqual(payload["thread_id"], "thr_previous")
            self.assertIsNone(payload["diagnostics_discussion_id"])
            self.assertIsNone(payload["diagnostics_lease_id"])
            self.assertNotIn("test-worker-secret", kwargs["input"])
            self.assertEqual(command[-1], "codex-child")
            (Path(payload["session_dir"]) / "discussion-result.json").write_text(
                json.dumps({"thread_id": "thr_previous", "response": "Javob."})
            )
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret",
                github_token="test-github-secret",
                temp_root=directory,
                opener=opener,
                command_runner=child,
            )
            with patch.object(
                worker,
                "_fetch_snapshot",
                side_effect=lambda target, *_, **__: target.mkdir(),
            ):
                self.assertEqual(worker.poll_discussion_once(), "answered")
        self.assertEqual(posted[0]["response"], "Javob.")
        self.assertEqual(posted[0]["revision"], 2)

    def test_owner_ketoshop_turn_passes_only_discussion_scope_to_mcp_config(self):
        lease = {
            "id": 18,
            "revision": 4,
            "lease_id": "lease-id",
            "repo_full_name": "muradjanov-dev/ketoshop",
            "base_branch": "master",
            "project_key": "ketoshop",
            "diagnostics_enabled": True,
            "thread_id": None,
            "text": "Buyurtma holati",
            "images": [],
        }
        child_requests = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            if request.full_url.endswith("/result"):
                return Response({})
            raise AssertionError(request.full_url)

        def child(command, **kwargs):
            payload = json.loads(kwargs["input"])
            child_requests.append(payload)
            (Path(payload["session_dir"]) / "discussion-result.json").write_text(
                json.dumps({"thread_id": "thr_keto", "response": "Javob."})
            )
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret",
                github_token="test-github-secret",
                temp_root=directory,
                opener=opener,
                command_runner=child,
            )
            with patch.object(
                worker,
                "_fetch_snapshot",
                side_effect=lambda target, *_, **__: target.mkdir(),
            ):
                self.assertEqual(worker.poll_discussion_once(), "answered")
        self.assertEqual(child_requests[0]["diagnostics_discussion_id"], 18)
        self.assertEqual(child_requests[0]["diagnostics_lease_id"], "lease-id")
        self.assertIn("ketoshop_diagnostics MCP tools", child_requests[0]["prompt"])

    def test_wrong_branch_fails_without_starting_codex(self):
        lease = {
            "id": 8,
            "revision": 2,
            "lease_id": "lease-id",
            "repo_full_name": "Asadtop4ik/task-manager",
            "base_branch": "wrong",
            "project_key": "task-manager",
            "diagnostics_enabled": False,
            "thread_id": None,
            "text": "Savol",
            "images": [],
        }
        posted = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            posted.append(json.loads(request.data))
            return Response({})

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret",
                github_token="test-github-secret",
                temp_root=directory,
                opener=opener,
                command_runner=lambda *_args, **_kwargs: self.fail("Codex was started"),
            )
            self.assertEqual(worker.poll_discussion_once(), "failed")
        self.assertIn("error", posted[0])
