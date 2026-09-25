import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

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
            context = Mock(return_value=(3, 41, 9, "ketoshop"))
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
                "lease_id": "private-lease-capability",
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
                        "actor_id",
                        "project_id",
                        "project_key",
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
            self.assertNotIn("private-lease-capability", audit_text)

    def test_wrong_discussion_capability_is_denied_before_any_tool_runs(self):
        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return self.payload

        def opener(request, timeout):
            lease = request.get_header("X-intake-lease-id")
            discussion_id = int(request.full_url.split("/")[-2])
            authorized = discussion_id == 99 and lease == "active-owner-lease"
            return Response(
                {
                    "authorized": authorized,
                    "active": authorized,
                    "project_key": "ketoshop" if authorized else "other",
                    "revision": 4 if authorized else 0,
                    "actor_id": 41 if authorized else 0,
                    "project_id": 9 if authorized else 8,
                }
            )

        host = DiagnosticHost(
            intake_token="worker-secret",
            database_url="postgresql://private-db",
            api_base_url="https://api.test/api/v1",
            opener=opener,
            audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
        )
        host._orders = Mock(return_value=({"rows": []}, "a" * 64, 0))
        request = {
            "discussion_id": 99,
            "lease_id": "guessed-capability",
            "tool": "ketoshop_query",
            "query": "SELECT order_id FROM ketoshop_diag_orders",
        }
        denied = host.handle(request)
        self.assertFalse(denied["ok"])
        host._orders.assert_not_called()

        request["discussion_id"] = 98
        request["lease_id"] = "active-owner-lease"
        cross_discussion = host.handle(request)
        self.assertFalse(cross_discussion["ok"])
        host._orders.assert_not_called()

        request["discussion_id"] = 99
        request["lease_id"] = "active-owner-lease"
        allowed = host.handle(request)
        self.assertTrue(allowed["ok"])
        host._orders.assert_called_once()

    def test_order_rows_mark_truncated_only_when_lookahead_found_more(self):
        host = DiagnosticHost(
            intake_token="worker-secret",
            database_url="postgresql://db-secret",
        )
        rows = [{"order_id": index} for index in range(200)]
        host._select = Mock(return_value=(rows, "hash", False))
        exact, _digest, _count = host._orders("SELECT * FROM ketoshop_diag_orders")
        self.assertFalse(exact["truncated"])
        host._select.return_value = (rows, "hash", True)
        more, _digest, _count = host._orders("SELECT * FROM ketoshop_diag_orders")
        self.assertTrue(more["truncated"])

    def test_recent_logs_use_fixed_container_and_redact_before_return(self):
        combined_stream = (
            b'{"time":"2026-09-25T09:00:00Z","level":"error",'
            b'"event":"db_timeout","message":"Name Jane Doe address Unit 9"}\n'
            b"plain text address Unit 4, House 9\n"
        )

        def spawn(command, **kwargs):
            code = (
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
                "sys.exit(int(sys.argv[2]))"
            )
            return subprocess.Popen(
                [sys.executable, "-c", code, combined_stream.hex(), "0"], **kwargs
            )

        runner = Mock(side_effect=spawn)
        host = DiagnosticHost(
            intake_token="private-worker-token",
            database_url="postgresql://private-db-secret",
            popen_factory=runner,
            audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
        )
        result, _digest, count = host._logs()
        self.assertEqual(count, 1)
        entry = result["entries"][0]
        self.assertEqual(entry["event"], "db_timeout")
        self.assertNotIn("message", entry)
        self.assertFalse(result["truncated"])
        self.assertEqual(runner.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(
            runner.call_args.args[0],
            ["/usr/bin/docker", "logs", "--since", "24h", "--tail", "500", "ketoshop"],
        )

    def test_logs_return_bounded_recent_metadata_and_cli_errors_fail_closed(self):
        body = b"".join(
            (
                b'{"time":"2026-09-25T09:00:00Z","level":"error",'
                b'"event":"database_timeout","message":"private full text"}'
                b"\n"
            )
            for _ in range(500)
        )
        exit_status = {"value": 0}

        def spawn(_command, **kwargs):
            code = (
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
                "sys.exit(int(sys.argv[2]))"
            )
            return subprocess.Popen(
                [sys.executable, "-c", code, body.hex(), str(exit_status["value"])],
                **kwargs,
            )

        runner = Mock(side_effect=spawn)
        host = DiagnosticHost(
            intake_token="worker-secret",
            database_url="postgresql://db-secret",
            popen_factory=runner,
            audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
        )
        result, _digest, count = host._logs()
        encoded = json.dumps(result, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 24 * 1024)
        self.assertLess(count, 500)
        self.assertTrue(result["truncated"])
        self.assertNotIn("private full text", encoded.decode())

        exit_status["value"] = 1
        from diagnostic_host import DiagnosticsError

        with self.assertRaisesRegex(DiagnosticsError, "logs are unavailable"):
            host._logs()

    def test_logs_stop_reading_after_the_raw_byte_budget(self):
        script = (
            "import sys\n"
            'line = b\'{"level":"error","event":"database_timeout"}\\n\'\n'
            "while True: sys.stdout.buffer.write(line)\n"
        )

        def spawn(_command, **kwargs):
            return subprocess.Popen([sys.executable, "-c", script], **kwargs)

        runner = Mock(side_effect=spawn)
        host = DiagnosticHost(
            intake_token="worker-secret",
            database_url="postgresql://db-secret",
            popen_factory=runner,
            audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
        )
        result, _digest, count = host._logs()
        encoded = json.dumps(result, separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), 24 * 1024)
        self.assertLessEqual(count, 500)
        self.assertTrue(result["truncated"])

    def test_finance_summary_aggregates_over_200_orders_with_numeric_money(self):
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params=None):
                executed.append((query, params))

            def fetchmany(self, limit):
                return [
                    {
                        "bucket": "2026-09-25",
                        "order_count": 205,
                        "revenue": Decimal("10250.00"),
                        "current_catalog_cost": Decimal("5125.00"),
                        "missing_cost_items": 7,
                        "expenses": Decimal("250.00"),
                        "status_breakdown": {"delivered": {"order_count": 205}},
                        "source_breakdown": {"bot": {"order_count": 205}},
                        "cost_basis": "current_catalog",
                        "historical_cost_data_available": False,
                    }
                ][:limit]

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return Cursor()

        fake_psycopg = ModuleType("psycopg")
        fake_psycopg.Error = RuntimeError
        fake_psycopg.connect = Mock(return_value=Connection())
        fake_rows = ModuleType("psycopg.rows")
        fake_rows.dict_row = object()
        image_runner = Mock(
            return_value=SimpleNamespace(
                stdout=(
                    b"ghcr.io/muradjanov-dev/ketoshop:"
                    + b"a" * 40
                    + b" sha256:"
                    + b"d" * 64
                ),
                returncode=0,
            )
        )
        with patch.dict(
            sys.modules, {"psycopg": fake_psycopg, "psycopg.rows": fake_rows}
        ):
            host = DiagnosticHost(
                intake_token="worker-secret",
                database_url="postgresql://db-secret",
                audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
                command_runner=image_runner,
            )
            result, digest, rows = host._finance_summary({"period": "day", "count": 1})
        self.assertEqual(rows, 1)
        self.assertEqual(result["buckets"][0]["order_count"], 205)
        self.assertEqual(result["buckets"][0]["revenue"], "10250.00")
        self.assertFalse(result["historical_cost_data_available"])
        self.assertEqual(result["cost_basis"], "current_catalog")
        self.assertEqual(result["source"], "ketoshop_postgresql_views")
        self.assertEqual(result["period"], "day")
        self.assertEqual(result["timezone"], "Asia/Tashkent")
        self.assertTrue(result["captured_at"].endswith("+00:00"))
        self.assertEqual(
            result["verified_version"],
            {
                "git_commit": "a" * 40,
                "image_digest": "sha256:" + "d" * 64,
                "verified": True,
            },
        )
        self.assertTrue(digest)
        self.assertTrue(any("SUM(o.revenue)" in query for query, _ in executed))


if __name__ == "__main__":
    unittest.main()
