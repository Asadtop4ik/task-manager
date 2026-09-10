from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1 import router as api_v1_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("api_starting", environment=settings.environment)
    yield
    await close_redis()
    log.info("api_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Task Manager API",
        version="0.1.0",
        lifespan=lifespan,
        # No public docs in production: the schema is a map of the API for anyone
        # who finds the host, and this one is on a public domain.
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )

    # In production the SPA and the API share one origin behind Caddy, so CORS is
    # only ever exercised by the Vite dev server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted({settings.public_url, "http://localhost:5173"}),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(api_v1_router)
    return app


app = create_app()
