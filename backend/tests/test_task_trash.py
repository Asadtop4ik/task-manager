from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        headers=auth(manager),
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
    assert [
        row["id"]
        for row in (await client.get("/api/v1/tasks/trash", headers=auth(manager))).json()
    ] == [task_id]
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


async def test_active_agent_must_be_stopped_before_delete(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project
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
            status="running",
        )
    )
    await session.commit()
    response = await client.delete(f"/api/v1/tasks/{task_id}", headers=auth(manager))
    assert response.status_code == 409
    assert (await session.get(Task, task_id)).deleted_at is None
