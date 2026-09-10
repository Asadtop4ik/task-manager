#!/usr/bin/env sh
set -e

case "$1" in
  bot)
    echo "[entrypoint] starting bot webhook server..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8080
    ;;
  worker)
    echo "[entrypoint] starting arq worker..."
    exec arq app.worker.WorkerSettings
    ;;
  *)
    # Pass-through, so `docker compose run ... alembic upgrade head` and friends
    # work without a shell override. Expected service commands are 'bot' or 'worker'.
    echo "[entrypoint] passthrough: $*"
    exec "$@"
    ;;
esac
