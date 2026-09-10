import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from redis.exceptions import RedisError

from app.config import settings
from app.loader import create_bot, create_dispatcher
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    bot = create_bot()
    dispatcher = create_dispatcher()
    app.state.bot = bot
    app.state.dispatcher = dispatcher

    if settings.public_url:
        await bot.set_webhook(
            url=f"{settings.public_url.rstrip('/')}{settings.webhook_path}",
            secret_token=settings.webhook_secret,
            # A restart must not silently swallow whatever arrived while the
            # container was down — those are real messages from real people.
            drop_pending_updates=False,
        )
        # The URL is deliberately not logged: it is a bearer of the webhook path.
        log.info("webhook_registered")
    else:
        log.info("webhook_disabled", reason="PUBLIC_URL is empty")

    yield

    await bot.session.close()
    await dispatcher.storage.close()


app = FastAPI(title="Task Manager Bot", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Redis is the bot's only hard dependency — it holds the FSM state."""
    try:
        storage_redis = app.state.dispatcher.storage.redis
        await storage_redis.ping()
    except (RedisError, OSError, AttributeError) as exc:
        log.error("readiness_redis_failed", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "checks": {"redis": "error"}}
    return {"status": "ok", "checks": {"redis": "ok"}}


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    # Constant-time: a plain `!=` leaks the secret one byte at a time to anyone
    # who can time the responses, and this endpoint is on a public domain.
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, settings.webhook_secret
    ):
        log.warning("webhook_rejected", reason="bad secret header")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    update = Update.model_validate(await request.json())
    await request.app.state.dispatcher.feed_webhook_update(request.app.state.bot, update)
    return {"ok": True}
