# Owner-only Ketoshop diagnostics

The diagnostics MCP server is available only in a live Ketoshop discussion by
the configured Task Manager owner. The app-server receives its tool command,
discussion ID and that turn's unguessable lease capability through trusted
`app-server --config` overrides. The broker sends the capability to the API,
which constant-time compares it with the active lease on that exact discussion.
The model never receives the intake token, database URL, Docker socket or
database password. Its proxy can call only the host broker over one Unix socket.

The lease capability is passed only to that turn's MCP proxy. We verified the
same systemd/bubblewrap profile with two simultaneous `codex-runner` processes:
a second read-only Codex shell reported the proxy PID's `/proc/<pid>/cmdline`
as unreadable. The host still requires the
capability and verifies it against the active lease for every call, so a socket
caller without the owner's current capability is denied.

The broker asks the Task Manager API to re-check the owner, Ketoshop project,
active discussion lease and turn revision before every tool call. It reads only
the allowlisted `ketoshop_diag_*` views in a read-only transaction. The row
query parser accepts a limited `SELECT` grammar, rejects raw
tables/joins/writes/comments, and parameterizes filter values. A separate fixed
aggregate tool groups by day or month and returns counts, status/source
breakdowns, delivered revenue, expenses, current catalog cost estimates and
missing-cost item counts across more than 200 orders.
Daily and monthly buckets use Asia/Tashkent business time; the production
Ketoshop database stores these timestamps without a timezone on a UTC server.

Historical `cost_price` values are not stored in Ketoshop order snapshots.
Finance summaries label cost as a current catalog estimate and mark historical
cost unavailable. Lines with a missing or zero catalog cost are counted as
unknown instead of being included as zero-cost sales.

Limits are five seconds per database statement, 200 rows, 64 KB per result and
10 tool calls per active turn. The logs tool reads only the fixed `ketoshop`
container, at most 500 lines from the last 24 hours, captures both output
streams, and returns at most 24 KB of safe metadata. It exposes structured
time, level, logger, event and status fields only; free-form messages and
unstructured lines are omitted. The audit file stores discussion ID, internal
actor/project IDs, revision, tool name, query hash, row count, result size,
duration and outcome. It keeps the latest 500 records from the last 24 hours;
it never stores lease capabilities, query text or results.

## Provision the read-only Ketoshop database role

Run the role script interactively against the Ketoshop database as its database
administrator. `psql` prompts for a generated password. Run the views file in
the same database after the role exists.

```sh
psql -v ON_ERROR_STOP=1 \
  -f ops/provision_ketoshop_diagnostics_role.sql \
  -f ops/ketoshop_diagnostic_views.sql
```

Store a DSN for `ketoshop_diagnostics` in the host service environment file.
The role has no table or sequence grants; it can select only the anonymized
order, item and finance views. Keep the database port on the host's private
network and verify the role has no inherited memberships.

## Install and enable the host broker

After deploying the Task Manager API commit and running
`bash ops/install_project_catalog.sh` from the matching clean `main`, create
the service account and its protected environment file on the host:

```sh
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin task-diagnostics
sudo usermod -aG docker task-diagnostics
sudo install -o root -g task-diagnostics -m 0640 /dev/null /etc/task-manager/diagnostics.env
sudo python3 -m venv /opt/task-manager/diagnostics-venv
sudo /opt/task-manager/diagnostics-venv/bin/pip install \
  --requirement /opt/task-manager/ops/requirements-diagnostics.txt
```

Set these values in `/etc/task-manager/diagnostics.env` without printing the
file or putting credentials in a shell command line:

```text
INTAKE_WORKER_TOKEN=<the existing worker token>
KETOSHOP_DIAGNOSTICS_DATABASE_URL=postgresql://ketoshop_diagnostics:<password>@<private-host>/<database>
TASK_MANAGER_API_URL=https://tasks.standart-eko.uz/api/v1
```

Enable the broker only after verifying the read-only role and views:

```sh
sudo systemctl enable --now task-manager-diagnostics.service
sudo systemctl is-active task-manager-diagnostics.service
```

The broker owns the database credentials and Docker group access. The socket
is group-readable by `codex-runner`; it accepts only the fixed read tools. Turn
on `KETOSHOP_DIAGNOSTICS_ENABLED=true` in the Task Manager API
environment only after the service is active and the socket exists. Restart
the API after changing that flag. Until then, the app-server does not advertise
diagnostic MCP tools.

After installation, verify the unit and socket before enabling the API flag:

```sh
sudo systemd-analyze verify /etc/systemd/system/task-manager-diagnostics.service
sudo systemctl is-active task-manager-diagnostics.service
sudo stat -c '%a %U %G' /run/task-manager-diagnostics/diagnostics.sock
```

Then verify the owner can see the Ketoshop tools and a non-owner cannot.
The broker audit file should show only metadata after a tool call; it must not
contain the submitted SQL or any returned order field. Keep the feature flag
off if the role/view, socket, or API authorization check fails.

## Synthetic QA database

The checked-in fixture contains 205 synthetic orders, current and missing
catalog costs, expenses and synthetic contact columns. It verifies that the
aggregate covers more than 200 orders without exposing contact fields. It
refuses to run unless connected to a database named exactly
`ketoshop_diagnostics_qa`. Use a disposable local PostgreSQL instance:

```sh
createdb ketoshop_diagnostics_qa
psql ketoshop_diagnostics_qa -v ON_ERROR_STOP=1 \
  -f ops/ketoshop_diagnostics_qa_fixture.sql \
  -f ops/ketoshop_diagnostic_views.sql
```

The fixture is for tests only. Do not point it at the production Ketoshop
database. Automated tests exercise the parser, proxy, aggregate caps, log
redaction, audit contents and API owner/lease checks using synthetic values.
