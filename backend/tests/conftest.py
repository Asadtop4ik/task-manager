import os

# Settings are required-by-default, so the environment has to be complete before
# `app.core.config` is imported anywhere. Local runs fall back to the
# docker-compose services on their shifted host ports (5433/6380 — see
# docker-compose.yml); CI sets these itself.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://taskmgr:taskmgr@localhost:5433/taskmgr"
)
os.environ.setdefault(
    "DATABASE_URL_SYNC", "postgresql+psycopg://taskmgr:taskmgr@localhost:5433/taskmgr"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("ENVIRONMENT", "test")
# A fixed, obviously-fake token: every Telegram signature in the tests is
# computed against this, and no test reaches the network.
os.environ.setdefault("BOT_TOKEN", "123456:TEST-BOT-TOKEN")
# 32+ chars so the tests exercise the same key length production requires.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789abcdef")
os.environ.setdefault("SERVICE_TOKEN", "test-service-token-0123456789abcd")

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import db_session
from app.core.config import settings
from app.core.security import create_access_token
from app.db.enums import UserRole
from app.db.models import Base, Membership, Project, User
from app.main import app

TEST_DB = "taskmgr_test"
_test_url = settings.database_url.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncGenerator:
    """Create the test database if it is missing, then build the schema once.

    Schema comes from the models rather than the migrations: CI runs the
    migrations separately against an empty database, so pinning them here would
    just test the same thing twice and slow every run down.
    """
    admin = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    await admin.dispose()

    test_engine = create_async_engine(_test_url, pool_pre_ping=True)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    """A clean database per test.

    TRUNCATE ... CASCADE rather than recreating the schema: it is far faster and
    resets the identity sequences, so ids are predictable inside a test.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as setup:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await setup.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        await setup.commit()

    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """An HTTP client whose requests run against the test session.

    The dependency override hands every request the same session the test holds,
    so a row the test just created is visible to the endpoint without a commit
    race between two connections.
    """

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[db_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ factories


@pytest_asyncio.fixture
async def project(session: AsyncSession) -> Project:
    row = Project(key="ketoshop", name="Ketoshop")
    session.add(row)
    await session.commit()
    return row


async def _make_user(
    session: AsyncSession, telegram_id: int, role: UserRole, projects: list[Project]
) -> User:
    user = User(
        telegram_id=telegram_id,
        full_name=f"User {telegram_id}",
        role=role,
        is_active=True,
        can_use_codex=role == UserRole.MANAGER,
    )
    session.add(user)
    await session.flush()
    for p in projects:
        session.add(Membership(user_id=user.id, project_id=p.id, role_in_project=role))
    await session.commit()
    return user


@pytest_asyncio.fixture
async def manager(session: AsyncSession, project: Project, monkeypatch) -> User:
    user = await _make_user(session, 1001, UserRole.MANAGER, [project])
    monkeypatch.setattr(settings, "owner_telegram_id", user.telegram_id)
    return user


@pytest_asyncio.fixture
async def executor(session: AsyncSession, project: Project) -> User:
    return await _make_user(session, 1002, UserRole.EXECUTOR, [project])


@pytest_asyncio.fixture
async def outsider(session: AsyncSession) -> User:
    """Active, but a member of no project — the permission tests' control group."""
    return await _make_user(session, 1003, UserRole.EXECUTOR, [])


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def sync_client() -> TestClient:
    """For the two health endpoints, which take no database session."""
    return TestClient(app)
