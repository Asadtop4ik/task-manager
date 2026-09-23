import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_task import usage


class UsageTests(unittest.TestCase):
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
