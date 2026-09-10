from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models import User
from tests.conftest import auth


async def test_pending_queue_is_manager_only(
    client: AsyncClient, manager: User, executor: User, session: AsyncSession
) -> None:
    session.add(User(telegram_id=9001, full_name="Waiting", role=UserRole.EXECUTOR))
    await session.commit()

    assert (
        await client.get("/api/v1/users/pending", headers=auth(executor))
    ).status_code == 403

    pending = (await client.get("/api/v1/users/pending", headers=auth(manager))).json()
    assert [u["telegram_id"] for u in pending] == [9001]


async def test_approving_lets_someone_in(
    client: AsyncClient, manager: User, session: AsyncSession
) -> None:
    waiting = User(telegram_id=9002, full_name="Waiting", role=UserRole.EXECUTOR)
    session.add(waiting)
    await session.commit()

    # Before approval the account exists but is refused with 403, not 401 — the
    # UI needs to tell them to wait rather than to log in again.
    assert (await client.get("/api/v1/auth/me", headers=auth(waiting))).status_code == 403

    approved = await client.patch(
        f"/api/v1/users/{waiting.id}", json={"is_active": True}, headers=auth(manager)
    )
    assert approved.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=auth(waiting))).status_code == 200


async def test_a_manager_cannot_deactivate_themselves(
    client: AsyncClient, manager: User
) -> None:
    """The obvious way to lock everyone out of the approval queue."""
    response = await client.patch(
        f"/api/v1/users/{manager.id}", json={"is_active": False}, headers=auth(manager)
    )
    assert response.status_code == 400


async def test_user_list_excludes_pending_accounts(
    client: AsyncClient, manager: User, executor: User, session: AsyncSession
) -> None:
    session.add(User(telegram_id=9003, full_name="Waiting", role=UserRole.EXECUTOR))
    await session.commit()

    listed = (await client.get("/api/v1/users", headers=auth(manager))).json()
    assert {u["telegram_id"] for u in listed} == {manager.telegram_id, executor.telegram_id}
