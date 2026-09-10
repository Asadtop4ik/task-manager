#!/usr/bin/env sh
set -e

echo "[entrypoint] running alembic migrations..."
alembic upgrade head

case "$1" in
  api)
    echo "[entrypoint] starting FastAPI (uvicorn)..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  *)
    # Pass-through, so `docker compose run ... alembic upgrade head` and friends
    # work without a shell override. Expected service commands are 'api'.
    echo "[entrypoint] passthrough: $*"
    exec "$@"
    ;;
esac
