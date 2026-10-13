# Agent diagnostics: live database and log access (owner only)

When the owner asks something in a `/suhbat` discussion, the Codex agent can
investigate live data itself instead of saying it has no database access. This
covers the discussion lane only, for four projects:

| Project key | Database | Containers (first = default for `logs`) |
| --- | --- | --- |
| `ketoshop` | `ketoshop` | `ketoshop` |
| `qurbot` | `qurbot` | `qurbot-web`, `qurbot-worker` |
| `kans-shop` | `kansshop` | `kans-api`, `kans-frontend` |
| `task-manager` | `taskmgr` | `task-api`, `task-bot`, `task-worker`, `task-frontend` |

The owner decided on full access: the agent may read and write every table of
these databases and read every log line of these containers.

## What the agent can do

The `project_diagnostics` MCP server (`ops/diagnostic_proxy.py`) exposes:

- `db_sql(sql, write=false, project?)`: exactly one SQL statement. `write=false`
  runs in a READ ONLY transaction (also `default_transaction_read_only=on` on the
  connection) and rolls back; `write=true` commits. 15 s statement timeout, 5 s
  lock timeout, idle-in-transaction timeout, `work_mem` 16 MB, `row_security=on`.
  At most 200 rows, cells clipped to 2000 characters, whole result under 64 KB.
  Row-returning statements stream through a server-side cursor, so a huge result
  is never buffered in the broker.
  - Refused in both modes: transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`,
    `SAVEPOINT`, ...), `PREPARE TRANSACTION`, `COMMIT/ROLLBACK PREPARED`,
    `LISTEN/NOTIFY/UNLISTEN`, `DISCARD`, `CHECKPOINT`, `VACUUM`, `REINDEX`,
    `CLUSTER`, `LOAD`, `COPY`, `DO`, `SET`/`RESET` (they would undo the
    timeouts) and `ALTER SYSTEM|ROLE|USER|GROUP|DATABASE`.
  - One statement per call: a lexer-aware splitter, and the statement is sent
    through the extended protocol (`prepare`), where the server itself rejects a
    second statement. `standard_conforming_strings=on` is forced on the
    connection and verified.
  - Credentials are redacted from every text cell (including nested JSON). Values
    in columns whose name looks like
    `password|passwd|secret|token|api_key|private_key|hash|lease|salt|otp` are
    returned as `[masked]`. Name masking is advisory only: it catches conventional
    names, so a secret stored in a column called `notes` is not masked (known
    broker DSN passwords still are).
- `logs(project?, container?, since_minutes<=1440 (60), tail<=2000 (300), grep?)`:
  full text lines from `docker logs` of an allowlisted container (the name is
  passed after `--`). `grep` is a plain, case-sensitive substring filter over the
  last 20000 lines. Known DSN passwords, `Bearer ...` values, Telegram bot tokens
  and passwords inside `postgres://user:pass@` URLs are redacted.
- `containers(project?)`: state, health, image, start time and restart count.
- The three older `ketoshop_*` tools keep working unchanged (anonymized views).

The agent never sees a DSN or password: the host-side broker
(`ops/diagnostic_host.py`, systemd `task-manager-diagnostics.service`) holds
them and runs the SQL and `docker` commands.

## Authorization

Every call carries the discussion id and the unguessable lease id of the running
turn. The lease id reaches the MCP proxy through the `DIAGNOSTIC_LEASE_ID`
environment variable (forwarded by Codex's `env_vars`), not argv, so it is not
visible in the process list. The broker asks the backend
(`GET /project-discussions/{id}/diagnostic-context`) and proceeds only if:

- the session is active and was started by the owner (`OWNER_TELEGRAM_ID`);
- the project row is the catalog repository for its key
  (`repository_for(key, repo, branch)`);
- the backend grants `full_access`: `AGENT_DIAGNOSTICS_ENABLED=true`, the key is
  in `AGENT_DIAGNOSTICS_PROJECTS`, **and** the project row's id equals the fixed
  id in `AGENT_DIAGNOSTICS_PROJECT_IDS` (for example `ketoshop:3,qurbot:5`). A
  project without a configured id never gets full access; this stops anyone who
  can create or rename a project row (the project "key" is data) from steering the
  agent at another database;
- the broker's own static map `DIAG_PROJECT_IDS` (same `key:id` list, in
  `/etc/task-manager/diagnostics.env`) agrees with the id the backend returned.

A `project` argument that differs from the discussion's own project is refused,
and a project with no configured DSN simply has no database tool (clear error, no
crash). Limits: 30 calls per turn, of which at most 10 may be `write=true`; 32 KB
per request; at most 8 concurrent socket calls.

The older `KETOSHOP_DIAGNOSTICS_ENABLED` flag still enables only the three
anonymized Ketoshop tools; setting a DSN alone never opens `db_sql` for such a
session.

## Database roles (required)

`DIAG_DSN_*` must point at a dedicated per-project role, **never** at the
application's own DSN. `ops/provision_agent_diagnostics_roles.sql` creates
`<project>_agent`: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
NOBYPASSRLS`, no built-in role membership, `CONNECT` on its own database only,
`SELECT/INSERT/UPDATE/DELETE` on the application schema's tables and
`USAGE/SELECT/UPDATE` on its sequences (plus default privileges for tables the
application creates later), no `CREATE`. The script also sets role-level
`temp_file_limit=256MB` and `statement_timeout=15s` (settings only a superuser
may apply, which the broker is not).

On every `db_sql` connection the broker checks the connected role and refuses to
run the agent's SQL (and audits the refusal) if it has `rolsuper`,
`rolcreaterole`, `rolcreatedb`, `rolreplication` or `rolbypassrls`, is a member of
`pg_execute_server_program`, `pg_read_server_files`, `pg_write_server_files`,
`pg_signal_backend`, `pg_read_all_data`, `pg_write_all_data` or `pg_monitor`, or
the database has `dblink`, `postgres_fdw` or any untrusted procedural language
installed. Use of an application role (usually the table owner, often with
`CREATE`) is thereby caught for the privilege cases above, but the script is
what narrows it properly.

Task Manager's own database is the sensitive one: the agent could otherwise grant
itself more access (`users`, project membership) or tamper with agent leases. Run
its script with a read-only list, so the `task-manager_agent` role gets only
`SELECT` on those tables and full DML on everything else (tasks, comments,
attachments, activity, reminders):

```text
-v readonly_tables=users,projects,memberships,project_discussions,agent_runs,agent_run_actions,agent_intakes,agent_events,agent_ops_requests,team_invites,join_requests
-v hidden_columns=project_discussions.lease_id,agent_runs.lease_id,agent_intakes.lease_id,agent_ops_requests.lease_id,team_invites.token_hash
```

(`alembic_version` is always read-only. `hidden_columns` are not readable at all:
lease ids and invite token hashes are capabilities.) After a migration that adds
a table, the new table is `SELECT`-only for this role until you re-run the script
with the same full lists, which then grants DML to any table not listed.

Stop the roles reaching other projects' databases (once per database):
`REVOKE CONNECT ON DATABASE <db> FROM PUBLIC;` then
`GRANT CONNECT ON DATABASE <db> TO <that database's application role>;` (the
database owner keeps access). The provisioning script prints a warning for every
database the agent role can still connect to.

## Audit

`/var/log/task-manager-diagnostics/audit.jsonl` (mode 0640, kept 30 days / 5000
records) records for every call: time, discussion id, actor id, project, tool,
`write` flag, the full SQL text, row count, duration, outcome (`ok`/`error`) and
error text. Result rows are never recorded, and data values inside Postgres error
text (`Key (col)=(value)`, `Failing row contains (...)`) are replaced by `...`
while constraint names are kept. To see what the agent changed:
`sudo jq 'select(.write)' /var/log/task-manager-diagnostics/audit.jsonl`.

## One-time server steps

1. **Provision the agent roles** as the Postgres admin (once per project; you
   will be prompted for the new role's password, so no secret is on a command
   line):

   ```sh
   psql -h 127.0.0.1 -U postgres -d ketoshop -v ON_ERROR_STOP=1 \
     -v agent_role=ketoshop_agent -v table_owner=ketoshop \
     -f ops/provision_agent_diagnostics_roles.sql
   psql -h 127.0.0.1 -U postgres -d ketoshop -c '\password ketoshop_agent'
   ```

   Repeat for `qurbot` (`qurbot_agent`, owner `qurbot`, db `qurbot`), `kans-shop`
   (`kans_shop_agent`, db `kansshop`), and `task-manager` (`task_manager_agent`,
   db `taskmgr`, with the read-only and hidden lists above). `table_owner` is the
   role from the project's own `DATABASE_URL`. Then run the `REVOKE CONNECT ...
   FROM PUBLIC` step above.
2. Deploy this commit to `main` (the backend settings must exist), then from the
   clean `main` checkout run `bash ops/install_project_catalog.sh`. It copies
   `diagnostic_host.py`, `diagnostic_proxy.py`, `diagnostic_security.py` and the
   unit file (now `MemoryMax=512M`) to `/opt/task-manager/ops` and runs
   `daemon-reload`. It does not restart the broker. `ops/install_agent_svc.sh`
   must already have run (it creates `task-diag-client` and adds
   `task-diagnostics` to it): with a DSN loaded the broker now **refuses to
   start** if it cannot `chgrp` its socket to that group.
3. Add these to `/etc/task-manager/diagnostics.env` (never print the file or put
   values on a command line). Each DSN uses the **agent** role from step 1, host
   `127.0.0.1`, port `5432`, no `+asyncpg` suffix (the broker also strips it) and
   no query parameters libpq does not know. Use the real Task Manager project row
   ids (the `id` of the project in the `projects` table):

   ```text
   DIAG_DSN_KETOSHOP=postgresql://ketoshop_agent:<password>@127.0.0.1:5432/ketoshop
   DIAG_DSN_QURBOT=postgresql://qurbot_agent:<password>@127.0.0.1:5432/qurbot
   DIAG_DSN_KANS_SHOP=postgresql://kans_shop_agent:<password>@127.0.0.1:5432/kansshop
   DIAG_DSN_TASK_MANAGER=postgresql://task_manager_agent:<password>@127.0.0.1:5432/taskmgr
   DIAG_PROJECT_IDS=ketoshop:<id>,qurbot:<id>,kans-shop:<id>,task-manager:<id>
   ```

   Keep the file `root:task-diagnostics 0640`. A project without its DSN line has
   no `db_sql` (logs and containers still work); `DIAG_PROJECT_IDS` is required
   for any full-access tool. `KETOSHOP_DIAGNOSTICS_DATABASE_URL` stays for the
   three anonymized Ketoshop tools and is optional.
4. In the Task Manager API environment set (same ids as `DIAG_PROJECT_IDS`; list
   only the projects you want live) and restart the API container:

   ```text
   AGENT_DIAGNOSTICS_ENABLED=true
   AGENT_DIAGNOSTICS_PROJECTS=ketoshop,qurbot,kans-shop,task-manager
   AGENT_DIAGNOSTICS_PROJECT_IDS=ketoshop:<id>,qurbot:<id>,kans-shop:<id>,task-manager:<id>
   ```

5. Restart the broker so it loads the new code and environment:
   `sudo systemctl restart task-manager-diagnostics.service`, then
   `sudo systemctl is-active task-manager-diagnostics.service` and
   `sudo stat -c '%a %U %G' /run/task-manager-diagnostics/diagnostics.sock`
   (group must be `task-diag-client`). Restart `agent-svc` too if it was deployed
   in the same release, so discussions use the new MCP wiring.
6. As the owner, ask a data question in a project discussion; confirm the answer
   cites queries/log lines and that the audit file has the matching records. Also
   confirm a `write=false` `DELETE` and, for `task-manager`, a write to `users`
   are both refused. A non-owner discussion must still answer without tools.

Rollback: set `AGENT_DIAGNOSTICS_ENABLED=false` and restart the API; new turns
lose the tools immediately.

## Risks

- Customer data (names, phones, addresses, orders) returned by `db_sql` and
  `logs` is sent to the model provider as part of the conversation. Only
  credential-shaped values and conventionally named columns are masked; the rest
  is visible to the model and to the Telegram chat that receives its answer.
- `write=true` changes production data for real and is committed immediately.
  The prompt tells the agent to write only on an explicit owner request, after
  showing a SELECT of the affected rows, but that is instruction, not a
  technical barrier. The agent role can do what its grants allow (DML on
  everything but the Task Manager tables listed above; no DDL).
- The per-call timeouts and READ ONLY mode are guard rails against accidents, not
  a security boundary against a determined statement (for example
  `set_config('statement_timeout', '0', true)` inside a SELECT); the role
  privileges are the boundary.
- Rows and log lines come from users and may contain prompt-injection text; the
  agent is told to treat them as data.
- Take a backup before bulk changes. Postgres backups are in
  `/srv/backups/postgres`; restoring is the only undo for a bad write.
- Always `LIMIT` large selects. A statement that hits the 15 s timeout is
  cancelled.
