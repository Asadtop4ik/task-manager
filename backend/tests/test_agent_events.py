from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentIntake, AgentRun, Project, ProjectDiscussion, Task, User
from app.services import agent_events
from tests.conftest import auth


async def test_owner_sees_all_three_flows_without_prompts(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
) -> None:
    task = Task(project_id=project.id, title="Private task title")
    session.add(task)
    await session.flush()
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000001",
        task_id=task.id,
        task_revision="a" * 64,
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        status="running",
    )
    intake = AgentIntake(
        user_id=manager.id,
        project_id=project.id,
        chat_id=manager.telegram_id,
        text="private intake prompt",
        mode="pr",
        status="analyzing",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    discussion = ProjectDiscussion(
        user_id=manager.id,
        project_id=project.id,
        chat_id=manager.telegram_id,
        status="running",
        messages=[{"role": "user", "text": "private discussion prompt"}],
    )
    session.add_all([run, intake, discussion])
    await session.flush()
    for subject in (run, intake, discussion):
        agent_events.record(session, subject)
    await session.commit()

    url = "/api/v1/agent-runs/events/recent"
    denied = await client.get(url, headers=auth(executor))
    assert denied.status_code == 403
    response = await client.get(url, headers=auth(manager))
    assert response.status_code == 200
    events = response.json()
    assert [event["flow"] for event in events] == ["discussion", "intake", "coding"]
    assert events[-1]["task_id"] == task.id
    assert "private intake prompt" not in response.text
    assert "private discussion prompt" not in response.text
    older = await client.get(url, params={"before_id": events[0]["id"]}, headers=auth(manager))
    assert [event["flow"] for event in older.json()] == ["intake", "coding"]
