import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from diagnostic_host import AuditLog, Context, DiagnosticHost, DiagnosticsError
from diagnostic_security import QueryError, redact_credentials, validate_single_statement

DSN_PASSWORD = "s3cretPassw0rd"
QURBOT_DSN = f"postgresql://qurbot:{DSN_PASSWORD}@127.0.0.1:5432/qurbot"


class FakePsycopg:
    """Just enough of psycopg to record what the broker sends."""

    class Error(Exception):
        pass

    def __init__(self, columns=None, rows=None, status="SELECT", fail=None):
        self.statements: list[str] = []
        self.events: list[str] = []
        self.dsns: list[str] = []
        self.columns = columns
        self.rows = rows or []
        self.status = status
        self.fail = fail
        outer = self

        class Cursor:
            def __init__(self):
                self.description = None
                self.statusmessage = None
                self.rowcount = -1
                self._rows = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query):
                outer.statements.append(query)
                if query.startswith("SET "):
                    self.statusmessage = "SET"
                    return
                if outer.fail:
                    raise outer.fail
                self.statusmessage = outer.status
                self.rowcount = len(outer.rows) if outer.columns else 1
                if outer.columns:
                    self.description = [SimpleNamespace(name=n) for n in outer.columns]
                    self._rows = list(outer.rows)

            def fetchmany(self, size):
                taken, self._rows = self._rows[:size], self._rows[size:]
                return taken

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return Cursor()

            def commit(self):
                outer.events.append("commit")

            def rollback(self):
                outer.events.append("rollback")

        self.Connection = Connection

    def connect(self, dsn, **_kwargs):
        self.dsns.append(dsn)
        return self.Connection()

    def module(self):
        module = ModuleType("psycopg")
        module.Error = self.Error
        module.connect = self.connect
        return module


def make_host(**overrides):
    values = dict(
        intake_token="worker-token-value",
        database_url="postgresql://legacy-view-role",
        project_dsns={"qurbot": QURBOT_DSN},
        audit=AuditLog(Path(tempfile.mkdtemp()) / "audit.jsonl"),
    )
    values.update(overrides)
    host = DiagnosticHost(**values)
    host._context = Mock(return_value=Context(2, 41, 9, "qurbot", True))
    return host


def db_request(sql, **arguments):
    return {
        "discussion_id": 7,
        "lease_id": "lease",
        "tool": "db_sql",
        "arguments": {"sql": sql, **arguments},
    }


def run_with(fake, host, request):
    with patch.dict(sys.modules, {"psycopg": fake.module()}):
        return host.handle(request)


class SingleStatementTests(unittest.TestCase):
    def test_accepts_one_statement_even_with_semicolons_in_literals_and_comments(self):
        for sql in (
            "SELECT 1",
            "SELECT 1;",
            "  select ';' as x ; -- trailing; comment",
            "SELECT 'it''s; fine'",
            'SELECT "a;b" FROM t',
            "SELECT $$a;b$$, $tag$;$tag$",
            "SELECT /* a; /* nested; */ b */ 1",
            "SELECT E'a\\';b'",
            "SELECT 1 -- ; DROP TABLE x",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(validate_single_statement(sql), sql.strip())

    def test_rejects_multiple_empty_and_transaction_control_statements(self):
        for sql in (
            "SELECT 1; SELECT 2",
            "SELECT 1; COMMIT",
            "SELECT 1;; DROP TABLE users",
            "SELECT 1; 'x'",
            "COMMIT",
            "rollback",
            "BEGIN",
            "",
            "   ;  ",
            "-- only a comment",
            "SELECT 'unterminated",
            "SELECT 1 /* open",
            "x" * 16_001,
        ):
            with self.subTest(sql=sql[:30]):
                with self.assertRaises(QueryError):
                    validate_single_statement(sql)


class DbSqlTests(unittest.TestCase):
    def test_project_mismatch_is_rejected_before_anything_runs(self):
        fake = FakePsycopg(columns=["x"], rows=[(1,)])
        host = make_host(project_dsns={"qurbot": QURBOT_DSN, "kans-shop": "postgresql://k"})
        for tool, arguments in (
            ("db_sql", {"sql": "SELECT 1", "project": "kans-shop"}),
            ("logs", {"project": "ketoshop"}),
            ("containers", {"project": "task-manager"}),
        ):
            with self.subTest(tool=tool):
                response = run_with(
                    fake,
                    host,
                    {
                        "discussion_id": 7,
                        "lease_id": "lease",
                        "tool": tool,
                        "arguments": arguments,
                    },
                )
                self.assertFalse(response["ok"])
                self.assertIn("project does not match", response["error"])
        self.assertEqual(fake.dsns, [])

    def test_project_defaults_to_the_discussion_project(self):
        fake = FakePsycopg(columns=["x"], rows=[(1,)])
        response = run_with(fake, make_host(), db_request("SELECT 1"))
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["project"], "qurbot")
        self.assertEqual(fake.dsns, [QURBOT_DSN])

    def test_multiple_statements_are_rejected_without_connecting(self):
        fake = FakePsycopg()
        response = run_with(fake, make_host(), db_request("SELECT 1; DELETE FROM users"))
        self.assertFalse(response["ok"])
        self.assertIn("one SQL statement", response["error"])
        self.assertEqual(fake.dsns, [])

    def test_read_mode_uses_a_read_only_transaction_and_rolls_back(self):
        fake = FakePsycopg(columns=["id"], rows=[(1,), (2,)])
        response = run_with(fake, make_host(), db_request("SELECT id FROM users"))
        self.assertTrue(response["ok"])
        self.assertEqual(fake.statements[0], "SET TRANSACTION READ ONLY")
        self.assertIn("SET LOCAL statement_timeout = '15000ms'", fake.statements)
        self.assertIn("SET LOCAL lock_timeout = '5000ms'", fake.statements)
        self.assertTrue(
            any(s.startswith("SET LOCAL idle_in_transaction_session_timeout") for s in fake.statements)
        )
        # SELECT 1 takes the snapshot that makes READ WRITE impossible to restore.
        self.assertEqual(fake.statements[-2:], ["SELECT 1", "SELECT id FROM users"])
        self.assertEqual(fake.events, ["rollback"])
        self.assertEqual(response["result"]["columns"], ["id"])
        self.assertEqual(response["result"]["rows"], [[1], [2]])

    def test_write_mode_commits_without_read_only_and_reports_rowcount(self):
        fake = FakePsycopg(status="UPDATE 1")
        response = run_with(
            fake,
            make_host(),
            db_request("UPDATE users SET active = false WHERE id = 3", write=True),
        )
        self.assertTrue(response["ok"], response)
        self.assertNotIn("SET TRANSACTION READ ONLY", fake.statements)
        self.assertEqual(fake.events, ["commit"])
        self.assertEqual(response["result"]["status"], "UPDATE 1")
        self.assertEqual(response["result"]["rowcount"], 1)
        self.assertTrue(response["result"]["write"])
        self.assertNotIn("rows", response["result"])

    def test_write_flag_must_be_a_boolean(self):
        fake = FakePsycopg()
        response = run_with(fake, make_host(), db_request("DELETE FROM t", write="yes"))
        self.assertFalse(response["ok"])
        self.assertEqual(fake.dsns, [])

    def test_sensitive_columns_are_masked(self):
        columns = ["id", "Password_Hash", "api_key", "otp_code", "salt", "footprint", "email"]
        fake = FakePsycopg(
            columns=columns,
            rows=[(1, "$2b$abc", "sk-live", "123456", "xyz", "keep", "a@b.uz")],
        )
        response = run_with(fake, make_host(), db_request("SELECT * FROM users"))
        row = dict(zip(columns, response["result"]["rows"][0], strict=True))
        for name in ("Password_Hash", "api_key", "otp_code", "salt"):
            self.assertEqual(row[name], "[masked]", name)
        self.assertEqual((row["id"], row["footprint"], row["email"]), (1, "keep", "a@b.uz"))
        self.assertEqual(
            response["result"]["masked_columns"],
            ["Password_Hash", "api_key", "otp_code", "salt"],
        )

    def test_row_cell_and_total_size_caps(self):
        fake = FakePsycopg(columns=["id", "blob"], rows=[(i, "x" * 5000) for i in range(250)])
        response = run_with(fake, make_host(), db_request("SELECT * FROM big"))
        result = response["result"]
        self.assertTrue(response["ok"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["returned_rows"], 200)
        self.assertTrue(result["rows"][0][1].startswith("x" * 2000))
        self.assertLess(len(result["rows"][0][1]), 2100)
        self.assertLessEqual(len(json.dumps(response, ensure_ascii=False).encode()), 64 * 1024)

        fake = FakePsycopg(columns=["id"], rows=[(i,) for i in range(250)])
        result = run_with(fake, make_host(), db_request("SELECT id FROM t"))["result"]
        self.assertEqual(result["returned_rows"], 200)
        self.assertTrue(result["truncated"])
        fake = FakePsycopg(columns=["id"], rows=[(i,) for i in range(200)])
        result = run_with(fake, make_host(), db_request("SELECT id FROM t"))["result"]
        self.assertFalse(result["truncated"])

    def test_database_errors_are_returned_without_the_dsn_password(self):
        fake = FakePsycopg(
            columns=["x"],
            fail=FakePsycopg.Error(f'bad connection {QURBOT_DSN} relation "x" does not exist'),
        )
        response = run_with(fake, make_host(), db_request("SELECT * FROM x"))
        self.assertFalse(response["ok"])
        self.assertIn("does not exist", response["error"])
        self.assertNotIn(DSN_PASSWORD, response["error"])

    def test_project_without_a_dsn_gets_a_clear_error_not_a_crash(self):
        fake = FakePsycopg()
        host = make_host(project_dsns={})
        response = run_with(fake, host, db_request("SELECT 1"))
        self.assertFalse(response["ok"])
        self.assertIn("no database is configured for project qurbot", response["error"])

    def test_sqlalchemy_driver_suffix_is_stripped_from_the_dsn(self):
        fake = FakePsycopg(columns=["x"], rows=[(1,)])
        host = make_host(
            project_dsns={"qurbot": "postgresql+asyncpg://u:pw123456@127.0.0.1:5432/qurbot"}
        )
        run_with(fake, host, db_request("SELECT 1"))
        self.assertEqual(fake.dsns, ["postgresql://u:pw123456@127.0.0.1:5432/qurbot"])

    def test_full_access_requires_the_backend_to_grant_it(self):
        fake = FakePsycopg(columns=["x"], rows=[(1,)])
        host = make_host()
        host._context = Mock(return_value=Context(2, 41, 9, "qurbot", False))
        response = run_with(fake, host, db_request("SELECT 1"))
        self.assertFalse(response["ok"])
        self.assertIn("not enabled", response["error"])
        self.assertEqual(fake.dsns, [])

    def test_backend_context_must_say_owner_initiated(self):
        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return self.payload

        base = {
            "authorized": True,
            "active": True,
            "owner_initiated": True,
            "full_access": True,
            "project_key": "qurbot",
            "revision": 1,
            "actor_id": 4,
            "project_id": 5,
        }
        host = make_host()
        del host._context
        for override, expected in (
            ({}, True),
            ({"owner_initiated": False}, False),
            ({"owner_initiated": None}, False),
            ({"project_key": "unknown-project"}, False),
        ):
            with self.subTest(override=override):
                payload = {**base, **override}
                if override == {"owner_initiated": None}:
                    del payload["owner_initiated"]
                host._opener = lambda *_a, _p=payload, **_k: Response(_p)
                if expected:
                    self.assertEqual(host._context(7, "lease").project_key, "qurbot")
                else:
                    with self.assertRaises(DiagnosticsError):
                        host._context(7, "lease")

    def test_audit_records_full_sql_flags_and_never_rows(self):
        fake = FakePsycopg(columns=["name"], rows=[("Customer Zarina",)])
        host = make_host()
        sql = "SELECT name FROM customers WHERE phone = '+998901234567'"
        run_with(fake, host, db_request(sql))
        run_with(fake, host, db_request("DELETE FROM customers WHERE id = 1", write=True))
        run_with(fake, host, db_request("SELECT 1; SELECT 2"))
        text = host._audit.path.read_text()
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([r["tool"] for r in records], ["db_sql"] * 3)
        self.assertEqual(records[0]["sql"], sql)
        self.assertFalse(records[0]["write"])
        self.assertEqual(records[0]["rows"], 1)
        self.assertEqual(records[0]["outcome"], "ok")
        self.assertEqual(records[0]["project_key"], "qurbot")
        self.assertEqual(records[0]["actor_id"], 41)
        self.assertTrue(records[1]["write"])
        self.assertEqual(records[1]["sql"], "DELETE FROM customers WHERE id = 1")
        self.assertEqual(records[2]["outcome"], "error")
        self.assertIn("one SQL statement", records[2]["error"])
        self.assertEqual(records[2]["sql"], "SELECT 1; SELECT 2")
        self.assertNotIn("Zarina", text)
        self.assertNotIn(DSN_PASSWORD, text)
        self.assertNotIn("worker-token-value", text)


class LogsTests(unittest.TestCase):
    def _host(self, body: bytes, status: int = 0):
        captured = {}

        def spawn(command, **kwargs):
            captured["command"] = command
            code = (
                "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
                "sys.exit(int(sys.argv[2]))"
            )
            return subprocess.Popen(
                [sys.executable, "-c", code, body.hex(), str(status)], **kwargs
            )

        host = make_host(popen_factory=spawn)
        return host, captured

    def _logs(self, host, **arguments):
        return host.handle(
            {"discussion_id": 7, "lease_id": "lease", "tool": "logs", "arguments": arguments}
        )

    def test_full_text_lines_are_returned_with_default_container_and_window(self):
        body = b"2026-10-01 plain text line without json\n{\"level\":\"error\",\"m\":\"boom\"}\n"
        host, captured = self._host(body)
        response = self._logs(host)
        self.assertTrue(response["ok"], response)
        self.assertEqual(
            captured["command"],
            ["/usr/bin/docker", "logs", "--since", "60m", "--tail", "300", "qurbot-web"],
        )
        text = response["result"]["text"]
        self.assertIn("plain text line without json", text)
        self.assertIn('"m":"boom"', text)
        self.assertEqual(response["result"]["returned_lines"], 2)

    def test_credentials_are_redacted_but_other_text_is_kept(self):
        token = "123456789:" + "A" * 35
        body = (
            f"connect {QURBOT_DSN} failed\n"
            f"url postgresql+asyncpg://app:hunter22hunter@db/x ok\n"
            f"Authorization: Bearer eyJhbGciOi.abc-def_ghi\n"
            f"bot {token} started\n"
            f"password is {DSN_PASSWORD} again\n"
            "order 71001 customer Zarina paid\n"
        ).encode()
        host, _captured = self._host(body)
        text = self._logs(host)["result"]["text"]
        for leaked in (DSN_PASSWORD, "hunter22hunter", "eyJhbGciOi", token):
            self.assertNotIn(leaked, text)
        self.assertIn("Bearer [redacted]", text)
        self.assertIn("[redacted-token]", text)
        self.assertIn("order 71001 customer Zarina paid", text)

    def test_container_must_be_in_the_project_allowlist(self):
        host, captured = self._host(b"x\n")
        for container in ("ketoshop", "qurbot-web; rm -rf /", "../qurbot-web", "", 5):
            with self.subTest(container=container):
                response = self._logs(host, container=container)
                self.assertFalse(response["ok"])
                self.assertIn("container must be one of", response["error"])
        self.assertNotIn("command", captured)
        ok = self._logs(host, container="qurbot-worker", since_minutes=5, tail=7)
        self.assertTrue(ok["ok"])
        self.assertEqual(captured["command"][-1], "qurbot-worker")
        self.assertEqual(captured["command"][3:], ["5m", "--tail", "7", "qurbot-worker"])

    def test_argument_bounds(self):
        host, _captured = self._host(b"x\n")
        for arguments in (
            {"since_minutes": 0},
            {"since_minutes": 1441},
            {"since_minutes": True},
            {"tail": 2001},
            {"tail": 0},
            {"grep": ""},
            {"grep": "x" * 201},
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(self._logs(host, **arguments)["ok"])

    def test_grep_is_a_plain_substring_filter_applied_to_redacted_text(self):
        body = b"ok a.b\nerror one\nfine\nerror two\n" + f"leak {DSN_PASSWORD}\n".encode()
        host, captured = self._host(body)
        result = self._logs(host, grep="error", tail=1)["result"]
        self.assertEqual(result["text"], "error two")
        self.assertEqual(result["matched_lines"], 2)
        self.assertTrue(result["truncated"])
        self.assertIn("20000", captured["command"])
        self.assertEqual(self._logs(host, grep="a.b")["result"]["returned_lines"], 1)
        self.assertEqual(self._logs(host, grep=DSN_PASSWORD)["result"]["returned_lines"], 0)

    def test_output_is_capped_to_the_result_budget_keeping_newest_lines(self):
        body = b"".join(f"line {i:05d} ".encode() + b"y" * 1000 + b"\n" for i in range(400))
        host, _captured = self._host(body)
        response = self._logs(host, tail=400)
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["result"]["truncated"])
        self.assertIn("line 00399", response["result"]["text"])
        self.assertNotIn("line 00000", response["result"]["text"])
        self.assertLessEqual(len(json.dumps(response).encode()), 64 * 1024)

    def test_docker_failure_is_reported_not_swallowed(self):
        host, _captured = self._host(b"Error response from daemon: No such container: x\n", 1)
        response = self._logs(host)
        self.assertFalse(response["ok"])
        self.assertIn("No such container", response["error"])


class ContainersTests(unittest.TestCase):
    def test_reports_each_allowlisted_container_and_marks_missing_ones(self):
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=1,
                stdout=(
                    b"/qurbot-web\trunning\ttrue\thealthy\tghcr.io/x/qurbot:abc\t"
                    b"2026-10-01T00:00:00Z\t2\t0\n"
                ),
            )
        )
        host = make_host(command_runner=runner)
        response = host.handle(
            {"discussion_id": 7, "lease_id": "lease", "tool": "containers", "arguments": {}}
        )
        containers = response["result"]["containers"]
        self.assertEqual([c["name"] for c in containers], ["qurbot-web", "qurbot-worker"])
        self.assertEqual(containers[0]["health"], "healthy")
        self.assertTrue(containers[0]["running"])
        self.assertEqual(containers[0]["restart_count"], 2)
        self.assertEqual(containers[1], {"name": "qurbot-worker", "state": "missing"})
        command = runner.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/docker", "inspect"])
        self.assertEqual(command[-2:], ["qurbot-web", "qurbot-worker"])


class LegacyIsolationTests(unittest.TestCase):
    def test_ketoshop_tools_are_refused_in_other_projects(self):
        host = make_host()
        host._orders = Mock()
        response = host.handle(
            {
                "discussion_id": 7,
                "lease_id": "lease",
                "tool": "ketoshop_query",
                "query": "SELECT order_id FROM ketoshop_diag_orders",
            }
        )
        self.assertFalse(response["ok"])
        host._orders.assert_not_called()

    def test_redact_credentials_only_changes_credential_shapes(self):
        self.assertEqual(redact_credentials("nothing to see"), "nothing to see")
        self.assertEqual(
            redact_credentials("postgres://u:pw@h/db"), "postgres://u:[redacted]@h/db"
        )


if __name__ == "__main__":
    unittest.main()
