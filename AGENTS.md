# Task Manager project instructions

This repository runs the internal Telegram task bot and web board at
`tasks.standart-eko.uz`. Read `README.md` for the current user flows and `PLAN.md`
for the original design; verify old deployment notes against the live workflows.

## Boundaries

- `backend/`: FastAPI, PostgreSQL models, migrations and permissions. The API is
  the authority for task transitions and user access.
- `bot/`: aiogram webhook and arq worker. The bot calls the API with a service
  token and acting Telegram user; it does not implement its own task permissions.
- `frontend/`: React task board. It must respect the same status transitions as
  the API.
- `.github/workflows/ci.yml` checks pull requests. `deploy.yml` builds and
  deploys the exact commit after CI. Do not report a skipped deploy as success.

## Checks

- Backend: from `backend/`, run `ruff check app tests`, `black --check app tests`,
  `mypy app`, and `pytest -q` with test PostgreSQL and Redis available.
- Bot: from `bot/`, run the same four commands. The bot test environment needs
  `REDIS_URL` and placeholder webhook/service values from CI.
- Frontend: from `frontend/`, run `npm ci`, `npm run typecheck`, `npm run build`.
- A backend model change requires an Alembic migration in the same change. CI
  applies migrations to an empty PostgreSQL database and checks schema drift.

## Agent tasks

- Keep one task on one branch and link its PR to the task ID. State the expected
  behavior and the verification performed.
- Ask for one specific decision when a business rule is missing. Do not invent
  prices, payment behavior, authorization rules, customer messages or deadlines.
- Treat changes to authentication, roles, money, database migrations, mass
  Telegram sends, secrets, CI/deploy, and agent permissions as owner-review work.
- Never use production bot tokens or production customer data in tests. Do not
  commit `.env`, login caches, SSH keys or other credentials.
- A task is done only when its stated behavior is verified. Deployment tasks also
  require the deployed commit and `/ready` checks to match.
