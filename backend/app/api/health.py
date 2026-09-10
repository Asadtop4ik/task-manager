from fastapi import APIRouter, Response, status
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import async_session_maker

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only: is this process answering?

    Deliberately touches nothing external — a Postgres blip should not make Docker
    kill an otherwise fine container.
    """
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness: can this process actually do its job?

    This is what the container healthcheck and `deploy.sh --wait` watch, so a
    deploy that cannot reach Postgres or Redis fails loudly instead of reporting
    a false success.
    """
    checks: dict[str, str] = {}

    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except (SQLAlchemyError, OSError) as exc:
        log.error("readiness_postgres_failed", error=str(exc))
        checks["postgres"] = "error"

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except (RedisError, OSError) as exc:
        log.error("readiness_redis_failed", error=str(exc))
        checks["redis"] = "error"

    ok = all(value == "ok" for value in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "checks": checks}
