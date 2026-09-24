import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from discussion_appserver import CODEX_BINARY, DiscussionError, run_turn


class FakeProcess:
    def __init__(self, messages):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(item) + "\n" for item in messages))
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=5):
        return 0


class AppServerTests(unittest.TestCase):
    def test_resumes_a_read_only_thread_and_returns_final_answer(self):
        process = FakeProcess([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thr_saved"}}},
            {"id": 3, "result": {"turn": {"id": "turn_1"}}},
            {"method": "item/completed", "params": {"item": {
                "type": "agentMessage", "phase": "final_answer", "text": "Javob tayyor."
            }}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ])
        with tempfile.TemporaryDirectory() as directory, patch(
            "discussion_appserver.subprocess.Popen", return_value=process
        ) as popen:
            thread, answer = run_turn(
                snapshot=Path(directory), thread_id="thr_saved", prompt="Savol", images=[]
            )
        self.assertEqual((thread, answer), ("thr_saved", "Javob tayyor."))
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual([item["method"] for item in sent], [
            "initialize", "initialized", "thread/resume", "turn/start"
        ])
        self.assertEqual(sent[-1]["params"]["sandboxPolicy"]["type"], "readOnly")
        self.assertEqual(popen.call_args.args[0][0], CODEX_BINARY)
        self.assertIn("node24/bin", popen.call_args.kwargs["env"]["PATH"])
        self.assertTrue(process.terminated)

    def test_approval_request_is_never_granted(self):
        process = FakeProcess([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "thr_new"}}},
            {"id": 3, "result": {"turn": {"id": "turn_1"}}},
            {"id": 77, "method": "item/commandExecution/requestApproval", "params": {}},
        ])
        with tempfile.TemporaryDirectory() as directory, patch(
            "discussion_appserver.subprocess.Popen", return_value=process
        ):
            with self.assertRaisesRegex(DiscussionError, "ruxsat"):
                run_turn(snapshot=Path(directory), thread_id=None, prompt="Savol", images=[])
