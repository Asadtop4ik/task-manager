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
  runs inside `SET TRANSACTION READ ONLY` and rolls back; `write=true` commits.
  15 s statement timeout, 5 s lock timeout, idle-in-transaction timeout. At most
  200 rows, cells clipped to 2000 characters, whole result under 64 KB. Values in
  columns named like `password|passwd|secret|token|api_key|private_key|hash|salt|otp`
  are returned as `[masked]`.
- `logs(project?, container?, since_minutes<=1440 (60), tail<=2000 (300), grep?)`:
  full text lines from `docker logs` of an allowlisted container. `grep` is a
  plain, case-sensitive substring filter over the last 20000 lines. Known DSN
  passwords, `Bearer ...` values, Telegram bot tokens and passwords inside
  `postgres://user:pass@` URLs are redacted.
- `containers(project?)`: state, health, image, start time and restart count.
- The three older `ketoshop_*` tools keep working unchanged (anonymized views).

The agent never sees a DSN or password: the host-side broker
(`ops/diagnostic_host.py`, systemd `task-manager-diagnostics.service`) holds
them and runs the SQL and `docker` commands.

## Authorization

Every call carries the discussion id and the unguessable lease id of the running
turn. The broker asks the backend (`GET /project-discussions/{id}/diagnostic-context`)
and proceeds only if the session is active, was started by the owner
(`OWNER_TELEGRAM_ID`), the project is allowed, and the backend grants
`full_access` (see below). A `project` argument that differs from the
discussion's own project is refused, and a project with no configured DSN simply
has no database tool (clear error, no crash). Limits: 30 calls per turn, 32 KB
per request.

`full_access` is true only when `AGENT_DIAGNOSTICS_ENABLED=true` and the project
key is in `AGENT_DIAGNOSTICS_PROJECTS`. The older `KETOSHOP_DIAGNOSTICS_ENABLED`
flag still enables only the three anonymized Ketoshop tools; setting a DSN alone
never opens `db_sql` for such a session.

## Audit

`/var/log/task-manager-diagnostics/audit.jsonl` (mode 0640, kept 30 days / 5000
records) records for every call: time, discussion id, actor id, project, tool,
`write` flag, the full SQL text, row count, duration, outcome (`ok`/`error`/
`denied`) and error text. Result rows are never recorded. To see what the agent
changed: `sudo jq 'select(.write)' /var/log/task-manager-diagnostics/audit.jsonl`.

## One-time server steps

1. Deploy this commit to `main` (the backend settings must exist), then from the
   clean `main` checkout run `bash ops/install_project_catalog.sh`. It copies
   `diagnostic_host.py`, `diagnostic_proxy.py`, `diagnostic_security.py` and the
   unit file (now `MemoryMax=512M`) to `/opt/task-manager/ops` and runs
   `daemon-reload`. It does not restart the broker.
2. Add one line per project to `/etc/task-manager/diagnostics.env` (the file the
   unit already loads; never print it or put values on a command line). Copy
   each `DATABASE_URL` from `/srv/stack/env/<project>.env` and change it as
   needed:
   - drop a `+asyncpg` / `+psycopg` driver suffix (the broker also strips it);
   - use host `127.0.0.1` and port `5432` instead of the Docker-network name;
   - remove query parameters libpq does not know (for example `?ssl=...`).

   ```text
   DIAG_DSN_KETOSHOP=postgresql://ketoshop:<password>@127.0.0.1:5432/ketoshop
   DIAG_DSN_QURBOT=postgresql://qurbot:<password>@127.0.0.1:5432/qurbot
   DIAG_DSN_KANS_SHOP=postgresql://<role>:<password>@127.0.0.1:5432/kansshop
   DIAG_DSN_TASK_MANAGER=postgresql://<role>:<password>@127.0.0.1:5432/taskmgr
   ```

   Keep the file `root:task-diagnostics 0640`. A project without its line has no
   `db_sql` (logs and containers still work). `KETOSHOP_DIAGNOSTICS_DATABASE_URL`
   stays for the three anonymized Ketoshop tools and is optional now.
3. The broker reaches Postgres at `127.0.0.1:5432` (published by the shared
   Postgres container; the unit allows `AF_INET`). Make sure `pg_hba.conf`
   accepts each DSN role from the Docker gateway address. The first `db_sql`
   call in step 6 proves it; a connection failure shows up as a `database error`
   in the answer and in the audit file.
4. In the Task Manager API environment set:

   ```text
   AGENT_DIAGNOSTICS_ENABLED=true
   AGENT_DIAGNOSTICS_PROJECTS=ketoshop,qurbot,kans-shop,task-manager
   ```

   (list only the projects you want live) and restart the API container.
5. Restart the broker so it loads the new code and environment:
   `sudo systemctl restart task-manager-diagnostics.service`, then
   `sudo systemctl is-active task-manager-diagnostics.service` and
   `sudo stat -c '%a %U %G' /run/task-manager-diagnostics/diagnostics.sock`.
6. As the owner, ask a data question in a project discussion; confirm the answer
   cites queries/log lines and that the audit file has the matching records. A
   non-owner discussion must still answer without tools.

Rollback: set `AGENT_DIAGNOSTICS_ENABLED=false` and restart the API; new turns
lose the tools immediately.

## Risks

- Customer data (names, phones, addresses, orders) returned by `db_sql` and
  `logs` is sent to the model provider as part of the conversation. Only
  credential-shaped values and sensitive-named columns are masked; the rest is
  visible to the model and to the Telegram chat that receives its answer.
- `write=true` changes production data for real and is committed immediately.
  The prompt tells the agent to write only on an explicit owner request, after
  showing a SELECT of the affected rows, but that is instruction, not a
  technical barrier. The role in each DSN can do whatever that application role
  can (including DDL if it owns the schema).
- Rows and log lines come from users and may contain prompt-injection text; the
  agent is told to treat them as data.
- Take a backup before bulk changes. Postgres backups are in
  `/srv/backups/postgres`; restoring is the only undo for a bad write.
- `db_sql` buffers a result set in memory before capping it; always `LIMIT`
  large selects. A statement that hits the 15 s timeout is cancelled.
