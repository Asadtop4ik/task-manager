# Task Manager — plan

A task manager for the three live projects (**ketoshop**, **qurbot**, **kans-shop**).
The manager assigns todos from a Telegram bot; work happens on the web at
`tasks.standart-eko.uz`; status flows back to Telegram.

Decisions taken up front: Telegram-only login, full two-way bot, two roles
(manager / executor) with three seeded projects, monorepo with three images.

---

## 1. Stack

Nothing new is introduced. Every piece below is already running on the netcup box
for kans-shop or qurbot, so there is one operational model to learn, not four.

| Layer | Choice | Why this one |
|---|---|---|
| API | **FastAPI + SQLAlchemy 2 async + Alembic**, Python 3.12 | Same as kans-shop; `entrypoint.sh` runs `alembic upgrade head` before uvicorn, so a deploy that ships a migration applies it with nothing to remember. |
| DB | **Postgres 18**, new database `taskmgr` on the shared instance | Adding a project costs a database, not a server (`infra/initdb/00-databases.sh` pattern). |
| Queue / state | **Redis 7 + arq** worker | Already there, `noeviction`, already holds qurbot's arq queue. Reminders and digests are jobs, not a `while True: sleep`. |
| Bot | **aiogram 3**, webhook | qurbot's transport. Webhook + secret header beats polling on a box that already has Caddy in front. |
| Web | **React 19 + Vite + TypeScript + Tailwind 4**, TanStack Query, zustand, react-router | Byte-for-byte the kans-shop frontend stack; the Dockerfile and nginx.conf can be copied. |
| Edge | **Caddy** site block, automatic TLS | One A record + one block. |
| CI/CD | GitHub Actions → GHCR → SSH forced command | Copy `ci.yml`/`deploy.yml` from kans-shop. |

**Three images, one repo** (`Asadtop4ik/task-manager`):

```
backend/    FastAPI app + Alembic          → ghcr.io/…/task-manager-api
bot/        aiogram 3 webhook + arq worker → ghcr.io/…/task-manager-bot
frontend/   Vite SPA served by nginx       → ghcr.io/…/task-manager-frontend
```

The bot is its own image because it has a different dependency set and a
different failure mode from the API — a bot crash-loop should not take the board
offline. Both talk to the same Postgres; the bot calls the API over the internal
`stack` network for anything with business rules in it, so notification and
permission logic lives in exactly one place.

---

## 2. Data model

```
users        id, telegram_id (unique), username, full_name, role(manager|executor),
             lang(uz|ru), tz, is_active, created_at
projects     id, key(ketoshop|qurbot|kans-shop|…), name, color, is_archived
memberships  user_id, project_id, role_in_project        -- who sees what
tasks        id, project_id, title, description,
             status(backlog|todo|in_progress|blocked|review|done|cancelled),
             priority(low|normal|high|urgent),
             assignee_id, created_by_id,
             due_at, started_at, done_at,
             estimate_minutes, spent_minutes,
             source(bot|web), source_chat_id, source_message_id,
             created_at, updated_at
comments     id, task_id, author_id, body, created_at
attachments  id, task_id, tg_file_id, file_name, mime, size   -- see §6 on files
activity     id, task_id, actor_id, kind, payload(jsonb), created_at
reminders    id, task_id, user_id, fire_at, kind, sent_at     -- arq job mirror
```

Notes that matter later:

- **`activity` is the audit log and the notification source.** Every mutation
  writes one row; the notifier reads rows, not endpoints. That keeps "who gets a
  Telegram message" out of every handler.
- **`source_chat_id` / `source_message_id`** let the bot *edit the original
  message* when a task is finished, instead of spamming a new one.
- **`status` is an enum with a fixed transition table**, not free text — the bot's
  inline buttons and the web board must agree on what "done" means.
- Soft delete only (`cancelled`), because the manager will ask what happened.

---

## 3. Auth — Telegram only

Two entry points, one identity:

1. **Web** — Telegram Login Widget on `tasks.standart-eko.uz`. The backend
   verifies the `hash` field: HMAC-SHA256 over the sorted `key=value` data-check
   string, keyed by `SHA256(bot_token)`, compared in constant time, and rejects
   `auth_date` older than 5 minutes. Requires **BotFather `/setdomain` →
   `tasks.standart-eko.uz`** — the same manual step qurbot needs (§8).
2. **Mini App / bot deep links** — `initData` verified the same way except the
   key is `HMAC-SHA256("WebAppData", bot_token)`. Different constant, same code
   path; get this wrong and every login fails with an identical-looking error.

On success the API issues a short-lived access JWT (15 min) plus a refresh token
in an `HttpOnly; Secure; SameSite=Lax` cookie. **A first-time telegram_id is not
auto-admitted** — it lands in a pending list and the manager approves it in the
bot. An open Telegram login on a public domain is otherwise an open door.

Roles: `manager` creates, assigns, reassigns, sees everything.
`executor` sees tasks in their projects, moves status, comments, logs time.

---

## 4. Bot UX

**Manager — creating a task.** Two paths, because typing a form on a phone is
worse than talking:

- *Quick* — free text with an optional prefix: `keto: fix mini app url !urgent
  @asad tomorrow 18:00`. The parser resolves project by key/alias, `!priority`,
  `@assignee`, and a date phrase (uz/ru/en). What it parsed is shown as a
  confirmation card with **Confirm / Edit / Cancel** — never silently guessed.
- *Guided* — `/new`, then inline keyboards: project → assignee → priority →
  deadline → title/description. An FSM in Redis, one message edited in place.

Forwarding or replying to any message with `/task` turns it into a task and keeps
the link back to the original.

**Executor — working.** On assignment you get a card:

```
🟠 ketoshop · urgent · due today 18:00
Fix Mini App URL in BotFather

[▶ Start] [✅ Done] [💬 Comment] [⏰ Snooze] [🌐 Open]
```

Buttons mutate status through the API and **edit the same message** so the chat
stays one card per task. `[🌐 Open]` deep-links into the web board.

**Both.** `/my` — my open tasks grouped by project. `/today` — due today +
overdue. `/project keto` — that project's board. `/done 12` — close by id.
Every callback is answered (`answerCallbackQuery`) — qurbot's release pass fixed
exactly this class of bug, don't repeat it.

**Push notifications** (arq jobs, all deduplicated per user per task):
assignment, comment on your task, status change on a task you created,
deadline in 1 hour, overdue at 09:00, daily digest at 09:00 in the user's tz,
manager's evening summary at 19:00.

---

## 5. Web app

Phone-first, because half the use is on a phone even outside Telegram.

- **Board** — kanban columns by status, drag between them, filter by project /
  assignee / priority / overdue. Optimistic updates via TanStack Query.
- **List** — dense sortable table for when the board gets long; bulk edit.
- **Task detail** — description (markdown), comments, activity timeline,
  attachments, time log.
- **My day** — today + overdue + in-progress, the default landing page.
- **Projects** — per-project settings, members, archive.
- **Manager dashboard** — open tasks per project, overdue count, throughput last
  14 days, per-person workload.

Realtime: **SSE** on `/api/v1/events` (not WebSockets — one-way updates through
Caddy, and it survives a reconnect without extra code).

---

## 6. API surface

`/api/v1` — `auth/telegram`, `auth/refresh`, `auth/me`;
`projects`; `tasks` (list with filters, create, patch, transition, assign);
`tasks/{id}/comments`; `tasks/{id}/time`; `users`; `events` (SSE);
`stats/dashboard`. Plus `/health` (process) and `/ready` (Postgres + Redis) —
the compose healthcheck uses `/ready`, matching qurbot.

The bot authenticates to the API with a service token and an `X-Acting-User`
header carrying the verified telegram_id, so bot actions are attributed to the
real person in `activity` and permission checks run identically for both clients.

**Attachments:** store Telegram's `file_id` and stream through the API on demand
rather than copying bytes to disk. No new volume, nothing new in the backup
rotation. (kans-shop's media volume is the one piece of state outside Postgres
and it exists only because it had to.)

---

## 7. Deploy

Follows §4 "Adding a fourth project" in `HANDOFF.md` exactly:

1. **DNS** — A record `tasks.standart-eko.uz` → `159.195.248.216`.
2. **Database** — create role + database `taskmgr` on the shared Postgres; add it
   to `infra/initdb/00-databases.sh` so a rebuilt box comes up complete.
3. **`deploy/stacks/task-manager.yml`** — services `task-api` (healthcheck
   `/ready`, mem_limit 512m), `task-bot` (webhook), `task-worker` (arq),
   `task-frontend` (nginx, 64m); `networks: [stack]`, `env_file:
   ../env/task-manager.env`, json-file logging capped at 10m×3.
4. **Caddy site block:**
   ```
   tasks.{$DOMAIN} {
       import common
       @api path /api/* /health
       handle @api { reverse_proxy task-api:8000 }
       @hook path /webhook/*
       handle @hook { reverse_proxy task-bot:8080 }
       handle { reverse_proxy task-frontend:80 }
   }
   ```
   Do not add `X-Frame-Options DENY` — the Mini App is an iframe.
5. **`scripts/ci-deploy-shell.sh`** — add `task-manager` to the `case`.
   **Nothing deploys until this line exists**; the forced command refuses
   unknown stacks and logs to `journalctl -t ci-deploy`.
6. **Repo secrets** — `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`,
   `DEPLOY_KNOWN_HOSTS` (pinned host key, not `StrictHostKeyChecking=no`).
7. **`/srv/stack/env/task-manager.env`** (chmod 600, never in git, never pasted
   into a chat): `ENVIRONMENT=production`, `BOT_TOKEN`, `BOT_USERNAME`,
   `WEBHOOK_SECRET`, `DATABASE_URL`, `DATABASE_URL_SYNC`, `REDIS_URL`,
   `JWT_SECRET`, `SERVICE_TOKEN`, `PUBLIC_URL`, `ADMIN_TELEGRAM_IDS`.
   **`REDIS_URL` must end in `/2`** — the shared Redis has db 0 for kans-shop and
   db 1 for qurbot, and reusing one would stomp on another bot's FSM state.
   `ENVIRONMENT=production` is what arms the placeholder-secret check that makes
   an unfilled env file fail at boot instead of running with a guessable secret.
8. Keep `~/Desktop/Business_AI/deploy/` and `/srv/stack` in sync — they were
   verified byte-identical on 2026-09-08, keep it that way.

Webhook hardening from day one: Telegram's `X-Telegram-Bot-Api-Secret-Token`
compared with `hmac.compare_digest`, `drop_pending_updates=False` on restart,
and a hard fail at boot on placeholder secrets.

**Cost of the new stack:** ~700 MB RAM. The box was at 1.8 GB of 7.7 GB.

---

## 8. Manual steps only a human can do

1. **Create the bot** in BotFather, take the token, put it straight into the
   server env file.
2. **`/setdomain` → `tasks.standart-eko.uz`**, or the web Login Widget renders
   nothing — this is the exact trap qurbot is still sitting in.
3. `/setcommands` for `/new /my /today /project /done /help`.
4. Send the manager the bot link and approve their telegram_id once.

---

## 9. Milestones

| # | Deliverable | Done when |
|---|---|---|
| 1 | ✅ **Done.** Repo layout, compose, full schema in `0001_initial_schema`, `/health` + `/ready`, CI, three Dockerfiles | Local stack up; `/ready` green on Postgres + Redis; ruff/black/mypy clean; 14 tests pass |
| 2 | ✅ **Done.** Telegram auth (widget + Mini App), users/approval, projects, tasks, comments, activity, permissions | 60 backend tests; full flow verified against the running API. The widget itself still needs a real bot token + BotFather `/setdomain` |
| 3 | Bot: `/new` guided + quick parse, assignment card, status buttons | Manager can create a task from the phone and I get the card |
| 4 | Web: My Day, board, task detail, comments | Usable as a daily driver |
| 5 | Reminders, digests, SSE realtime | Overdue and 09:00 digest fire correctly across timezones |
| 6 | Deploy to `tasks.standart-eko.uz`, seed the 3 projects, both users in | Live, CI/CD deploying on push to `main` |
| 7 | Dashboard, time tracking, attachments, uz/ru i18n | — |

Milestones 1–4 are the usable product; 5–7 are what make it stick.

---

## 10. Risks and things to decide

- **Nightly backups are still not scheduled** (`HANDOFF.md` §7.3). This project
  adds a fourth database to a box whose data has no off-site copy. Add the
  crontab entry and a remote destination *before* this becomes load-bearing —
  it is a larger risk than anything in this plan.
- **Date parsing in uz/ru** is where a quick-capture bot usually fails. The
  confirmation card is the mitigation: never create a task from a guessed date.
- **Telegram-only auth** means a lost Telegram account is a lost login. The
  `ADMIN_TELEGRAM_IDS` bootstrap list is the escape hatch.
- **Timezones**: store UTC, render in the user's tz, and set `tz` per user at
  first login — a digest that fires at 09:00 UTC is 14:00 in Tashkent.
- Open: should the manager see time logs? Does a task need subtasks/checklists,
  or are comments enough for v1? (Recommendation: no subtasks in v1.)

---

## 11. Decisions made while building milestone 1

Things that were not obvious from the plan and are now settled in code:

- **The whole §2 schema shipped in `0001_initial_schema`**, not just a stub. The
  migration is where the model gets locked in; doing it once makes milestone 2
  purely API and auth work. Verified by applying it to an empty Postgres 18 and
  re-running autogenerate, which finds nothing — CI enforces that from now on.
- **Two Python images, two different `redis` pins.** `arq==0.28.0` is the newest
  release and still requires `redis<6`, so the bot holds at `redis==5.3.1` while
  the API (no arq) runs `redis==8.0.1`. They never share a process.
- **Postgres 18 moved its data directory.** The mount is `/var/lib/postgresql`,
  not `/var/lib/postgresql/data`, or the entrypoint refuses to start — the same
  thing the production infra compose already learned.
- **GHCR rejects uppercase owners.** This repo is under `Asadtop4ik`, so the
  deploy workflow lowercases `github.repository_owner` before tagging; the raw
  value fails the push.
- **Local host ports are shifted** (Postgres 5433, Redis 6380) because the dev
  machine already runs both for the other projects, and bound to 127.0.0.1.
- **TypeScript 7 removed `baseUrl`** — path aliases now resolve relative to the
  tsconfig, and leaving it in fails the build.
- **`/ready` is the container healthcheck, `/health` is liveness.** Liveness
  deliberately touches nothing external, so a Postgres blip cannot turn a small
  outage into a Docker-driven crash loop.
- **The status transition table lives in `app/db/enums.py`** with tests. It is
  the single place the board, the bot's buttons and the API agree, and it is what
  stops a stale Telegram card from reopening a task closed last week.

### Milestone 2

- **`GET /auth/config` serves the bot username at runtime** instead of baking it
  in as a Vite build arg, so one frontend image works against any bot.
- **A missing project or an invisible task answers 404, never 403.** "Exists but
  is not yours" leaks the id space and how busy other projects are.
- **JWT_SECRET and SERVICE_TOKEN must be ≥32 characters in production**, checked
  at boot. PyJWT warns at runtime about short HS256 keys; failing at startup is
  better than a warning nobody reads.
- **Model-level `default=` is applied by SQLAlchemy in Python, not by Postgres.**
  The seed migration has to spell out `is_archived` because a raw INSERT never
  sees it — the same trap waits for any future data migration.
- **The refresh cookie is scoped to `/api/v1/auth` and is SameSite=Lax.** Strict
  would drop the cookie when the Login Widget returns the user by top-level
  navigation, which looks exactly like a broken login.
- **One shared in-flight refresh on the client.** Three parallel 401s must not
  fire three refreshes and race each other's tokens.
