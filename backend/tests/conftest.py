import os

# Settings are required-by-default, so the environment has to be complete before
# `app.core.config` is imported anywhere. Local runs fall back to the docker-compose
# services on their shifted host ports (5433/6380 — see docker-compose.yml);
# CI sets these itself.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://taskmgr:taskmgr@localhost:5433/taskmgr"
)
os.environ.setdefault(
    "DATABASE_URL_SYNC", "postgresql+psycopg://taskmgr:taskmgr@localhost:5433/taskmgr"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("ENVIRONMENT", "test")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
