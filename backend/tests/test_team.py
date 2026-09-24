from urllib.parse import parse_qs, urlsplit

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import auth as auth_routes
from app.core.config import settings
from app.db.enums import UserRole
from app.db.models import JoinRequest, Membership, Project, User
from tests.conftest import auth


def bot_headers(telegram_id: int) -> dict[str, str]:
    return {
        "X-Service-Token": settings.service_token,
        "X-Acting-User": str(telegram_id),
    }


async def test_invite_requires_owner_and_approval_assigns_projects(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    executor: User,
    project: Project,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "bot_username", "mn_taskmanagerbot")
    assert (
        await client.post("/api/v1/team/invites", headers=auth(executor))
    ).status_code == 403
    invite = await client.post("/api/v1/team/invites", headers=auth(manager))
    assert invite.status_code == 201
    token = invite.json()["url"].split("invite_", 1)[1]
    payload = {
        "invite_token": token,
        "telegram_id": 987654321,
        "first_name": "New",
        "last_name": "Member",
        "username": "newmember",
    }
    assert (
        await client.post(
            "/api/v1/team/join-requests",
            json=payload,
            headers=bot_headers(123),
        )
    ).status_code == 403
    first = await client.post(
        "/api/v1/team/join-requests", json=payload, headers=bot_headers(987654321)
    )
    assert first.status_code == 200
    assert first.json()["notify_owner"] is True
    request_id = first.json()["id"]
    duplicate = await client.post(
        "/api/v1/team/join-requests", json=payload, headers=bot_headers(987654321)
    )
    assert duplicate.json()["id"] == request_id
    assert duplicate.json()["notify_owner"] is False

    assert (
        await client.post(
            f"/api/v1/team/join-requests/{request_id}/approve",
            json={"project_ids": [project.id]},
            headers=auth(executor),
        )
    ).status_code == 403
    approved = await client.post(
        f"/api/v1/team/join-requests/{request_id}/approve",
        json={"project_ids": [project.id]},
        headers=auth(manager),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert (
        await client.post(
            f"/api/v1/team/join-requests/{request_id}/approve",
            json={"project_ids": [project.id]},
            headers=auth(manager),
        )
    ).status_code == 200
    user = await session.scalar(select(User).where(User.telegram_id == 987654321))
    assert user is not None and user.is_active and not user.can_use_codex
    projects = (await client.get("/api/v1/projects", headers=auth(user))).json()
    assert [item["id"] for item in projects] == [project.id]
    assert (
        await client.post(
            "/api/v1/team/join-requests",
            json=payload | {"telegram_id": 987654322},
            headers=bot_headers(987654322),
        )
    ).status_code == 410


async def test_rejected_invite_does_not_create_an_active_user(
    client: AsyncClient, session: AsyncSession, manager: User, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "bot_username", "mn_taskmanagerbot")
    invite = await client.post("/api/v1/team/invites", headers=auth(manager))
    token = invite.json()["url"].split("invite_", 1)[1]
    requested = await client.post(
        "/api/v1/team/join-requests",
        json={"invite_token": token, "telegram_id": 999, "first_name": "Rejected"},
        headers=bot_headers(999),
    )
    request_id = requested.json()["id"]
    rejected = await client.post(
        f"/api/v1/team/join-requests/{request_id}/reject", headers=auth(manager)
    )
    assert rejected.json()["status"] == "rejected"
    assert await session.scalar(select(User).where(User.telegram_id == 999)) is None
    row = await session.get(JoinRequest, request_id)
    assert row is not None and row.decided_by_id == manager.id


async def test_only_one_non_owner_gets_codex_access(
    client: AsyncClient, session: AsyncSession, manager: User, executor: User, outsider: User
) -> None:
    grant = await client.put(
        f"/api/v1/team/members/{executor.id}/codex-access",
        json={"enabled": True},
        headers=auth(manager),
    )
    assert grant.status_code == 200
    assert grant.json()["can_use_codex"] is True
    assert (
        await client.put(
            f"/api/v1/team/members/{outsider.id}/codex-access",
            json={"enabled": True},
            headers=auth(manager),
        )
    ).status_code == 409
    assert (
        await client.put(
            f"/api/v1/team/members/{executor.id}/codex-access",
            json={"enabled": False},
            headers=auth(outsider),
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/v1/users/{executor.id}",
            json={"is_active": False},
            headers=auth(manager),
        )
    ).status_code == 200
    # A stale grant on an inactive account must not occupy the active seat.
    await session.refresh(executor)
    executor.can_use_codex = True
    await session.commit()
    replacement = await client.put(
        f"/api/v1/team/members/{outsider.id}/codex-access",
        json={"enabled": True},
        headers=auth(manager),
    )
    assert replacement.status_code == 200


async def test_reapproval_replaces_old_role_codex_and_projects(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "bot_username", "mn_taskmanagerbot")
    old_project = Project(key="old-client", name="Old Client")
    session.add(old_project)
    await session.flush()
    former = User(
        telegram_id=7654,
        full_name="Former manager",
        role=UserRole.MANAGER,
        is_active=False,
        can_use_codex=True,
    )
    session.add(former)
    await session.flush()
    session.add(
        Membership(
            user_id=former.id,
            project_id=old_project.id,
            role_in_project=UserRole.MANAGER,
        )
    )
    await session.commit()

    invite = await client.post("/api/v1/team/invites", headers=auth(manager))
    token = invite.json()["url"].split("invite_", 1)[1]
    requested = await client.post(
        "/api/v1/team/join-requests",
        json={
            "invite_token": token,
            "telegram_id": former.telegram_id,
            "first_name": "Former",
        },
        headers=bot_headers(former.telegram_id),
    )
    approved = await client.post(
        f"/api/v1/team/join-requests/{requested.json()['id']}/approve",
        json={"project_ids": [project.id]},
        headers=auth(manager),
    )
    assert approved.status_code == 200
    await session.refresh(former)
    assert former.role == UserRole.EXECUTOR
    assert former.can_use_codex is False
    memberships = (
        await session.scalars(select(Membership).where(Membership.user_id == former.id))
    ).all()
    assert [membership.project_id for membership in memberships] == [project.id]


async def test_magic_link_is_single_use_and_requires_an_active_member(
    client: AsyncClient, manager: User, monkeypatch
) -> None:
    class FakeRedis:
        def __init__(self):
            self.values: dict[str, str] = {}

        async def set(self, key: str, value: str, *, ex: int) -> None:
            assert ex == 300
            self.values[key] = value

        async def getdel(self, key: str) -> str | None:
            return self.values.pop(key, None)

    redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "get_redis", lambda: redis)
    assert (
        await client.post("/api/v1/auth/magic/request", headers=bot_headers(999999))
    ).status_code == 403
    link = await client.post(
        "/api/v1/auth/magic/request", headers=bot_headers(manager.telegram_id)
    )
    assert link.status_code == 200
    token = parse_qs(urlsplit(link.json()["url"]).fragment)["token"][0]
    first = await client.post("/api/v1/auth/magic/redeem", json={"token": token})
    assert first.status_code == 200
    assert "refresh_token" in first.cookies
    assert (
        await client.post("/api/v1/auth/magic/redeem", json={"token": token})
    ).status_code == 401
