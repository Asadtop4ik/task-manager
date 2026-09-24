from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRun, Project, Task, User
from tests.conftest import auth


async def test_owner_metrics_count_tasks_retries_and_stage_times(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    task = Task(project_id=project.id, title="Demo task")
    second = Task(project_id=project.id, title="Another task")
    session.add_all([task, second])
    await session.flush()
    session.add_all(
        [
            AgentRun(
                run_id="failed-attempt",
                task_id=task.id,
                task_revision="a" * 64,
                attempt_index=1,
                repo_full_name="Asadtop4ik/task-manager",
                base_branch="main",
                mode="pr",
                status="failed",
                attempts=1,
                created_at=now - timedelta(minutes=10),
                input_tokens=100,
            ),
            AgentRun(
                run_id="success-attempt",
                task_id=task.id,
                task_revision="a" * 64,
                attempt_index=2,
                repo_full_name="Asadtop4ik/task-manager",
                base_branch="main",
                mode="pr",
                status="deployed",
                attempts=1,
                created_at=now - timedelta(minutes=8),
                runner_started_at=now - timedelta(minutes=7, seconds=50),
                pr_ready_at=now - timedelta(minutes=6, seconds=50),
                merged_at=now - timedelta(minutes=3, seconds=50),
                deployed_at=now,
                input_tokens=200,
                output_tokens=20,
            ),
            AgentRun(
                run_id="cancelled-attempt",
                task_id=second.id,
                task_revision="b" * 64,
                attempt_index=1,
                repo_full_name="Asadtop4ik/task-manager",
                base_branch="main",
                mode="pr",
                status="cancelled",
                attempts=1,
                created_at=now - timedelta(minutes=5),
            ),
        ]
    )
    await session.commit()
    params = {"since": (now - timedelta(hours=1)).isoformat()}
    denied = await client.get(
        "/api/v1/agent-runs/metrics", params=params, headers=auth(executor)
    )
    assert denied.status_code == 403
    response = await client.get(
        "/api/v1/agent-runs/metrics", params=params, headers=auth(manager)
    )
    assert response.status_code == 200
    report = response.json()
    assert report["sampled_runs"] == 2 and not report["enough_data"]
    assert report["deployed"] == 1 and report["failed_attempts"] == 1
    assert report["cancelled_attempts"] == 1 and report["retried"] == 1
    assert report["queue"]["p50_seconds"] == 10
    assert report["implementation"]["p50_seconds"] == 60
    assert report["human_review"]["p50_seconds"] == 180
    assert report["end_to_end"]["p50_seconds"] == 600
    assert report["input_tokens"] == 300 and report["output_tokens"] == 20
