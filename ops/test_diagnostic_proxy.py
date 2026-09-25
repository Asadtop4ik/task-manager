import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from diagnostic_proxy import HOST_CALL_TIMEOUT_SECONDS, TOOLS, _tool_call


class DiagnosticProxyTests(unittest.TestCase):
    def test_exposes_only_fixed_read_tools_and_forwards_bound_discussion_id(self):
        self.assertGreaterEqual(HOST_CALL_TIMEOUT_SECONDS, 12)
        self.assertEqual(
            [tool["name"] for tool in TOOLS],
            [
                "ketoshop_query",
                "ketoshop_recent_logs",
                "ketoshop_finance_summary",
            ],
        )
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


if __name__ == "__main__":
    unittest.main()
