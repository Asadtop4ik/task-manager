from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_intakes, agent_runs
from app.api.v1.agent_intakes import _description
from app.core.config import settings
from app.db.models import AgentEvent, AgentIntake, AgentRun, Attachment, Project, Task, User
from tests.conftest import auth


def bot_headers(user: User) -> dict[str, str]:
    return {"X-Service-Token": settings.service_token, "X-Acting-User": str(user.telegram_id)}


def worker_headers() -> dict[str, str]:
    return {"X-Intake-Worker-Token": settings.intake_worker_token}


async def ready_project(session: AsyncSession, project: Project, monkeypatch) -> None:
    project.key = "task-manager"
    project.repo_full_name = "Asadtop4ik/task-manager"
    project.default_branch = "main"
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "intake_worker_token", "test-intake-worker-token-0123456789")


async def test_public_project_can_clarify_pr_but_not_fast(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    project.key = "kans-shop"
    project.repo_full_name = "muradjanov-dev/kans-shop"
    project.default_branch = "main"
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_public_enabled", True)
    payload = {
        "project_id": project.id,
        "text": "kans-shop: @codex Show the empty catalog message",
        "chat_id": manager.telegram_id,
    }
    fast = await client.post(
        "/api/v1/agent-intakes",
        json=payload | {"mode": "fast"},
        headers=bot_headers(manager),
    )
    assert fast.status_code == 409
    pr = await client.post(
        "/api/v1/agent-intakes",
        json=payload | {"mode": "pr"},
        headers=bot_headers(manager),
    )
    assert pr.status_code == 201 and pr.json()["status"] == "queued"
    assert (await session.scalars(select(Task))).all() == []


async def test_only_approved_bot_actor_can_start_one_intake(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    payload = {
        "project_id": project.id,
        "text": "Make the board clearer",
        "mode": "fast",
        "chat_id": manager.telegram_id,
    }
    assert (
        await client.post("/api/v1/agent-intakes", json=payload, headers=auth(manager))
    ).status_code == 401
    assert (
        await client.post("/api/v1/agent-intakes", json=payload, headers=bot_headers(executor))
    ).status_code == 403
    created = await client.post(
        "/api/v1/agent-intakes", json=payload, headers=bot_headers(manager)
    )
    assert created.status_code == 201
    assert created.json()["status"] == "queued"
    assert (await session.scalars(select(Task))).all() == []
    assert (
        await client.post("/api/v1/agent-intakes", json=payload, headers=bot_headers(manager))
    ).status_code == 409


async def test_questions_then_confirmation_persists_brief_and_image_once(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/agent-intakes",
        json={
            "project_id": project.id,
            "text": "Fix this board screenshot",
            "mode": "fast",
            "chat_id": manager.telegram_id,
            "images": [{"file_id": "telegram-photo-1", "mime": "image/jpeg", "size": 1024}],
        },
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    assert (await session.scalars(select(Task))).all() == []

    lease = await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())
    assert lease.status_code == 200
    assert (
        lease.json()["id"] == intake_id
        and lease.json()["images"][0]["file_id"] == "telegram-photo-1"
    )
    questions = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/result",
        json={
            "revision": lease.json()["revision"],
            "lease_id": lease.json()["lease_id"],
            "status": "needs_answers",
            "questions": ["Qaysi yozuv?"],
        },
        headers=worker_headers(),
    )
    assert questions.status_code == 200 and questions.json()["status"] == "needs_answers"
    events = (
        await session.scalars(
            select(AgentEvent.status)
            .where(AgentEvent.agent_intake_id == intake_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events == ["queued", "analyzing", "needs_answers"]
    notices = (
        await client.get(
            "/api/v1/agent-intakes/notifications",
            headers={"X-Agent-Worker-Token": settings.service_token},
        )
    ).json()
    assert len(notices) == 1 and notices[0]["questions"] == ["Qaysi yozuv?"]
    assert (
        await client.post(
            f"/api/v1/agent-intakes/{intake_id}/notified",
            json={"revision": notices[0]["revision"], "message_id": 77},
            headers={"X-Agent-Worker-Token": settings.service_token},
        )
    ).status_code == 204

    answer = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/answer",
        json={"text": "O‘chirilganlar"},
        headers=bot_headers(manager),
    )
    assert answer.status_code == 200 and answer.json()["status"] == "queued"
    lease = await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())
    assert lease.json()["answer_text"] == "O‘chirilganlar"
    brief = {
        "title": "Add a board tooltip",
        "goal": "Clarify the trash link",
        "acceptance": ["Tooltip is visible"],
        "assumptions": ["Keep current behavior"],
    }
    result = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/result",
        json={
            "revision": lease.json()["revision"],
            "lease_id": lease.json()["lease_id"],
            "status": "ready",
            "brief": brief,
        },
        headers=worker_headers(),
    )
    assert result.status_code == 200 and result.json()["status"] == "ready"
    assert (await session.scalars(select(Task))).all() == []

    first = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
    )
    second = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] is True and second.json()["created"] is False
    assert first.json()["task"]["id"] == second.json()["task"]["id"]
    assert first.json()["mode"] == "fast"
    task = await session.get(Task, first.json()["task"]["id"])
    assert task is not None and "Tooltip is visible" in task.description
    files = (
        await session.scalars(select(Attachment).where(Attachment.task_id == task.id))
    ).all()
    assert len(files) == 1 and files[0].tg_file_id == "telegram-photo-1"


async def test_expiry_and_stale_worker_result_never_create_a_task(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/agent-intakes",
        json={"project_id": project.id, "text": "Ambiguous", "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    lease = (await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())).json()
    row = await session.get(AgentIntake, intake_id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    stale = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/result",
        json={
            "revision": lease["revision"],
            "lease_id": lease["lease_id"],
            "status": "ready",
            "brief": {"title": "No", "goal": "No", "acceptance": ["No"]},
        },
        headers=worker_headers(),
    )
    assert stale.status_code == 409
    assert (
        await client.post(
            f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
        )
    ).status_code == 404
    assert (await session.scalars(select(Task))).all() == []


async def test_failed_intake_can_be_confirmed_only_as_pr_once(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User, monkeypatch
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/agent-intakes",
        json={
            "project_id": project.id,
            "text": "Need a screenshot fix",
            "mode": "fast",
            "chat_id": manager.telegram_id,
        },
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    lease = (await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())).json()
    failed = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/result",
        json={
            "revision": lease["revision"],
            "lease_id": lease["lease_id"],
            "status": "failed",
            "error": "read-only analysis timed out",
        },
        headers=worker_headers(),
    )
    assert failed.status_code == 200
    assert (
        await client.post(
            f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
        )
    ).status_code == 409
    first = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/confirm",
        json={"fallback_pr": True},
        headers=bot_headers(manager),
    )
    repeat = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
    )
    assert first.status_code == repeat.status_code == 200
    assert first.json()["mode"] == repeat.json()["mode"] == "pr"
    assert first.json()["created"] is True and repeat.json()["created"] is False


async def test_images_are_only_available_to_the_matching_work_and_run(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User, monkeypatch
) -> None:
    await ready_project(session, project, monkeypatch)
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")
    created = await client.post(
        "/api/v1/agent-intakes",
        json={
            "project_id": project.id,
            "text": "Fix the image",
            "chat_id": manager.telegram_id,
            "images": [{"file_id": "photo-123", "mime": "image/jpeg", "size": 4}],
        },
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    lease = (await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())).json()
    image = b"\xff\xd8\xff\xd9"

    async def fake_image(file_id: str, mime: str, expected_size: int | None) -> bytes:
        assert file_id == "photo-123" and mime == "image/jpeg" and expected_size == 4
        return image

    monkeypatch.setattr(agent_intakes, "telegram_image", fake_image)
    monkeypatch.setattr(agent_runs, "telegram_image", fake_image)
    preflight = await client.get(
        f"/api/v1/agent-intakes/{intake_id}/images/0",
        headers=worker_headers() | {"X-Intake-Lease-ID": lease["lease_id"]},
    )
    assert preflight.status_code == 200 and preflight.content == image
    assert (
        await client.get(
            f"/api/v1/agent-intakes/{intake_id}/images/0", headers=worker_headers()
        )
    ).status_code == 404
    result = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/result",
        json={
            "revision": lease["revision"],
            "lease_id": lease["lease_id"],
            "status": "ready",
            "brief": {
                "title": "Fix image",
                "goal": "Match screenshot",
                "acceptance": ["Matches"],
            },
        },
        headers=worker_headers(),
    )
    assert result.status_code == 200
    confirmed = await client.post(
        f"/api/v1/agent-intakes/{intake_id}/confirm", json={}, headers=bot_headers(manager)
    )
    task_id = confirmed.json()["task"]["id"]
    run_id = "00000000-0000-0000-0000-000000000123"
    session.add(
        AgentRun(
            run_id=run_id,
            task_id=task_id,
            task_revision="a" * 64,
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            mode="pr",
            status="pending",
        )
    )
    await session.commit()
    url = f"/api/v1/agent-runs/{run_id}/images"
    assert (await client.get(url)).status_code == 401
    listed = await client.get(url, headers={"X-Agent-Callback-Token": "test-callback-token"})
    assert listed.status_code == 200 and len(listed.json()) == 1
    attachment_id = listed.json()[0]["id"]
    downloaded = await client.get(
        f"{url}/{attachment_id}", headers={"X-Agent-Callback-Token": "test-callback-token"}
    )
    assert downloaded.status_code == 200 and downloaded.content == image


async def test_expired_worker_lease_reports_failure_instead_of_stalling(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User, monkeypatch
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/agent-intakes",
        json={
            "project_id": project.id,
            "text": "Need context",
            "chat_id": manager.telegram_id,
        },
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    row = await session.get(AgentIntake, intake_id)
    assert row is not None
    row.status = "analyzing"
    row.attempts = 2
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    assert (
        await client.post("/api/v1/agent-intakes/lease", headers=worker_headers())
    ).status_code == 204
    await session.rollback()  # The production request closes its session here.
    await session.refresh(row)
    assert row.status == "failed" and row.error == "Codex intake worker timed out"
    events = (
        await session.scalars(
            select(AgentEvent.status)
            .where(AgentEvent.agent_intake_id == intake_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events[-1] == "failed"
    notices = (
        await client.get(
            "/api/v1/agent-intakes/notifications",
            headers={"X-Agent-Worker-Token": settings.service_token},
        )
    ).json()
    assert len(notices) == 1 and notices[0]["status"] == "failed"


async def test_other_users_cannot_read_or_confirm_an_intake(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    executor.can_use_codex = True
    await session.commit()
    created = await client.post(
        "/api/v1/agent-intakes",
        json={"project_id": project.id, "text": "Owner task", "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    intake_id = created.json()["id"]
    assert (
        await client.get(f"/api/v1/agent-intakes/{intake_id}", headers=bot_headers(executor))
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/agent-intakes/{intake_id}/confirm",
            json={},
            headers=bot_headers(executor),
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/agent-intakes",
            json={
                "project_id": project.id,
                "text": "Wrong chat",
                "chat_id": manager.telegram_id,
            },
            headers=bot_headers(executor),
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/agent-intakes",
            json={
                "project_id": project.id,
                "text": "Big image",
                "chat_id": executor.telegram_id,
                "images": [
                    {"file_id": "x", "mime": "image/jpeg", "size": 20 * 1024 * 1024 + 1}
                ],
            },
            headers=bot_headers(executor),
        )
    ).status_code == 422


def test_discussion_task_description_keeps_approved_brief_without_raw_transcript() -> None:
    row = SimpleNamespace(
        text="@codex Quyidagi loyiha suhbatidagi eski xabarlar: " + "old log " * 900,
        answer_text=None,
        brief={
            "title": "Opus 5.5 yangilash",
            "goal": "Model va xarajatni yangilash",
            "acceptance": [".env.example o‘zgarmasin"],
            "assumptions": [],
        },
    )
    title, description = _description(row, False)
    assert title == "Opus 5.5 yangilash"
    assert ".env.example o‘zgarmasin" in description
    assert "old log" not in description
