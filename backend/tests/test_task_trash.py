import asyncio
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.models import Activity, AgentRun, Project, Task, User
from tests.conftest import auth


async def test_only_owner_can_hide_and_restore_a_task_with_history(
    client: AsyncClient, session: AsyncSession, manager: User, executor: User, project: Project
) -> None:
    created = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Pilot task",
            "source": "bot",
            "source_chat_id": 111,
            "source_message_id": 222,
        },
        headers={
            "X-Service-Token": settings.service_token,
            "X-Acting-User": str(manager.telegram_id),
        },
    )
    task_id = created.json()["id"]
    assert (
        await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(executor))
    ).status_code == 403

    deleted = await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(manager))
    assert deleted.status_code == 200 and deleted.json()["deleted_at"] is not None
    assert (
        await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))
    ).status_code == 404
    assert (await client.get("/api/v1/tasks", headers=auth(manager))).json()["total"] == 0
    trash = (await client.get("/api/v1/tasks/trash", headers=auth(manager))).json()
    assert trash["total"] == 1
    assert [row["id"] for row in trash["items"]] == [task_id]
    assert (await client.get("/api/v1/tasks/trash", headers=auth(executor))).status_code == 403
    assert await session.get(Task, task_id) is not None
    events = (await session.scalars(select(Activity).where(Activity.task_id == task_id))).all()
    assert [row.kind for row in events] == ["created", "deleted"]

    headers = {"X-Agent-Worker-Token": settings.service_token}
    notices = (await client.get("/api/v1/tasks/card-sync/pending", headers=headers)).json()
    assert len(notices) == 1 and notices[0]["kind"] == "deleted"
    assert notices[0]["chat_id"] == 111 and notices[0]["message_id"] == 222
    assert (
        await client.post(
            f"/api/v1/tasks/card-sync/{notices[0]['event_id']}/notified", headers=headers
        )
    ).status_code == 204

    restored = await client.post(f"/api/v1/tasks/{task_id}/restore", headers=auth(manager))
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None
    assert (await client.get("/api/v1/tasks", headers=auth(manager))).json()["total"] == 1
    notices = (await client.get("/api/v1/tasks/card-sync/pending", headers=headers)).json()
    assert len(notices) == 1 and notices[0]["kind"] == "restored"


@pytest.mark.parametrize("agent_status", ["running", "pr_opened"])
async def test_active_agent_must_be_stopped_before_delete(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    agent_status: str,
) -> None:
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Busy"},
            headers=auth(manager),
        )
    ).json()["id"]
    session.add(
        AgentRun(
            run_id="00000000-0000-0000-0000-000000000099",
            task_id=task_id,
            task_revision="a" * 64,
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            status=agent_status,
        )
    )
    await session.commit()
    response = await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(manager))
    assert response.status_code == 409
    assert (await session.get(Task, task_id)).deleted_at is None


async def test_delete_lock_prevents_a_concurrent_agent_dispatch(
    client: AsyncClient, session: AsyncSession, engine, manager: User, project: Project
) -> None:
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Delete race"},
            headers=auth(manager),
        )
    ).json()["id"]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as deleting:
        task = await deleting.scalar(select(Task).where(Task.id == task_id).with_for_update())
        assert task is not None
        task.deleted_at = datetime.now(UTC)
        pending = asyncio.create_task(
            client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        await deleting.commit()
        response = await asyncio.wait_for(pending, timeout=2)
    assert response.status_code == 404
    assert (
        await session.scalars(select(AgentRun).where(AgentRun.task_id == task_id))
    ).all() == []


async def test_delete_lock_prevents_a_concurrent_task_update(
    client: AsyncClient, session: AsyncSession, engine, manager: User, project: Project
) -> None:
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Original"},
            headers=auth(manager),
        )
    ).json()["id"]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as deleting:
        task = await deleting.scalar(select(Task).where(Task.id == task_id).with_for_update())
        assert task is not None
        task.deleted_at = datetime.now(UTC)
        pending = asyncio.create_task(
            client.patch(
                f"/api/v1/tasks/{task_id}",
                json={"title": "Changed after delete"},
                headers=auth(manager),
            )
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        await deleting.commit()
        response = await asyncio.wait_for(pending, timeout=2)
    assert response.status_code == 404
    stored = await session.get(Task, task_id)
    assert stored is not None
    await session.refresh(stored)
    assert stored.title == "Original"


async def test_only_bot_service_can_set_telegram_card_coordinates(
    client: AsyncClient, manager: User, project: Project
) -> None:
    create = {"project_id": project.id, "title": "Web task"}
    for extra in (
        {"source": "bot"},
        {"source_chat_id": 123},
        {"source_message_id": 456},
    ):
        response = await client.post(
            "/api/v1/tasks", json=create | extra, headers=auth(manager)
        )
        assert response.status_code == 401
    task_id = (await client.post("/api/v1/tasks", json=create, headers=auth(manager))).json()[
        "id"
    ]
    assert (
        await client.post(
            f"/api/v1/tasks/{task_id}/card",
            json={"chat_id": 123, "message_id": 456},
            headers=auth(manager),
        )
    ).status_code == 401
    service_headers = {
        "X-Service-Token": settings.service_token,
        "X-Acting-User": str(manager.telegram_id),
    }
    assert (
        await client.post(
            f"/api/v1/tasks/{task_id}/card",
            json={"chat_id": 123, "message_id": 456},
            headers=service_headers,
        )
    ).status_code == 200


async def test_trash_pages_remain_reachable(
    client: AsyncClient, manager: User, project: Project
) -> None:
    for title in ("first", "second", "third"):
        task = await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": title},
            headers=auth(manager),
        )
        assert (
            await client.delete(f"/api/v1/tasks/{task.json()['id']}", headers=auth(manager))
        ).status_code == 200
    page = (
        await client.get("/api/v1/tasks/trash?limit=1&offset=2", headers=auth(manager))
    ).json()
    assert page["total"] == 3 and page["offset"] == 2
    assert len(page["items"]) == 1
