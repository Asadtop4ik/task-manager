# Task Manager

Tasks for the three live projects (**ketoshop**, **qurbot**, **kans-shop**). The
manager assigns todos from a Telegram bot; the work happens on the web at
`tasks.standart-eko.uz`; status flows back to Telegram.

Design and milestones: **[PLAN.md](PLAN.md)**.

Status: **milestone 3 — the bot works.** Telegram login, the approval queue,
projects, tasks with a real status machine, comments, an activity log,
permissions, and the bot's full command set are done and tested. The web board
is milestone 4; reminders and digests are milestone 5.

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
