from httpx import AsyncClient

from app.core.config import settings
from app.db.models import User


def service_headers(telegram_id: int) -> dict[str, str]:
    return {
        "X-Service-Token": settings.service_token,
        "X-Acting-User": str(telegram_id),
    }


async def test_bot_acts_as_a_real_user(client: AsyncClient, manager: User) -> None:
    """The bot is a client, not a user.

    It presents the service token and names the telegram_id it is acting for, so
    attribution and permission checks run through exactly the same code as a web
    request instead of a parallel set of bot-only rules.
    """
    response = await client.get(
        "/api/v1/auth/me", headers=service_headers(manager.telegram_id)
    )
    assert response.status_code == 200
    assert response.json()["id"] == manager.id


async def test_a_wrong_service_token_is_rejected(client: AsyncClient, manager: User) -> None:
    headers = service_headers(manager.telegram_id) | {"X-Service-Token": "not-the-token"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_service_token_without_an_acting_user_is_a_400(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/auth/me", headers={"X-Service-Token": settings.service_token}
    )
    assert response.status_code == 400


async def test_bot_cannot_act_as_an_unknown_telegram_id(client: AsyncClient) -> None:
    assert (
        await client.get("/api/v1/auth/me", headers=service_headers(424242))
    ).status_code == 401
