# Task Manager

Tasks for the three live projects (**ketoshop**, **qurbot**, **kans-shop**). The
manager assigns todos from a Telegram bot; the work happens on the web at
`tasks.standart-eko.uz`; status flows back to Telegram.

Design and milestones: **[PLAN.md](PLAN.md)**.

Status: the private Task Manager pilot is live. Invite-only team access,
one-time bot login links, projects, task history, the board, and the Codex
PR/owner `!fast` workflows are available. Scheduled reminders and digests
remain planned work.

## Coding agent integration

In a private Telegram chat, send `task-manager: @codex ...` or owner-only
`task-manager: !fast ...`. The bot keeps this as a draft while a separate
read-only Codex worker checks the repository and up to three reference images.
If the request is clear, the bot sends a short goal and acceptance summary;
otherwise it asks at most three necessary questions. Answer in the chat, then
press **Bajarish** to create the task and start the coding run. **Tuzatish**
requests a new summary, and **Bekor qilish** discards the draft. No task or
implementation run exists before confirmation. Use `/cancel` to discard a
pending draft. A picture with the task as its caption works, as does a picture
followed by the task text within ten minutes. PNG, JPEG and WebP are supported,
up to three images of at most 20 MB each. This pilot is enabled only for the
Task Manager project and for members with Codex access.

After an operator configures a private project repository and the agent credentials,
send `task-manager: update the task view @codex` to the Telegram bot. Confirm the
parsed task; the bot creates it and starts one Codex run. `/agent 42` delegates
an existing task. The task detail page shows the current run and PR link, and
the bot reports PR-ready or failed runs through the worker's notification queue.
Use `/stopagent 42` or the task page to cancel a running job. A ready PR is
closed when cancelled, so it cannot auto-merge later. A failed or cancelled job
can be retried once on the same task; change its description when the agent
needs new information. Failure notices include the agent's question or error.

Ordinary `@codex` tasks create a PR. Owner-only `!fast` tasks in the Task Manager
pilot publish to `main` only after the exact commit passes independent GitHub
CI. Sensitive changes fall back to a PR. A failed or missing CI run blocks the
task without publishing or opening a PR. Deployment marks the task done only
after the requested image passes readiness checks; a failed release rolls back.
During this first pilot, direct publication is limited to existing CSS styling
and literal JSX tooltip/accessibility attributes; other code changes use a PR.

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
- Intake host worker: a separate read-only Codex process and
  `INTAKE_WORKER_TOKEN` matching the API server environment. Leave
  `AGENT_INTAKE_ENABLED=false` until the host worker is installed. Intake
  questions do not create GitHub Actions jobs; implementation still uses the
  existing dispatch workflow after confirmation.

The workflow keeps GitHub write credentials out of the Codex runner. Its patch
is applied in a separate clean GitHub-hosted publisher job. Protected paths
(authentication, permissions, customer messages, money, migrations, CI/deploy
and agent control) take the PR route even when the owner requested `!fast`.
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
- **O‘chirilganlar** — owner-only task trash with restore. Hiding a task keeps
  comments, agent runs and audit history; an active agent must be stopped first.

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

The owner runs `/invite` to create a seven-day, one-use invitation. A teammate
opens it with the bot, then the owner approves or rejects the request and chooses
visible projects. An ordinary `/start` without an invite grants no access. An
approved teammate runs `/login` to receive a five-minute, one-use HTTPS link in
their private chat; the browser that opens it gets the session. Request a new
link for another browser. The owner and at most one other active teammate can
use Codex; only the owner can request `!fast`.

## API

`/api/v1` — `auth/{config,telegram,telegram/miniapp,magic/request,magic/redeem,refresh,logout,me}`,
`team/{invites,join-requests,members}`, `users`, `projects`,
`projects/{id}/members`, `tasks`, `tasks/trash`, and
`tasks/{id}/{transition,assign,time,comments,activity,restore}`. Plus `/health`
(liveness) and `/ready` (Postgres + Redis, used by the container healthcheck).

Two ways in, one identity: a browser sends a Bearer access token; the bot sends
`X-Service-Token` plus `X-Acting-User`, so its actions are attributed to the real
person and run through exactly the same permission checks.

The browser login page uses only the bot's one-time link. Legacy Telegram auth
endpoints can refresh an existing approved account, but cannot create an
uninvited one. The owner is pinned by `OWNER_TELEGRAM_ID`; invitation approval
and Codex-seat grants require that Telegram identity, not a mutable name or
username.

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
