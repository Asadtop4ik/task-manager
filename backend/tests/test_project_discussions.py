from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import project_discussions
from app.core.config import settings
from app.db.models import AgentEvent, Project, ProjectDiscussion, Task, User


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
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")


async def test_private_discussion_requires_codex_access_and_creates_no_task(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    payload = {"project_id": project.id, "chat_id": manager.telegram_id}
    assert (
        await client.post(
            "/api/v1/project-discussions", json=payload, headers=bot_headers(executor)
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/project-discussions", json=payload, headers=bot_headers(manager)
        )
    ).status_code == 200
    created = await client.post(
        "/api/v1/project-discussions", json=payload, headers=bot_headers(manager)
    )
    assert created.status_code == 200 and created.json()["status"] == "idle"
    rows = (await session.scalars(select(ProjectDiscussion))).all()
    assert len(rows) == 1
    assert (await session.scalars(select(Task))).all() == []


async def test_messages_resume_one_thread_and_deny_stale_results(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    discussion_id = created.json()["id"]
    first = await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={"text": "Buyurtma oqimi qanday ishlaydi?"},
        headers=bot_headers(manager),
    )
    assert first.status_code == 200 and first.json()["status"] == "queued"
    lease = await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    work = lease.json()
    assert work["thread_id"] is None and work["base_branch"] == "main"
    assert (
        await client.post(
            f"/api/v1/project-discussions/{discussion_id}/messages",
            json={"text": "Ikkinchi savol"},
            headers=bot_headers(manager),
        )
    ).status_code == 409
    result = await client.post(
        f"/api/v1/project-discussions/{discussion_id}/result",
        json={
            "revision": work["revision"],
            "lease_id": work["lease_id"],
            "thread_id": "thr_test123",
            "response": "Oqim shunday ishlaydi.",
        },
        headers=worker_headers(),
    )
    assert result.status_code == 200 and result.json()["status"] == "idle"
    events = (
        await session.scalars(
            select(AgentEvent.status)
            .where(AgentEvent.project_discussion_id == discussion_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events == ["idle", "queued", "running", "answered"]
    assert (
        await client.post(
            f"/api/v1/project-discussions/{discussion_id}/result",
            json={
                "revision": work["revision"],
                "lease_id": work["lease_id"],
                "thread_id": "thr_test123",
                "response": "Takror",
            },
            headers=worker_headers(),
        )
    ).status_code == 409
    notices = await client.get(
        "/api/v1/project-discussions/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert (
        notices.status_code == 200
        and notices.json()[0]["response"] == "Oqim shunday ishlaydi."
    )
    ack = await client.post(
        f"/api/v1/project-discussions/{discussion_id}/notified",
        params={"revision": work["revision"]},
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert ack.status_code == 200
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={"text": "Davomi-chi?"},
        headers=bot_headers(manager),
    )
    resumed = await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    assert resumed.json()["thread_id"] == "thr_test123"
    assert (await session.scalars(select(Task))).all() == []


async def test_ketoshop_diagnostic_context_is_owner_project_and_active_turn_only(
    client: AsyncClient,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    project.key = "ketoshop"
    project.repo_full_name = "muradjanov-dev/ketoshop"
    project.default_branch = "master"
    await ready_project_flags(monkeypatch)
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    discussion_id = created.json()["id"]
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={"text": "Buyurtmalarni tekshiring"},
        headers=bot_headers(manager),
    )
    work = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    assert work["project_key"] == "ketoshop" and work["diagnostics_enabled"] is True
    context = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert context.status_code == 200
    assert context.json() == {
        "authorized": True,
        "project_key": "ketoshop",
        "project_id": project.id,
        "actor_id": manager.id,
        "active": True,
        "revision": work["revision"],
    }
    missing_capability = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers(),
    )
    wrong_capability = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": "guessable-wrong"},
    )
    assert missing_capability.status_code == wrong_capability.status_code == 404

    completed = await client.post(
        f"/api/v1/project-discussions/{discussion_id}/result",
        json={
            "revision": work["revision"],
            "lease_id": work["lease_id"],
            "thread_id": "thr_diag",
            "response": "Tayyor.",
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200
    inactive = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert inactive.status_code == 404


async def test_nonowner_ketoshop_discussion_does_not_get_diagnostics(
    client: AsyncClient,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    project.key = "ketoshop"
    project.repo_full_name = "muradjanov-dev/ketoshop"
    project.default_branch = "master"
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id + 1)
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    discussion_id = created.json()["id"]
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={"text": "Tekshiring"},
        headers=bot_headers(manager),
    )
    work = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    assert work["diagnostics_enabled"] is False
    denied = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert denied.status_code == 404


async def ready_project_flags(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_public_enabled", True)
    monkeypatch.setattr(settings, "ketoshop_diagnostics_enabled", True)
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")


async def test_image_is_leased_only_to_worker_and_reset_clears_context(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    discussion_id = created.json()["id"]
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={
            "text": "Rasmni tushuntir",
            "images": [{"file_id": "telegram-image", "mime": "image/png", "size": 12}],
        },
        headers=bot_headers(manager),
    )
    lease = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()

    async def fake_image(file_id: str, mime: str, size: int | None) -> bytes:
        assert (file_id, mime, size) == ("telegram-image", "image/png", 12)
        return b"\x89PNG\r\n\x1a\nxxxx"

    monkeypatch.setattr(project_discussions, "telegram_image", fake_image)
    url = f"/api/v1/project-discussions/{discussion_id}/images/0"
    assert (await client.get(url, headers=worker_headers())).status_code == 409
    image = await client.get(
        url, headers=worker_headers() | {"X-Intake-Lease-ID": lease["lease_id"]}
    )
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/result",
        json={
            "revision": lease["revision"],
            "lease_id": lease["lease_id"],
            "thread_id": "thr_image",
            "response": "Bu rasmda buyurtma bor.",
        },
        headers=worker_headers(),
    )
    reset = await client.post(
        f"/api/v1/project-discussions/{discussion_id}/reset", headers=bot_headers(manager)
    )
    assert reset.status_code == 200 and reset.json()["messages"] == []
