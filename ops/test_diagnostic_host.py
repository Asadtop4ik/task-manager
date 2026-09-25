import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from diagnostic_host import AuditLog, DiagnosticHost


class DiagnosticHostTests(unittest.TestCase):
    def test_audit_prunes_records_older_than_24h_and_caps_at_500(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            now = datetime.now(timezone.utc)
            records = [
                {"time": (now - timedelta(hours=25)).isoformat(), "marker": "old"}
            ]
            records.extend(
                {"time": now.isoformat(), "marker": str(index)} for index in range(500)
            )
            path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
            AuditLog(path).record(
                {
                    "discussion_id": 1,
                    "revision": 1,
                    "tool": "ketoshop_query",
                    "rows": 0,
                    "result_bytes": 0,
                    "duration_ms": 1,
                    "outcome": "ok",
                }
            )
            kept = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(kept), 500)
            self.assertNotIn("old", [row.get("marker") for row in kept])

    def test_calls_are_owner_context_checked_capped_and_audited_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = AuditLog(Path(directory) / "audit.jsonl")
            host = DiagnosticHost(
                intake_token="private-worker-token",
                database_url="postgresql://private-db-secret",
                api_base_url="https://api.test/api/v1",
                audit=audit,
            )
            context = Mock(return_value=(3, "ketoshop"))
            host._context = context
            host._orders = Mock(
                return_value=(
                    {"rows": [{"order_id": 71001, "total": 90000}], "row_count": 1},
                    "a" * 64,
                    1,
                )
            )
            request = {
                "discussion_id": 99,
                "tool": "ketoshop_query",
                "query": "SELECT order_id FROM ketoshop_diag_orders",
            }

            for _ in range(10):
                self.assertTrue(host.handle(request)["ok"])
            limited = host.handle(request)
            self.assertFalse(limited["ok"])
            self.assertIn("10 diagnostic calls", limited["error"])
            self.assertEqual(context.call_count, 11)
            self.assertEqual(host._orders.call_count, 10)

            records = [json.loads(line) for line in audit.path.read_text().splitlines()]
            self.assertEqual(len(records), 11)
            self.assertTrue(
                all(
                    set(record)
                    == {
                        "time",
                        "discussion_id",
                        "revision",
                        "tool",
                        "query_sha256",
                        "rows",
                        "result_bytes",
                        "duration_ms",
                        "outcome",
                    }
                    for record in records
                )
            )
            audit_text = audit.path.read_text()
            self.assertNotIn("SELECT", audit_text)
            self.assertNotIn("private-db-secret", audit_text)
            self.assertNotIn("private-worker-token", audit_text)
            self.assertNotIn("Synthetic Customer", audit_text)

    def test_recent_logs_use_fixed_container_and_redact_before_return(self):
        runner = Mock(
            return_value=SimpleNamespace(
                stdout=b"phone=+998901234567 user@example.com Bearer secret-token\n",
                returncode=0,
            )
        )
        host = DiagnosticHost(
            intake_token="private-worker-token",
            database_url="postgresql://private-db-secret",
            command_runner=runner,
            audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
        )
        result, _digest, count = host._logs()
        self.assertEqual(count, 1)
        self.assertNotIn("+998901234567", result["lines"][0])
        self.assertNotIn("user@example.com", result["lines"][0])
        self.assertNotIn("secret-token", result["lines"][0])
        self.assertEqual(
            runner.call_args.args[0],
            ["/usr/bin/docker", "logs", "--since", "24h", "--tail", "500", "ketoshop"],
        )


if __name__ == "__main__":
    unittest.main()
