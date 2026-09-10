from datetime import UTC, datetime

from arq.connections import RedisSettings

from app.config import settings
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


async def ping(ctx: dict[str, object]) -> str:
    """Round-trip check for the queue itself.

    Enqueue it and wait for the result to prove the whole path — Redis, the
    worker process, the job serialiser — is alive, which a container healthcheck
    on the process alone cannot tell you.
    """
    return datetime.now(UTC).isoformat()


async def startup(ctx: dict[str, object]) -> None:
    configure_logging()
    log.info("worker_starting")


async def shutdown(ctx: dict[str, object]) -> None:
    log.info("worker_stopped")


class WorkerSettings:
    """arq entrypoint.

    Only `ping` for now — reminder and digest jobs land in milestone 5. arq
    refuses to start with an empty function list, and the process exists from the
    first deploy so the stack file and the deploy path are exercised early rather
    than bolted on later.
    """

    functions = [ping]  # noqa: RUF012
    cron_jobs: list = []  # noqa: RUF012
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
