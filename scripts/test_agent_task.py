import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_task import callback, started, usage


class UsageTests(unittest.TestCase):
    def test_cancelled_run_stops_before_codex(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "github-output"
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "GITHUB_OUTPUT": str(output),
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "cancelled"}
            ):
                started()
            self.assertEqual(output.read_text(), "cancelled=true\n")

    def test_failed_run_carries_agent_question(self) -> None:
        task = {
            "task_id": 7,
            "run_id": "00000000-0000-0000-0000-000000000007",
            "title": "Fix menu",
            "description": "",
            "base_branch": "main",
        }
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "agent-result.txt").write_text(
                "Which menu label should I use?"
            )
            environment = {
                "TASK_JSON": json.dumps(task),
                "GITHUB_REPOSITORY": "Asadtop4ik/task-manager",
                "GITHUB_RUN_ID": "42",
                "JOB_STATUS": "failure",
                "RUNNER_TEMP": temp,
            }
            with patch.dict(os.environ, environment), patch(
                "agent_task._send_status", return_value={"status": "failed"}
            ) as sent:
                callback()
            self.assertEqual(
                sent.call_args.args[0]["error"], "Which menu label should I use?"
            )

    def test_extracts_last_completed_turn_and_ignores_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events = Path(temp) / "agent-events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "turn.started"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 6194,
                                    "cached_input_tokens": 4000,
                                    "output_tokens": 280,
                                },
                            }
                        ),
                        '{"type": "turn.completed",',
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"RUNNER_TEMP": temp}):
                self.assertEqual(
                    usage(),
                    {
                        "input_tokens": 6194,
                        "cached_input_tokens": 4000,
                        "output_tokens": 280,
                    },
                )


if __name__ == "__main__":
    unittest.main()
