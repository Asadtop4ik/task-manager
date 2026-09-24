import time

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.enums import UserRole
from app.db.models import User
from tests.test_telegram_auth import sign_widget


async def _login(client: AsyncClient, telegram_id: int, name: str = "Asad"):
    payload = sign_widget(
        {"id": telegram_id, "first_name": name, "auth_date": int(time.time())}
    )
    return await client.post("/api/v1/auth/telegram", json=payload)


async def test_unknown_telegram_login_requires_an_invite(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The legacy widget cannot bypass invite-only onboarding."""
    response = await _login(client, 555)
    assert response.status_code == 403
    assert "invitation required" in response.json()["detail"]

    user = await session.scalar(select(User).where(User.telegram_id == 555))
    assert user is None


async def test_bootstrap_admin_is_approved_immediately(
    client: AsyncClient, session: AsyncSession, monkeypatch
) -> None:
    """Without this, nobody could ever approve anybody."""
    monkeypatch.setattr(settings, "admin_telegram_ids", "777")

    response = await _login(client, 777)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.jwt_access_ttl_minutes * 60

    user = await session.scalar(select(User).where(User.telegram_id == 777))
    assert user is not None
    assert user.is_active is True
    assert user.role == UserRole.MANAGER


async def test_login_rejects_a_forged_signature(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/telegram",
        json={
            "id": 999,
            "first_name": "Mallory",
            "auth_date": int(time.time()),
            "hash": "00" * 32,
        },
    )
    assert response.status_code == 401


async def test_refresh_cookie_issues_a_new_access_token(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "admin_telegram_ids", "778")
    login = await _login(client, 778)
    assert login.status_code == 200
    assert "refresh_token" in login.cookies

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


async def test_refresh_stops_working_once_deactivated(
    client: AsyncClient, session: AsyncSession, monkeypatch
) -> None:
    """Deactivating someone has to end their session, not just block new logins."""
    monkeypatch.setattr(settings, "admin_telegram_ids", "779")
    await _login(client, 779)

    user = await session.scalar(select(User).where(User.telegram_id == 779))
    assert user is not None
    user.is_active = False
    await session.commit()

    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_me_returns_the_authenticated_user(client: AsyncClient, manager: User) -> None:
    from tests.conftest import auth

    response = await client.get("/api/v1/auth/me", headers=auth(manager))
    assert response.status_code == 200
    assert response.json()["telegram_id"] == manager.telegram_id


async def test_unauthenticated_requests_are_401(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.get("/api/v1/tasks")).status_code == 401
