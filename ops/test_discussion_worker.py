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
    def test_private_lease_uses_tokenless_child_and_posts_answer(self):
        lease = {
            "id": 8, "revision": 2, "lease_id": "lease-id",
            "repo_full_name": "Asadtop4ik/task-manager", "base_branch": "main",
            "thread_id": "thr_previous", "text": "Buyurtma qanday?", "images": [],
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
            self.assertNotIn("test-worker-secret", kwargs["input"])
            self.assertEqual(command[-1], "codex-child")
            (Path(payload["session_dir"]) / "discussion-result.json").write_text(
                json.dumps({"thread_id": "thr_previous", "response": "Javob."})
            )
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret", github_token="test-github-secret",
                temp_root=directory, opener=opener, command_runner=child,
            )
            with patch.object(worker, "_fetch_snapshot", side_effect=lambda target, *_: target.mkdir()):
                self.assertEqual(worker.poll_discussion_once(), "answered")
        self.assertEqual(posted[0]["response"], "Javob.")
        self.assertEqual(posted[0]["revision"], 2)

    def test_wrong_branch_fails_without_starting_codex(self):
        lease = {
            "id": 8, "revision": 2, "lease_id": "lease-id",
            "repo_full_name": "Asadtop4ik/task-manager", "base_branch": "wrong",
            "thread_id": None, "text": "Savol", "images": [],
        }
        posted = []

        def opener(request, timeout):
            if request.full_url.endswith("/lease"):
                return Response(lease)
            posted.append(json.loads(request.data))
            return Response({})

        with tempfile.TemporaryDirectory() as directory:
            worker = IntakeWorker(
                intake_token="test-worker-secret", github_token="test-github-secret",
                temp_root=directory, opener=opener,
                command_runner=lambda *_args, **_kwargs: self.fail("Codex was started"),
            )
            self.assertEqual(worker.poll_discussion_once(), "failed")
        self.assertIn("error", posted[0])
