.PHONY: help up down logs migrate revision lint test fe-install fe-dev fe-build

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

up:  ## Start postgres, redis, api and frontend locally
	docker compose up -d --build

down:  ## Stop everything (volumes kept)
	docker compose down

logs:  ## Tail the API log
	docker compose logs -f task-api

migrate:  ## Apply migrations inside the api container
	docker compose run --rm task-api alembic upgrade head

revision:  ## Autogenerate a migration: make revision M="add widgets"
	docker compose run --rm task-api alembic revision --autogenerate -m "$(M)"

lint:  ## Lint and type-check both Python services
	cd backend && ruff check app tests && black --check app tests && mypy app
	cd bot && ruff check app tests && black --check app tests && mypy app

test:  ## Run backend and bot tests
	cd backend && pytest -q
	cd bot && pytest -q

fe-install:  ## Install frontend dependencies
	cd frontend && npm ci

fe-dev:  ## Vite dev server on :5173
	cd frontend && npm run dev

fe-build:  ## Production build
	cd frontend && npm run build
