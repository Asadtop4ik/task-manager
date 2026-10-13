"""Privileged host-side broker for owner-only project diagnostics.

Holds every database DSN and runs SQL/docker on the agent's behalf; the agent
only ever sees tool results. Access is bound per call to an active
owner-initiated discussion lease that the backend re-verifies.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import re
import selectors
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from diagnostic_security import (
    MAX_RESULT_BYTES,
    MAX_ROWS,
    MAX_SQL_CHARS,
    RESULT_BODY_BUDGET,
    ParsedQuery,
    QueryError,
    pack_rows,
    parse_select,
    redact_credentials,
    scrub_error_values,
    statement_first_word,
    structured_log_metadata,
    validate_single_statement,
)

API_BASE_URL = os.environ.get(
    "TASK_MANAGER_API_URL", "https://tasks.standart-eko.uz/api/v1"
)
SOCKET_PATH = Path(
    os.environ.get(
        "TASK_MANAGER_DIAGNOSTICS_SOCKET",
        "/run/task-manager-diagnostics/diagnostics.sock",
    )
)
AUDIT_PATH = Path(
    os.environ.get(
        "TASK_MANAGER_DIAGNOSTICS_AUDIT_FILE",
        "/var/log/task-manager-diagnostics/audit.jsonl",
    )
)
MAX_REQUEST_BYTES = 32 * 1024
# The audit trail now carries full SQL text for writes, so it has to outlive
# the day the owner asks "what did the agent change?".
MAX_AUDIT_AGE = timedelta(days=30)
MAX_AUDIT_LINES = 5000
# Phase 3 (agent-svc chat lane): a dedicated group that owns ONLY this socket,
# narrower than the old `codex-runner` group (the live GitHub Actions runner
# identity, far broader than "may read this one socket"). `agent-codex`'s
# sudoers rule for the `discussion` subcommand grants exactly this group, via
# `sudo -g`, nothing else.
SOCKET_GROUP = os.environ.get("TASK_MANAGER_DIAGNOSTICS_SOCKET_GROUP", "task-diag-client")
MAX_CALLS_PER_REVISION = 30
MAX_LOG_INPUT_BYTES = 4 * 1024 * 1024
MAX_LOG_LINE_BYTES = 4 * 1024
MAX_LOG_RESULT_BYTES = 24 * 1024
FINANCE_VERSION_TIMEOUT_SECONDS = 3
ALLOWED_CONTAINER = "ketoshop"
LEGACY_TOOLS = frozenset(
    {"ketoshop_query", "ketoshop_recent_logs", "ketoshop_finance_summary"}
)
FULL_ACCESS_TOOLS = frozenset({"db_sql", "logs", "containers"})
ALLOWED_TOOLS = LEGACY_TOOLS | FULL_ACCESS_TOOLS

# Writes are a smaller slice of the per-turn call budget.
MAX_WRITES_PER_REVISION = 10
MAX_CONCURRENT_CLIENTS = 8
DB_WORK_MEM = "16MB"
DB_STATEMENT_TIMEOUT = "15000ms"
DB_LOCK_TIMEOUT = "5000ms"
DB_IDLE_TX_TIMEOUT = "20000ms"
DOCKER_BINARY = "/usr/bin/docker"
# Attributes and memberships that turn an "agent" role into a way around every
# other control here (file/program access, signalling backends, bypassing RLS or
# reading all data). The role must be the dedicated `<project>_agent` role from
# ops/provision_agent_diagnostics_roles.sql, never an application role.
DANGEROUS_ROLES = (
    "pg_execute_server_program",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_signal_backend",
    "pg_read_all_data",
    "pg_write_all_data",
    "pg_monitor",
)
DANGEROUS_EXTENSIONS = ("dblink", "postgres_fdw")
# File and large-object functions that a plain GRANT could hand to the role.
DANGEROUS_FUNCTIONS = (
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
)
ROLE_GUARD_SQL = """
SELECT reason FROM (
    SELECT 'role attribute ' || v.name AS reason
    FROM pg_roles AS r
    CROSS JOIN LATERAL (VALUES
        ('rolsuper', r.rolsuper),
        ('rolcreaterole', r.rolcreaterole),
        ('rolcreatedb', r.rolcreatedb),
        ('rolreplication', r.rolreplication),
        ('rolbypassrls', r.rolbypassrls)
    ) AS v(name, flag)
    WHERE r.rolname = current_user AND v.flag
    UNION ALL
    SELECT 'member of ' || rolname
    FROM pg_roles
    WHERE rolname = ANY(%s) AND pg_has_role(current_user, oid, 'MEMBER')
    UNION ALL
    SELECT 'extension ' || extname FROM pg_extension WHERE extname = ANY(%s)
    UNION ALL
    SELECT 'untrusted language ' || lanname
    FROM pg_language
    WHERE NOT lanpltrusted AND lanname NOT IN ('internal', 'c')
    UNION ALL
    SELECT 'standard_conforming_strings is off'
    WHERE current_setting('standard_conforming_strings') <> 'on'
    UNION ALL
    -- Must be the dedicated, creation-incapable agent role.
    SELECT 'role name does not end in _agent' WHERE current_user !~ '_agent$'
    UNION ALL
    SELECT 'can create objects in the database'
    WHERE has_database_privilege(current_user, current_database(), 'CREATE')
    UNION ALL
    SELECT 'can create objects in schema ' || n.nspname
    FROM pg_namespace AS n
    WHERE n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
      AND has_schema_privilege(current_user, n.oid, 'CREATE')
    UNION ALL
    SELECT 'is a member of ' || g.rolname
    FROM pg_auth_members AS m
    JOIN pg_roles AS g ON g.oid = m.roleid
    JOIN pg_roles AS me ON me.oid = m.member
    WHERE me.rolname = current_user
    UNION ALL
    SELECT 'owns the database' FROM pg_database AS d
    JOIN pg_roles AS me ON me.oid = d.datdba
    WHERE d.datname = current_database() AND me.rolname = current_user
    UNION ALL
    SELECT 'owns relation ' || c.relname
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_roles AS me ON me.oid = c.relowner
    WHERE me.rolname = current_user
      AND n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
    UNION ALL
    SELECT 'owns function ' || p.proname
    FROM pg_proc AS p
    JOIN pg_namespace AS n ON n.oid = p.pronamespace
    JOIN pg_roles AS me ON me.oid = p.proowner
    WHERE me.rolname = current_user
      AND n.nspname NOT LIKE 'pg\\_%%' AND n.nspname <> 'information_schema'
    UNION ALL
    SELECT 'can execute ' || p.proname
    FROM pg_proc AS p
    WHERE p.pronamespace = 'pg_catalog'::regnamespace AND p.proname = ANY(%s)
      AND has_function_privilege(current_user, p.oid, 'EXECUTE')
) AS problems
"""
# Row-returning statements of these kinds stream through a server-side cursor so
# a huge result never sits in the broker's memory.
_CURSOR_WORDS = frozenset({"select", "values", "table"})
LOG_DEADLINE_SECONDS = 10
LOG_MAX_SINCE_MINUTES = 1440
LOG_MAX_TAIL = 2000
LOG_GREP_SCAN_LINES = 20000
LOG_MAX_LINE_CHARS = 4096
MAX_GREP_CHARS = 200


@dataclass(frozen=True)
class ProjectAccess:
    """One project's DSN variable and the containers its logs may be read from."""

    dsn_env: str
    containers: tuple[str, ...]


# Keys match backend/app/services/agent_repos.py. The first container is the
# default for `logs`. Names are the allowlist: nothing else reaches `docker`.
PROJECTS: dict[str, ProjectAccess] = {
    "ketoshop": ProjectAccess("DIAG_DSN_KETOSHOP", ("ketoshop",)),
    "qurbot": ProjectAccess("DIAG_DSN_QURBOT", ("qurbot-web", "qurbot-worker")),
    "kans-shop": ProjectAccess("DIAG_DSN_KANS_SHOP", ("kans-api", "kans-frontend")),
    "task-manager": ProjectAccess(
        "DIAG_DSN_TASK_MANAGER",
        ("task-api", "task-bot", "task-worker", "task-frontend"),
    ),
}
_KETOSHOP_IMAGE_REF = re.compile(r"^ghcr\.io/muradjanov-dev/ketoshop:([0-9a-f]{40})$")
_KETOSHOP_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

FINANCE_SUMMARY_SQL = """
WITH parameters AS (
    SELECT
        %s::text AS granularity,
        %s::integer AS bucket_count,
        date_trunc(
            %s,
            CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent'
        ) AS local_current_bucket,
        date_trunc(
            %s,
            CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Tashkent'
        ) - INTERVAL '5 hours' AS current_bucket,
        CASE WHEN %s::text = 'day' THEN INTERVAL '1 day'
             ELSE INTERVAL '1 month' END AS bucket_step
), buckets AS (
    SELECT generate_series(
        local_current_bucket - ((bucket_count - 1) * bucket_step),
        local_current_bucket,
        bucket_step
    )::date AS bucket
    FROM parameters
), orders_by_bucket AS (
    SELECT
        date_trunc(p.granularity, o.created_at + INTERVAL '5 hours')::date AS bucket,
        o.status,
        o.source,
        COUNT(*)::integer AS order_count,
        SUM(o.revenue)::numeric AS revenue,
        SUM(o.current_catalog_cost)::numeric AS current_catalog_cost,
        SUM(o.missing_cost_items)::integer AS missing_cost_items
    FROM public.ketoshop_diag_finance_orders AS o
    CROSS JOIN parameters AS p
    WHERE o.created_at >= p.current_bucket - ((p.bucket_count - 1) * p.bucket_step)
      AND o.created_at < p.current_bucket + p.bucket_step
    GROUP BY 1, 2, 3
), totals AS (
    SELECT
        bucket,
        SUM(order_count)::integer AS order_count,
        SUM(revenue)::numeric AS revenue,
        SUM(current_catalog_cost)::numeric AS current_catalog_cost,
        SUM(missing_cost_items)::integer AS missing_cost_items
    FROM orders_by_bucket
    GROUP BY bucket
), status_breakdown AS (
    SELECT
        bucket,
        jsonb_object_agg(status, jsonb_build_object(
            'order_count', order_count,
            'revenue', revenue
        )) AS breakdown
    FROM (
        SELECT bucket, status, SUM(order_count)::integer AS order_count,
               SUM(revenue)::numeric AS revenue
        FROM orders_by_bucket
        GROUP BY bucket, status
    ) AS grouped
    GROUP BY bucket
), source_breakdown AS (
    SELECT
        bucket,
        jsonb_object_agg(source, jsonb_build_object(
            'order_count', order_count,
            'revenue', revenue
        )) AS breakdown
    FROM (
        SELECT bucket, source, SUM(order_count)::integer AS order_count,
               SUM(revenue)::numeric AS revenue
        FROM orders_by_bucket
        GROUP BY bucket, source
    ) AS grouped
    GROUP BY bucket
), expenses_by_bucket AS (
    SELECT
        date_trunc(p.granularity, e.created_at + INTERVAL '5 hours')::date AS bucket,
        SUM(e.amount)::numeric AS expenses
    FROM public.ketoshop_diag_expenses AS e
    CROSS JOIN parameters AS p
    WHERE e.created_at >= p.current_bucket - ((p.bucket_count - 1) * p.bucket_step)
      AND e.created_at < p.current_bucket + p.bucket_step
    GROUP BY 1
)
SELECT
    b.bucket,
    COALESCE(t.order_count, 0)::integer AS order_count,
    COALESCE(t.revenue, 0::numeric)::numeric AS revenue,
    COALESCE(t.current_catalog_cost, 0::numeric)::numeric AS current_catalog_cost,
    COALESCE(t.missing_cost_items, 0)::integer AS missing_cost_items,
    COALESCE(e.expenses, 0::numeric)::numeric AS expenses,
    COALESCE(s.breakdown, '{}'::jsonb) AS status_breakdown,
    COALESCE(src.breakdown, '{}'::jsonb) AS source_breakdown,
    'current_catalog'::text AS cost_basis,
    FALSE AS historical_cost_data_available
FROM buckets AS b
LEFT JOIN totals AS t USING (bucket)
LEFT JOIN status_breakdown AS s USING (bucket)
LEFT JOIN source_breakdown AS src USING (bucket)
LEFT JOIN expenses_by_bucket AS e USING (bucket)
ORDER BY b.bucket DESC
"""


class DiagnosticsError(RuntimeError):
    pass


class AuditLog:
    """Store call metadata plus the full SQL text; never result rows.

    Pruned to 30 days and 5000 records.
    """

    def __init__(self, path: Path = AUDIT_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        safe = {
            "time": now.isoformat(),
            "discussion_id": int(event["discussion_id"]),
            "revision": int(event["revision"]),
            "actor_id": (
                int(event["actor_id"])
                if isinstance(event.get("actor_id"), int)
                and not isinstance(event.get("actor_id"), bool)
                and event["actor_id"] > 0
                else None
            ),
            "project_id": (
                int(event["project_id"])
                if isinstance(event.get("project_id"), int)
                and not isinstance(event.get("project_id"), bool)
                and event["project_id"] > 0
                else None
            ),
            "project_key": (
                event["project_key"]
                if isinstance(event.get("project_key"), str)
                and event["project_key"] in PROJECTS
                else "unknown"
            ),
            "tool": (
                event["tool"]
                if isinstance(event.get("tool"), str) and event["tool"] in ALLOWED_TOOLS
                else "invalid"
            ),
            "query_sha256": str(event.get("query_sha256", ""))[:64],
            "write": event.get("write") is True,
            "sql": str(event.get("sql", ""))[: MAX_SQL_CHARS + 1],
            "detail": str(event.get("detail", ""))[:500],
            "rows": int(event.get("rows", 0)),
            "result_bytes": int(event.get("result_bytes", 0)),
            "duration_ms": int(event.get("duration_ms", 0)),
            "outcome": str(event.get("outcome", "error"))[:24],
            "error": str(event.get("error", ""))[:300],
        }
        line = json.dumps(safe, separators=(",", ":"))
        cutoff = now - MAX_AUDIT_AGE
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prior: list[str] = []
            try:
                for old_line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(old_line)
                        timestamp = datetime.fromisoformat(item["time"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    if timestamp >= cutoff:
                        prior.append(old_line)
            except FileNotFoundError:
                pass
            records = (prior + [line])[-MAX_AUDIT_LINES:]
            temp = self.path.with_suffix(".tmp")
            temp.write_text("\n".join(records) + "\n", encoding="utf-8")
            temp.chmod(0o640)
            os.replace(temp, self.path)


class Context(NamedTuple):
    revision: int
    actor_id: int
    project_id: int
    project_key: str
    # True only when the backend's AGENT_DIAGNOSTICS_* settings (not the older
    # Ketoshop-only flag) authorise this session for db_sql/logs/containers.
    full_access: bool


def _normalise_dsn(dsn: str) -> str:
    """Accept a SQLAlchemy-style DATABASE_URL copied verbatim from a project."""
    return re.sub(r"^(postgres(?:ql)?)\+\w+://", r"\1://", dsn.strip())


def _known_secrets(*values: str) -> tuple[str, ...]:
    found: set[str] = set()
    for value in values:
        if not value:
            continue
        found.add(value)
        try:
            password = urllib.parse.urlsplit(value).password
        except ValueError:
            password = None
        if password:
            found.add(password)
            found.add(urllib.parse.unquote(password))
    # Longest first so a DSN is replaced whole before its password fragment.
    return tuple(sorted((item for item in found if len(item) >= 6), key=len, reverse=True))


class DiagnosticHost:
    def __init__(
        self,
        *,
        intake_token: str,
        database_url: str = "",
        project_dsns: Mapping[str, str] | None = None,
        project_ids: Mapping[str, int] | None = None,
        api_base_url: str = API_BASE_URL,
        audit: AuditLog | None = None,
        command_runner: Any = subprocess.run,
        popen_factory: Any = subprocess.Popen,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        if not intake_token:
            raise ValueError("diagnostic host credentials are required")
        self._intake_token = intake_token
        # Only the three legacy Ketoshop tools use this role (views only).
        self._database_url = database_url
        self._project_dsns = {
            key: _normalise_dsn(dsn)
            for key, dsn in (project_dsns or {}).items()
            if key in PROJECTS and dsn and dsn.strip()
        }
        # Static project key -> database id of the Task Manager project row. The
        # backend's answer must agree, so a renamed or re-pointed project row
        # cannot redirect the agent to another project's database.
        self._project_ids = dict(project_ids or {})
        self._secrets = _known_secrets(
            intake_token, database_url, *self._project_dsns.values()
        )
        self._api_base_url = api_base_url.rstrip("/")
        self._audit = audit or AuditLog()
        self._command_runner = command_runner
        self._popen_factory = popen_factory
        self._opener = opener
        self._counter_lock = threading.Lock()
        self._calls: dict[int, tuple[int, int, float, int]] = {}

    @property
    def has_full_access_databases(self) -> bool:
        # Any sign that full-access tools are configured: a DSN, or the static
        # project-id map that only exists to authorize them.
        return bool(self._project_dsns or self._project_ids)

    def _context(self, discussion_id: int, lease_id: str) -> Context:
        if (
            isinstance(discussion_id, bool)
            or not isinstance(discussion_id, int)
            or discussion_id < 1
            or not isinstance(lease_id, str)
            or not lease_id
            or len(lease_id) > 100
        ):
            raise DiagnosticsError("diagnostics are not available")
        request = urllib.request.Request(
            f"{self._api_base_url}/project-discussions/{discussion_id}/diagnostic-context",
            headers={
                "X-Intake-Worker-Token": self._intake_token,
                "X-Intake-Lease-ID": lease_id,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=3) as response:
                raw = response.read(2048)
        except (OSError, urllib.error.URLError, TimeoutError):
            raise DiagnosticsError("diagnostics are not available") from None
        try:
            context = json.loads(raw)
        except (ValueError, TypeError):
            raise DiagnosticsError("diagnostics are not available") from None
        if (
            not isinstance(context, dict)
            or context.get("authorized") is not True
            or context.get("active") is not True
            or context.get("owner_initiated") is not True
            or not isinstance(context.get("project_key"), str)
            or context["project_key"] not in PROJECTS
            or isinstance(context.get("revision"), bool)
            or not isinstance(context.get("revision"), int)
            or context["revision"] < 1
            or isinstance(context.get("actor_id"), bool)
            or not isinstance(context.get("actor_id"), int)
            or isinstance(context.get("project_id"), bool)
            or not isinstance(context.get("project_id"), int)
            or context["actor_id"] < 1
            or context["project_id"] < 1
        ):
            raise DiagnosticsError("diagnostics are not available")
        return Context(
            context["revision"],
            context["actor_id"],
            context["project_id"],
            context["project_key"],
            context.get("full_access") is True,
        )

    def _consume_call(self, discussion_id: int, revision: int, write: bool = False) -> None:
        now = time.monotonic()
        with self._counter_lock:
            for key, (saved_revision, _count, last_call, _writes) in list(self._calls.items()):
                if key == discussion_id and (
                    saved_revision != revision or now - last_call > 300
                ):
                    self._calls.pop(key, None)
            saved = self._calls.get(discussion_id)
            count = saved[1] if saved and saved[0] == revision else 0
            writes = saved[3] if saved and saved[0] == revision else 0
            if count >= MAX_CALLS_PER_REVISION:
                raise DiagnosticsError(
                    f"{MAX_CALLS_PER_REVISION} diagnostic calls per discussion turn "
                    "are allowed"
                )
            if write and writes >= MAX_WRITES_PER_REVISION:
                raise DiagnosticsError(
                    f"{MAX_WRITES_PER_REVISION} write calls per discussion turn are allowed"
                )
            self._calls[discussion_id] = (revision, count + 1, now, writes + int(write))

    def _select(self, query: str) -> tuple[list[dict[str, Any]], str, bool]:
        parsed: ParsedQuery = parse_select(query)
        if not self._database_url:
            raise DiagnosticsError("Ketoshop view diagnostics are not configured")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise DiagnosticsError(
                "diagnostics database driver is unavailable"
            ) from None
        try:
            with (
                psycopg.connect(
                    self._database_url, connect_timeout=3, row_factory=dict_row
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '5000ms'")
                cursor.execute(
                    "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
                )
                cursor.execute(parsed.sql, parsed.params)
                rows = cursor.fetchmany(min(parsed.limit, MAX_ROWS) + 1)
            has_more = len(rows) > parsed.limit
            return rows[: parsed.limit], parsed.digest, has_more
        except psycopg.Error as exc:
            # Do not surface SQL, DSNs or server exception text to the model.
            raise DiagnosticsError("approved Ketoshop query failed") from exc

    @staticmethod
    def _clean_value(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {
                str(key): DiagnosticHost._clean_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [DiagnosticHost._clean_value(item) for item in value]
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)[:512]

    def _orders(self, query: str) -> tuple[dict[str, Any], str, int]:
        rows, digest, has_more = self._select(query)
        cleaned = [
            {key: self._clean_value(value) for key, value in row.items()}
            for row in rows
        ]
        result = {
            "rows": cleaned,
            "row_count": len(cleaned),
            "truncated": has_more,
        }
        return result, digest, len(cleaned)

    def _finance_summary(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str, int]:
        period = arguments.get("period")
        bucket_count = arguments.get("count")
        if period not in {"day", "month"}:
            raise DiagnosticsError("period must be day or month")
        maximum = 180 if period == "day" else 36
        if (
            isinstance(bucket_count, bool)
            or not isinstance(bucket_count, int)
            or not 1 <= bucket_count <= maximum
        ):
            raise DiagnosticsError(
                f"count must be between 1 and {maximum} for {period}"
            )
        if not self._database_url:
            raise DiagnosticsError("Ketoshop view diagnostics are not configured")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise DiagnosticsError(
                "diagnostics database driver is unavailable"
            ) from None
        try:
            with (
                psycopg.connect(
                    self._database_url, connect_timeout=3, row_factory=dict_row
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '5000ms'")
                cursor.execute(
                    "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
                )
                cursor.execute(
                    FINANCE_SUMMARY_SQL,
                    (period, bucket_count, period, period, period),
                )
                rows = cursor.fetchmany(min(bucket_count, MAX_ROWS) + 1)
        except psycopg.Error as exc:
            raise DiagnosticsError("approved Ketoshop finance summary failed") from exc
        if len(rows) > bucket_count or len(rows) > MAX_ROWS:
            raise DiagnosticsError("finance summary exceeded its fixed bucket limit")
        image_version = self._verified_ketoshop_image_version()
        buckets = [
            {key: self._clean_value(value) for key, value in row.items()}
            for row in rows
        ]
        result = {
            "source": "ketoshop_postgresql_views",
            "period": period,
            "timezone": "Asia/Tashkent",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "bucket_count": len(buckets),
            "buckets": buckets,
            "cost_basis": "current_catalog",
            "historical_cost_data_available": False,
            "verified_version": {
                "git_commit": image_version["git_commit"] if image_version else None,
                "image_digest": (
                    image_version["image_digest"] if image_version else None
                ),
                "verified": image_version is not None,
            },
        }
        digest = hashlib.sha256(f"finance:{period}:{bucket_count}".encode()).hexdigest()
        return result, digest, len(buckets)

    def _verified_ketoshop_image_version(self) -> dict[str, str] | None:
        try:
            completed = self._command_runner(
                [
                    "/usr/bin/docker",
                    "inspect",
                    "--format",
                    "{{.Config.Image}} {{.Image}}",
                    ALLOWED_CONTAINER,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=FINANCE_VERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout or b""
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        fields = str(value).split()
        if len(fields) != 2:
            return None
        image_ref, image_id = fields
        ref_match = _KETOSHOP_IMAGE_REF.fullmatch(image_ref)
        if ref_match is None or _KETOSHOP_IMAGE_ID.fullmatch(image_id) is None:
            return None
        return {"git_commit": ref_match.group(1), "image_digest": image_id}

    def _logs(self) -> tuple[dict[str, Any], str, int]:
        command = [
            "/usr/bin/docker",
            "logs",
            "--since",
            "24h",
            "--tail",
            "500",
            ALLOWED_CONTAINER,
        ]
        try:
            process = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except OSError:
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        if process.stdout is None:
            process.kill()
            raise DiagnosticsError("Ketoshop logs are unavailable")

        lines: list[bytes] = []
        pending = bytearray()
        dropping_long_line = False
        bytes_read = 0
        truncated = False
        input_capped = False
        deadline = time.monotonic() + 5
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 5)
                    events = selector.select(min(remaining, 0.5))
                    if not events:
                        continue
                    for key, _ in events:
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        bytes_read += len(chunk)
                        if bytes_read > MAX_LOG_INPUT_BYTES:
                            truncated = True
                            input_capped = True
                            process.kill()
                            selector.unregister(key.fileobj)
                            break
                        for byte in chunk:
                            if byte == 10:
                                if not dropping_long_line:
                                    lines.append(bytes(pending).rstrip(b"\r"))
                                    if len(lines) > 500:
                                        del lines[0]
                                        truncated = True
                                pending.clear()
                                dropping_long_line = False
                            elif not dropping_long_line:
                                if len(pending) < MAX_LOG_LINE_BYTES:
                                    pending.append(byte)
                                else:
                                    pending.clear()
                                    dropping_long_line = True
                                    truncated = True
                if pending and not dropping_long_line and len(lines) < 500:
                    lines.append(bytes(pending).rstrip(b"\r"))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        finally:
            process.stdout.close()
        try:
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            raise DiagnosticsError("Ketoshop logs are unavailable") from None
        if return_code != 0 and not input_capped:
            raise DiagnosticsError("Ketoshop logs are unavailable")

        entries: list[dict[str, Any]] = []
        for line in lines[-500:]:
            metadata = structured_log_metadata(line.decode("utf-8", "replace"))
            if metadata is not None:
                entries.append(metadata)
        result: dict[str, Any] = {
            "entries": entries,
            "entry_count": len(entries),
            "window_hours": 24,
            "truncated": truncated,
        }
        while (
            entries
            and len(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            )
            > MAX_LOG_RESULT_BYTES
        ):
            entries.pop(0)
            truncated = True
            result["entry_count"] = len(entries)
            result["truncated"] = truncated
        return result, "", len(entries)

    # ------------------------------------------------------------------
    # Full-access tools. `project` is optional in every call and defaults to
    # the discussion's own project; a different value is refused.
    # ------------------------------------------------------------------
    @staticmethod
    def _project_arg(arguments: dict[str, Any], project_key: str) -> str:
        requested = arguments.get("project", project_key)
        if requested != project_key:
            raise DiagnosticsError("project does not match this discussion's project")
        return project_key

    def _error_text(self, exc: Exception) -> str:
        lines = str(exc).strip().splitlines()
        return redact_credentials(lines[0] if lines else type(exc).__name__, self._secrets)[
            :500
        ]

    def _check_session(self, cursor: Any) -> None:
        """Refuse to run agent SQL under a role that could escape the guards."""
        cursor.execute(
            ROLE_GUARD_SQL,
            (
                list(DANGEROUS_ROLES),
                list(DANGEROUS_EXTENSIONS),
                list(DANGEROUS_FUNCTIONS),
            ),
        )
        reasons = [str(row[0]) for row in cursor.fetchall()]
        if reasons:
            raise DiagnosticsError(
                "role guard: refusing to run SQL because " + "; ".join(reasons[:6])
            )

    def _db_sql(
        self, project: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str, int]:
        write = arguments.get("write", False)
        if not isinstance(write, bool):
            raise DiagnosticsError("write must be true or false")
        try:
            sql = validate_single_statement(arguments.get("sql"))
            first_word = statement_first_word(sql)
        except QueryError as exc:
            raise DiagnosticsError(str(exc)) from None
        dsn = self._project_dsns.get(project)
        if not dsn:
            raise DiagnosticsError(f"no database is configured for project {project}")
        try:
            import psycopg
        except ImportError:
            raise DiagnosticsError(
                "diagnostics database driver is unavailable"
            ) from None
        options = "-c standard_conforming_strings=on"
        if not write:
            options += " -c default_transaction_read_only=on"
        # WITH may hide a data-modifying CTE, which a cursor cannot declare.
        streamed = first_word in _CURSOR_WORDS or (not write and first_word == "with")

        def redact(text: str) -> str:
            return redact_credentials(text, self._secrets)

        try:
            with psycopg.connect(
                dsn,
                connect_timeout=3,
                application_name="task-manager-diagnostics",
                options=options,
            ) as connection:
                with connection.cursor() as setup:
                    if not write:
                        # Must be the first statement of the transaction.
                        setup.execute("SET TRANSACTION READ ONLY")
                    setup.execute(f"SET LOCAL statement_timeout = '{DB_STATEMENT_TIMEOUT}'")
                    setup.execute(f"SET LOCAL lock_timeout = '{DB_LOCK_TIMEOUT}'")
                    setup.execute(
                        f"SET LOCAL idle_in_transaction_session_timeout = '{DB_IDLE_TX_TIMEOUT}'"
                    )
                    setup.execute(f"SET LOCAL work_mem = '{DB_WORK_MEM}'")
                    setup.execute("SET LOCAL row_security = on")
                    # This query also takes the transaction's first snapshot;
                    # after that Postgres refuses SET TRANSACTION READ WRITE, so
                    # the one statement below cannot lift the read-only guard.
                    self._check_session(setup)
                cursor = (
                    connection.cursor(name="diagnostics_cursor")
                    if streamed
                    else connection.cursor()
                )
                with cursor:
                    if streamed:
                        cursor.execute(sql)
                    else:
                        # prepare=True sends Parse/Bind/Execute, so the server
                        # itself rejects a second statement.
                        cursor.execute(sql, prepare=True)
                    body: dict[str, Any] = {
                        "project": project,
                        "write": write,
                        "status": cursor.statusmessage,
                        "rowcount": cursor.rowcount,
                    }
                    if cursor.description is not None:
                        columns = [column.name for column in cursor.description]
                        fetched = cursor.fetchmany(MAX_ROWS + 1)
                        packed, truncated, masked = pack_rows(columns, fetched, redact=redact)
                        body.update(
                            columns=columns,
                            rows=packed,
                            returned_rows=len(packed),
                            truncated=truncated,
                            masked_columns=masked,
                        )
                        if streamed:
                            # A server-side cursor does not know the total.
                            body["status"] = "SELECT"
                            body["rowcount"] = len(fetched)
                if write:
                    connection.commit()
                else:
                    connection.rollback()
        except psycopg.Error as exc:
            raise DiagnosticsError(f"database error: {self._error_text(exc)}") from None
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return body, digest, max(0, int(body["rowcount"] or 0))

    def _read_docker_logs(
        self, command: list[str], tail: int, grep: str | None
    ) -> tuple[list[str], int, int, bool]:
        """Run `docker logs`; return (kept lines, scanned, matched, scan limit hit).

        Lines are redacted before the optional substring filter so `grep`
        cannot be used to probe for a redacted secret.
        """
        try:
            process = self._popen_factory(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
            )
        except OSError:
            raise DiagnosticsError("container logs are unavailable") from None
        if process.stdout is None:
            process.kill()
            raise DiagnosticsError("container logs are unavailable")
        kept: deque[str] = deque(maxlen=tail)
        head: list[str] = []
        counters = {"scanned": 0, "matched": 0}
        pending = bytearray()
        clipped = False

        def accept(raw: bytes) -> None:
            text = raw.rstrip(b"\r").decode("utf-8", "replace")
            if clipped:
                text += " ...[line truncated]"
            text = redact_credentials(text, self._secrets)
            counters["scanned"] += 1
            if len(head) < 3:
                head.append(text[:200])
            if grep and grep not in text:
                return
            counters["matched"] += 1
            kept.append(text)

        deadline = time.monotonic() + LOG_DEADLINE_SECONDS
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, LOG_DEADLINE_SECONDS)
                    if not selector.select(min(remaining, 0.5)):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                        continue
                    for index, part in enumerate(chunk.split(b"\n")):
                        if index:
                            accept(bytes(pending))
                            pending.clear()
                            clipped = False
                        room = LOG_MAX_LINE_CHARS - len(pending)
                        if len(part) > room:
                            clipped = True
                        pending.extend(part[: max(room, 0)])
            if pending:
                accept(bytes(pending))
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            raise DiagnosticsError("container logs timed out or are unavailable") from None
        finally:
            process.stdout.close()
        try:
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
            raise DiagnosticsError("container logs timed out") from None
        if return_code != 0:
            reason = " ".join(head)[:200]
            raise DiagnosticsError(f"docker logs failed: {reason}".rstrip(": "))
        return (
            list(kept),
            counters["scanned"],
            counters["matched"],
            bool(grep) and counters["scanned"] >= LOG_GREP_SCAN_LINES,
        )

    def _project_logs(
        self, project: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], str, int]:
        allowed = PROJECTS[project].containers
        container = arguments.get("container", allowed[0])
        if not isinstance(container, str) or container not in allowed:
            raise DiagnosticsError(
                f"container must be one of: {', '.join(allowed)}"
            )
        since = arguments.get("since_minutes", 60)
        tail = arguments.get("tail", 300)
        grep = arguments.get("grep")
        if (
            isinstance(since, bool)
            or not isinstance(since, int)
            or not 1 <= since <= LOG_MAX_SINCE_MINUTES
        ):
            raise DiagnosticsError(
                f"since_minutes must be between 1 and {LOG_MAX_SINCE_MINUTES}"
            )
        if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= LOG_MAX_TAIL:
            raise DiagnosticsError(f"tail must be between 1 and {LOG_MAX_TAIL}")
        if grep is not None and (
            not isinstance(grep, str) or not 1 <= len(grep) <= MAX_GREP_CHARS
        ):
            raise DiagnosticsError(f"grep must be 1-{MAX_GREP_CHARS} characters")
        command = [
            DOCKER_BINARY,
            "logs",
            "--since",
            f"{since}m",
            "--tail",
            str(LOG_GREP_SCAN_LINES if grep else tail),
            "--",
            container,
        ]
        lines, scanned, matched, scan_limit = self._read_docker_logs(command, tail, grep)
        # Keep the newest lines that fit the result budget.
        used = 0
        start = len(lines)
        for index in range(len(lines) - 1, -1, -1):
            size = len(lines[index].encode("utf-8")) + 1
            if used + size > RESULT_BODY_BUDGET - 1024:
                break
            used += size
            start = index
        shown = lines[start:]
        body: dict[str, Any] = {
            "project": project,
            "container": container,
            "since_minutes": since,
            "scanned_lines": scanned,
            "returned_lines": len(shown),
            "truncated": len(shown) < matched or scan_limit,
            "text": "\n".join(shown),
        }
        if grep:
            body.update(grep=grep, matched_lines=matched, scan_limit_reached=scan_limit)
        return body, "", len(shown)

    def _project_containers(self, project: str) -> tuple[dict[str, Any], str, int]:
        names = PROJECTS[project].containers
        template = "\t".join(
            [
                "{{.Name}}",
                "{{.State.Status}}",
                "{{.State.Running}}",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                "{{.Config.Image}}",
                "{{.State.StartedAt}}",
                "{{.RestartCount}}",
                "{{.State.ExitCode}}",
            ]
        )
        try:
            completed = self._command_runner(
                [DOCKER_BINARY, "inspect", "--format", template, *names],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DiagnosticsError("docker is unavailable") from None
        raw = completed.stdout or b""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        found: dict[str, dict[str, Any]] = {}
        for line in str(raw).splitlines():
            fields = line.split("\t")
            if len(fields) != 8:
                continue
            name = fields[0].lstrip("/")
            if name not in names:
                continue
            found[name] = {
                "name": name,
                "state": fields[1],
                "running": fields[2] == "true",
                "health": fields[3],
                "image": redact_credentials(fields[4], self._secrets),
                "started_at": fields[5],
                "restart_count": int(fields[6]) if fields[6].isdigit() else None,
                "exit_code": int(fields[7]) if fields[7].lstrip("-").isdigit() else None,
            }
        containers = [found.get(name, {"name": name, "state": "missing"}) for name in names]
        return {"project": project, "containers": containers}, "", len(containers)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        discussion_id = request.get("discussion_id")
        lease_id = request.get("lease_id")
        tool = request.get("tool")
        revision = 0
        actor_id = 0
        project_id = 0
        project_key = ""
        digest = ""
        rows = 0
        outcome = "denied"
        error_text = ""
        body: dict[str, Any]
        # Captured before any check so denied attempts are audited as well.
        raw_arguments = request.get("arguments")
        arguments: dict[str, Any] = raw_arguments if isinstance(raw_arguments, dict) else {}
        audit_sql = ""
        if tool == "ketoshop_query" and isinstance(request.get("query"), str):
            audit_sql = request["query"]
        elif isinstance(arguments.get("sql"), str):
            audit_sql = arguments["sql"]
        write = arguments.get("write") is True
        detail = json.dumps(
            {key: value for key, value in arguments.items() if key != "sql"},
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        try:
            context = self._context(discussion_id, lease_id)
            revision, actor_id, project_id = (
                context.revision,
                context.actor_id,
                context.project_id,
            )
            project_key = context.project_key
            self._consume_call(
                discussion_id, revision, write=tool == "db_sql" and write
            )
            outcome = "error"
            if tool in LEGACY_TOOLS:
                if project_key != "ketoshop":
                    raise DiagnosticsError("this tool is only available for ketoshop")
                if tool == "ketoshop_query":
                    query = request.get("query")
                    if not isinstance(query, str):
                        raise DiagnosticsError("invalid query")
                    body, digest, rows = self._orders(query)
                elif tool == "ketoshop_recent_logs":
                    body, digest, rows = self._logs()
                else:
                    if not isinstance(raw_arguments, dict):
                        raise DiagnosticsError("invalid finance summary arguments")
                    body, digest, rows = self._finance_summary(raw_arguments)
            elif tool in FULL_ACCESS_TOOLS:
                if not context.full_access:
                    raise DiagnosticsError(
                        "full diagnostics are not enabled for this project"
                    )
                if raw_arguments is not None and not isinstance(raw_arguments, dict):
                    raise DiagnosticsError("invalid arguments")
                expected_id = self._project_ids.get(project_key)
                if expected_id is None or expected_id != context.project_id:
                    raise DiagnosticsError(
                        "project identity does not match the broker's configuration"
                    )
                project = self._project_arg(arguments, project_key)
                if tool == "db_sql":
                    body, digest, rows = self._db_sql(project, arguments)
                elif tool == "logs":
                    body, digest, rows = self._project_logs(project, arguments)
                else:
                    body, digest, rows = self._project_containers(project)
            else:
                raise DiagnosticsError("unknown diagnostic tool")
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode()
            if len(encoded) > MAX_RESULT_BYTES:
                raise DiagnosticsError("diagnostic result exceeds 64 KB")
            outcome = "ok"
            response = {"ok": True, "result": body}
            result_bytes = len(encoded)
        except DiagnosticsError as exc:
            error_text = str(exc)
            response = {"ok": False, "error": error_text}
            result_bytes = len(error_text.encode())
        except (OSError, TypeError, ValueError, subprocess.SubprocessError):
            error_text = "diagnostics request failed"
            response = {"ok": False, "error": error_text}
            result_bytes = len(error_text.encode())
        if isinstance(discussion_id, int) and not isinstance(discussion_id, bool):
            self._audit.record(
                {
                    "discussion_id": discussion_id,
                    "revision": revision,
                    "actor_id": actor_id if revision > 0 else None,
                    "project_id": project_id if revision > 0 else None,
                    "project_key": project_key if revision > 0 else "unknown",
                    "tool": (
                        tool
                        if isinstance(tool, str) and tool in ALLOWED_TOOLS
                        else "invalid"
                    ),
                    "query_sha256": digest,
                    "write": write,
                    "sql": audit_sql,
                    "detail": detail if detail != "{}" else "",
                    "rows": rows,
                    "result_bytes": result_bytes,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "outcome": outcome,
                    "error": scrub_error_values(error_text),
                }
            )
        return response


_CLIENT_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CLIENTS)


def _handle_client(connection: socket.socket, host: DiagnosticHost) -> None:
    with connection:
        connection.settimeout(40)
        # Bound concurrent database/docker work; callers beyond it wait briefly.
        if not _CLIENT_SLOTS.acquire(timeout=5):
            connection.sendall(b'{"ok":false,"error":"diagnostics broker is busy"}\n')
            return
        try:
            _serve_request(connection, host)
        finally:
            _CLIENT_SLOTS.release()


def _serve_request(connection: socket.socket, host: DiagnosticHost) -> None:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
        if not chunk or b"\n" in chunk:
            if chunk:
                data.extend(chunk.split(b"\n", 1)[0])
            break
        data.extend(chunk)
    if len(data) > MAX_REQUEST_BYTES:
        response = {"ok": False, "error": "diagnostic request is too large"}
    else:
        try:
            request = json.loads(data)
            if not isinstance(request, dict):
                raise TypeError("request must be a JSON object")
            response = host.handle(request)
        except (ValueError, TypeError):
            response = {"ok": False, "error": "invalid diagnostic request"}
    encoded = json.dumps(
        response, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"ok":false,"error":"diagnostic result exceeds 64 KB"}'
    connection.sendall(encoded + b"\n")


def _narrow_socket_group(socket_path: Path, group_name: str, *, required: bool = False) -> None:
    """Best-effort: chgrp the socket to `group_name` if that group exists on
    this host; otherwise leave its group exactly as it is today (the
    process's own primary/effective group, `codex-runner` per the systemd
    unit's own `Group=`). This ordering means installing this code can never
    itself break the still-live legacy consumer (`ops/discussion_appserver.py`,
    running as `codex-runner`) regardless of whether the group-creation step
    of the installer has run yet on this host.

    `required=True` (full-access tools enabled) fails closed instead: with a
    database DSN loaded, a socket left in the wider legacy group is not acceptable.
    """
    try:
        gid = grp.getgrnam(group_name).gr_gid
    except KeyError:
        if required:
            raise RuntimeError(
                f"diagnostics: group {group_name} is required when full-access "
                "tools are configured"
            ) from None
        return
    try:
        os.chown(socket_path, -1, gid)
    except OSError as error:
        if required:
            raise RuntimeError(
                f"diagnostics: cannot chgrp the socket to {group_name} "
                f"({type(error).__name__}); the service user must be a member"
            ) from None
        # The service user must be a member of the group to chgrp (no
        # CAP_CHOWN). Never let this take the broker down: keep today's group.
        print(
            f"diagnostics: socket stays in its current group ({type(error).__name__})",
            flush=True,
        )


def serve(host: DiagnosticHost, socket_path: Path = SOCKET_PATH) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError("diagnostics socket path exists and is not a socket")
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    try:
        socket_path.chmod(0o660)
        _narrow_socket_group(
            socket_path, SOCKET_GROUP, required=host.has_full_access_databases
        )
        server.listen(16)
    except BaseException:
        server.close()
        socket_path.unlink(missing_ok=True)
        raise
    try:
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=_handle_client, args=(connection, host), daemon=True
            ).start()
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def _parse_project_ids(raw: str) -> dict[str, int]:
    """`ketoshop:3,qurbot:5` -> {"ketoshop": 3, "qurbot": 5}; bad entries are skipped."""
    found: dict[str, int] = {}
    for part in raw.split(","):
        key, _, value = part.strip().partition(":")
        if key.strip() in PROJECTS and value.strip().isdigit() and int(value) > 0:
            found[key.strip()] = int(value)
    return found


def main() -> None:
    host = DiagnosticHost(
        intake_token=os.environ["INTAKE_WORKER_TOKEN"],
        database_url=os.environ.get("KETOSHOP_DIAGNOSTICS_DATABASE_URL", ""),
        # A project whose variable is absent simply has no database tool.
        project_dsns={
            key: os.environ.get(access.dsn_env, "") for key, access in PROJECTS.items()
        },
        project_ids=_parse_project_ids(os.environ.get("DIAG_PROJECT_IDS", "")),
    )
    serve(host)


if __name__ == "__main__":
    main()
