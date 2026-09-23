# Task Manager

Tasks for the three live projects (**ketoshop**, **qurbot**, **kans-shop**). The
manager assigns todos from a Telegram bot; the work happens on the web at
`tasks.standart-eko.uz`; status flows back to Telegram.

Design and milestones: **[PLAN.md](PLAN.md)**.

Status: **milestone 4 — usable.** Telegram login, the approval queue, projects,
tasks with a real status machine, comments, an activity log, permissions, the
bot's full command set, and the web app are done. Reminders and digests are
milestone 5.

## Coding agent integration

After an operator configures a private project repository and the agent credentials,
send `task-manager: update the task view @codex` to the Telegram bot. Confirm the
parsed task; the bot creates it and starts one Codex run. `/agent 42` delegates
an existing task. The task detail page shows the current run and PR link, and
the bot reports PR-ready or failed runs through the worker's notification queue.
Use `/stopagent 42` or the task page to cancel a running job. A ready PR is
closed when cancelled, so it cannot auto-merge later. A failed or cancelled job
can be retried once on the same task; change its description when the agent
needs new information. Failure notices include the agent's question or error.

Masalan, 42-raqamli task allaqachon mavjud bo'lsa, Telegram botiga `/agent 42`
yuboring. Bot shu taskni Codexga topshiradi. Codex PR yaratgach, uning havolasi
veb boarddagi shu task sahifasida ko'rinadi.

Agar `@codex` ishga tushmasa, loyiha repoga ulanganini, repo private ekanini va runner online ekanini tekshiring.

The project repository and default branch are manager-only settings. The backend
also requires the repository in `GITHUB_AGENT_ALLOWED_REPOS` and verifies it is
private before dispatch. Only `Asadtop4ik/task-manager` is allowlisted by default.
The worker uses a GitHub `repository_dispatch` event; its workflow runs on a
private `codex-agent` self-hosted runner. The repository needs these credentials:

- Server env: `GITHUB_AGENT_TOKEN` for repository metadata, dispatch and PR
  verification; `AGENT_CALLBACK_TOKEN` for workflow callbacks.
- GitHub Actions secrets: `AGENT_REPO_TOKEN` for the PR push/create step;
  `AGENT_CALLBACK_TOKEN` matching the server value.
- Runner: Codex CLI logged in under its dedicated account, plus `gh` and Python 3.
  Do not expose the Codex auth cache to a public repository or a general runner.

The workflow keeps GitHub write credentials out of the Codex step. Protected
paths (CI, agent instructions, migrations and auth code) stop before PR creation.
The default-branch auto-merge workflow accepts only README, Markdown docs and
frontend CSS changes after the latest commit's backend, bot, frontend and policy
checks pass. Other PRs need review. A coding task is done only after CI, a real
SSH deploy, the exact running image tag and both `/ready` endpoints pass; the
deployment callback then records the deployed SHA and notifies Telegram.
Completed Codex runs also store input, cached-input and output token counts;
these are usage measurements, not a dollar invoice for a ChatGPT subscription.
For automatic review of human and agent PRs, connect this repository to Codex
Cloud and enable Code review plus Automatic reviews in Codex settings. The
repository's `AGENTS.md` includes the review rules. This is a separate, one-time
account setting from the self-hosted task runner.

## The web app

- **Bugun** — the first screen answers one question, am I behind, so the count of
  late work is the headline rather than a stat tile.
- **Doska** — kanban with drag between columns on desktop, a status picker on a
  phone, filtered by project, assignee and lateness.
- **Task detail** — status, assignee and priority inline; comments; time log; the
  full history. The title and description are edited where they sit: click, type,
  Enter to save, Escape to abandon.

Keyboard: **⌘K / Ctrl+K** opens search — it looks through open tasks and the
pages, arrow keys move, Enter opens. **n** starts a new task. A bare letter never
fires while you are typing into a field.

On the board, drag a card between columns to change its status, or within a
column to set the order you want to work in. Both are optimistic and both stick.

Colour is information: each project owns a hue, shown as a rail down every row,
and lateness owns red. There is no brand accent competing with them. Light and
dark both follow the system.

## Using the bot

Type a line and confirm what it understood:

```
keto: mini app url ni tuzat !shoshilinch @asad ertaga 18:00
```

Project by prefix (`keto` → ketoshop, only when unambiguous), `!priority` in
uz/ru/en, `@username`, and a deadline as `ertaga` / `завтра` / `tomorrow`,
`indinga`, a weekday, `25.12 09:00`, `18:00` or `+3`. **Nothing is created from
the parse** — the bot shows what it read and waits, because a silently mis-read
deadline is worse than no parsing at all.

`/new` walks the same thing with buttons. `/my`, `/today`, `/projects` list work;
`/task` as a reply turns that message into a task. Task cards carry Start / Done
/ Block / Comment / Snooze / Open and are **edited in place**, so a task keeps one
card instead of filling the chat.

## API

`/api/v1` — `auth/{config,telegram,telegram/miniapp,refresh,logout,me}`,
`users`, `users/pending`, `projects`, `projects/{id}/members`, `tasks`,
`tasks/{id}/{transition,assign,time,comments,activity}`. Plus `/health`
(liveness) and `/ready` (Postgres + Redis, used by the container healthcheck).

Two ways in, one identity: a browser sends a Bearer access token; the bot sends
`X-Service-Token` plus `X-Acting-User`, so its actions are attributed to the real
person and run through exactly the same permission checks.

A first-time Telegram login creates an **inactive** account that waits in
`users/pending` for a manager. `ADMIN_TELEGRAM_IDS` bootstraps the first one —
without it nobody could ever approve anybody.

## Layout

```
backend/    FastAPI + SQLAlchemy 2 async + Alembic   → task-manager-api
bot/        aiogram 3 webhook + arq worker           → task-manager-bot
frontend/   React 19 + Vite + Tailwind 4             → task-manager-frontend
```

Three images from one repo. The bot is separate from the API because a bot
crash-loop should not take the board offline; it reaches the API over the
internal `stack` network so permission and notification logic lives in one place.

## Running it locally

```bash
cp .env.example .env      # fill in BOT_TOKEN only when you need the bot
make up                   # postgres, redis, api, frontend
```

- API — <http://localhost:8000> (`/health`, `/ready`, `/docs`)
- Frontend — <http://localhost:8081>, or `make fe-dev` for Vite on :5173

Host ports are shifted off the defaults (Postgres **5433**, Redis **6380**)
because this machine already runs a Postgres and a Redis for the other projects.

The bot and worker are behind a profile, since they need a real token:

```bash
docker compose --profile bot up -d
```

`make help` lists the rest.

## Migrations

`entrypoint.sh` runs `alembic upgrade head` before uvicorn, so a deploy that
ships a migration applies it on the way up — and `deploy.sh --wait` turns a
failed migration into a failed deploy rather than a silent one.

```bash
make revision M="add widgets"   # autogenerate
make migrate                    # apply
```

CI re-runs the migrations against an empty database *and* checks that
autogenerate finds nothing left to do. A model changed without a migration fails
the build instead of failing the deploy.

## Checks

```bash
make lint    # ruff + black + mypy, both Python services
make test    # pytest, both Python services
```

## Deploying

Push to `main` → CI → three images to GHCR → SSH to the netcup box →
`/srv/stack/scripts/deploy.sh task-manager <sha>`.

The server-side files (`stacks/task-manager.yml`, the Caddy block, the
`ci-deploy-shell.sh` allowlist entry) live in `../deploy/`, which mirrors
`/srv/stack`. Nothing deploys until `task-manager` is in that allowlist — the
deploy key is behind a forced command that refuses unknown stack names.

Remaining before the first deploy: the DNS record, the database, the env file,
the four repo secrets, and the BotFather steps. PLAN.md §7–8 has the checklist.
