# Owner-only Ketoshop diagnostics

The diagnostics MCP server is available only in a live Ketoshop discussion by
the configured Task Manager owner. The app-server receives its tool command and
the active discussion ID through trusted `app-server --config` overrides. The
model never receives the intake token, database URL, Docker socket or database
password. Its proxy can call only the host broker over one Unix socket.

The broker asks the Task Manager API to re-check the owner, Ketoshop project,
active discussion lease and turn revision before every tool call. It reads only
the two `ketoshop_diag_*` views in a read-only transaction. The query parser
accepts a limited `SELECT` grammar, rejects raw tables/joins/writes/comments,
and parameterizes filter values.

Limits are five seconds per database statement, 200 rows, 64 KB per result and
10 tool calls per active turn. The logs tool reads only the fixed `ketoshop`
container, at most 500 lines from the last 24 hours, and redacts common contact
fields, phone numbers, email addresses and tokens. The audit file stores
discussion ID, revision, tool name, query hash, row count, result size,
duration and outcome. It keeps the latest 500 records from the last 24 hours;
it never stores query text or results.

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
order summary and item views. Keep the database port on the host's private
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
is group-readable by `codex-runner`; it accepts only the two fixed diagnostic
tools. Turn on `KETOSHOP_DIAGNOSTICS_ENABLED=true` in the Task Manager API
environment only after the service is active and the socket exists. Restart
the API after changing that flag. Until then, the app-server does not advertise
diagnostic MCP tools.

After installation, verify the unit and socket before enabling the API flag:

```sh
sudo systemd-analyze verify /etc/systemd/system/task-manager-diagnostics.service
sudo systemctl is-active task-manager-diagnostics.service
sudo stat -c '%a %U %G' /run/task-manager-diagnostics/diagnostics.sock
```

Then verify the owner can see the two Ketoshop tools and a non-owner cannot.
The broker audit file should show only metadata after a tool call; it must not
contain the submitted SQL or any returned order field. Keep the feature flag
off if the role/view, socket, or API authorization check fails.

## Synthetic QA database

The checked-in fixture contains only synthetic orders and intentionally
includes synthetic contact columns to verify that views do not expose them. It
refuses to run unless connected to a database named exactly
`ketoshop_diagnostics_qa`. Use a disposable local PostgreSQL instance:

```sh
createdb ketoshop_diagnostics_qa
psql ketoshop_diagnostics_qa -v ON_ERROR_STOP=1 \
  -f ops/ketoshop_diagnostics_qa_fixture.sql \
  -f ops/ketoshop_diagnostic_views.sql
```

The fixture is for tests only. Do not point it at the production Ketoshop
database. Automated tests exercise the parser, proxy, host limits, redaction,
audit contents and API owner/lease checks using synthetic values.
