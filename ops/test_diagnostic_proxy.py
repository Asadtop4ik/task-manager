import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostic_proxy import HOST_CALL_TIMEOUT_SECONDS, TOOLS, _tool_call


class DiagnosticProxyTests(unittest.TestCase):
    def test_exposes_only_fixed_tools_and_forwards_bound_discussion_id(self):
        self.assertGreaterEqual(HOST_CALL_TIMEOUT_SECONDS, 25)
        self.assertEqual(
            [tool["name"] for tool in TOOLS],
            [
                "ketoshop_query",
                "ketoshop_recent_logs",
                "ketoshop_finance_summary",
                "db_sql",
                "logs",
                "containers",
            ],
        )
        # db_sql can write, so it is the one tool that must not claim read-only.
        self.assertFalse(
            next(t for t in TOOLS if t["name"] == "db_sql")["annotations"]["readOnlyHint"]
        )
        for tool in (t for t in TOOLS if t["name"] != "db_sql"):
            with self.subTest(tool=tool["name"]):
                annotations = tool["annotations"]
                self.assertEqual(
                    set(annotations),
                    {
                        "title",
                        "readOnlyHint",
                        "destructiveHint",
                        "idempotentHint",
                        "openWorldHint",
                    },
                )
                self.assertIsInstance(annotations["title"], str)
                self.assertTrue(annotations["title"])
                self.assertTrue(annotations["readOnlyHint"])
                self.assertFalse(annotations["destructiveHint"])
                self.assertTrue(annotations["idempotentHint"])
                self.assertFalse(annotations["openWorldHint"])
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "diagnostics.sock")
            received = []
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            server.listen(1)

            def respond():
                connection, _ = server.accept()
                with connection:
                    payload = b""
                    while not payload.endswith(b"\n"):
                        payload += connection.recv(4096)
                    received.append(json.loads(payload))
                    connection.sendall(b'{"ok":true,"result":{"rows":[]}}\n')
                server.close()

            thread = threading.Thread(target=respond)
            thread.start()
            answer = _tool_call(
                socket_path,
                77,
                "owner-turn-lease",
                {
                    "name": "ketoshop_query",
                    "arguments": {"query": "SELECT order_id FROM ketoshop_diag_orders"},
                },
            )
            thread.join(timeout=2)
            self.assertTrue(answer["ok"])
            self.assertEqual(received[0]["discussion_id"], 77)
            self.assertEqual(received[0]["lease_id"], "owner-turn-lease")
            self.assertEqual(received[0]["tool"], "ketoshop_query")
            self.assertNotIn("intake_token", received[0])

    def test_new_tools_forward_validated_arguments_and_reject_bad_shapes(self):
        sent = []

        def fake_host(_socket, payload):
            sent.append(payload)
            return {"ok": True, "result": {}}

        with patch("diagnostic_proxy._request_host", fake_host):
            ok = _tool_call(
                "s",
                5,
                "lease",
                {"name": "db_sql", "arguments": {"sql": "SELECT 1", "write": False}},
            )
            self.assertTrue(ok["ok"])
            self.assertEqual(
                sent[0],
                {
                    "discussion_id": 5,
                    "lease_id": "lease",
                    "tool": "db_sql",
                    "arguments": {"sql": "SELECT 1", "write": False},
                },
            )
            for name, arguments in (
                ("db_sql", {}),
                ("db_sql", {"sql": ""}),
                ("db_sql", {"sql": "x" * 16_001}),
                ("db_sql", {"sql": "SELECT 1", "write": "true"}),
                ("db_sql", {"sql": "SELECT 1", "dsn": "postgresql://x"}),
                ("logs", {"tail": 2001}),
                ("logs", {"since_minutes": 1441}),
                ("logs", {"grep": ""}),
                ("logs", {"container": 3}),
                ("containers", {"container": "x"}),
            ):
                with self.subTest(name=name, arguments=arguments):
                    self.assertFalse(
                        _tool_call("s", 5, "lease", {"name": name, "arguments": arguments})["ok"]
                    )
            self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
