from datetime import UTC, datetime, timedelta

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


async def test_qa_project_discussion_requires_qa_flag_and_creates_no_task(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    project.key = "agent-qa"
    project.repo_full_name = "Asadtop4ik/agent-qa"
    project.default_branch = "main"
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_enabled", False)
    payload = {"project_id": project.id, "chat_id": manager.telegram_id}
    disabled = await client.post(
        "/api/v1/project-discussions", json=payload, headers=bot_headers(manager)
    )
    assert disabled.status_code == 409

    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    executor.can_use_codex = True
    await session.commit()
    nonowner = await client.post(
        "/api/v1/project-discussions",
        json=payload | {"chat_id": executor.telegram_id},
        headers=bot_headers(executor),
    )
    assert nonowner.status_code == 403
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")
    created = await client.post(
        "/api/v1/project-discussions", json=payload, headers=bot_headers(manager)
    )
    assert created.status_code == 200 and created.json()["status"] == "idle"
    discussion = await session.get(ProjectDiscussion, created.json()["id"])
    assert discussion is not None and discussion.project_id == project.id
    queued = await client.post(
        f"/api/v1/project-discussions/{discussion.id}/messages",
        json={"text": "Verify the synthetic service"},
        headers=bot_headers(manager),
    )
    assert queued.status_code == 200 and queued.json()["status"] == "queued"
    lease = await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    assert lease.status_code == 200
    assert lease.json()["project_key"] == "agent-qa"
    assert lease.json()["base_branch"] == "main"
    assert (await session.scalars(select(Task))).all() == []


async def test_qa_discussion_lease_refuses_nonowner_and_disabled_flag(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    project.key = "agent-qa"
    project.repo_full_name = "Asadtop4ik/agent-qa"
    project.default_branch = "main"
    executor.can_use_codex = True
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")

    nonowner_row = ProjectDiscussion(
        user_id=executor.id,
        project_id=project.id,
        chat_id=executor.telegram_id,
        status="queued",
        messages=[{"role": "user", "text": "Unauthorized"}],
        pending_text="Unauthorized",
        pending_images=[],
        revision=1,
        notified_revision=0,
    )
    session.add(nonowner_row)
    await session.commit()
    not_leased = await client.post(
        "/api/v1/project-discussions/lease", headers=worker_headers()
    )
    assert not_leased.status_code == 204
    await session.refresh(nonowner_row)
    assert nonowner_row.status == "failed" and nonowner_row.lease_id is None

    owner_row = ProjectDiscussion(
        user_id=manager.id,
        project_id=project.id,
        chat_id=manager.telegram_id,
        status="queued",
        messages=[{"role": "user", "text": "Check QA"}],
        pending_text="Check QA",
        pending_images=[],
        revision=1,
        notified_revision=0,
    )
    session.add(owner_row)
    await session.commit()
    monkeypatch.setattr(settings, "agent_qa_enabled", False)
    disabled = await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    assert disabled.status_code == 204
    await session.refresh(owner_row)
    assert owner_row.status == "failed" and owner_row.lease_id is None


async def test_qa_discussion_result_is_rejected_if_flag_turns_off_after_lease(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    project.key = "agent-qa"
    project.repo_full_name = "Asadtop4ik/agent-qa"
    project.default_branch = "main"
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    queued = await client.post(
        f"/api/v1/project-discussions/{created.json()['id']}/messages",
        json={"text": "Check QA"},
        headers=bot_headers(manager),
    )
    assert queued.status_code == 200
    lease = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    monkeypatch.setattr(settings, "agent_qa_enabled", False)
    result = await client.post(
        f"/api/v1/project-discussions/{created.json()['id']}/result",
        json={
            "revision": lease["revision"],
            "lease_id": lease["lease_id"],
            "thread_id": "thr_qa_disabled",
            "response": "Must be discarded",
        },
        headers=worker_headers(),
    )
    assert result.status_code == 409
    row = await session.get(ProjectDiscussion, created.json()["id"])
    assert row is not None and row.status == "failed" and row.thread_id is None


async def test_qa_discussion_result_is_rejected_for_nonowner_legacy_row(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    executor: User,
    monkeypatch,
) -> None:
    project.key = "agent-qa"
    project.repo_full_name = "Asadtop4ik/agent-qa"
    project.default_branch = "main"
    executor.can_use_codex = True
    await session.commit()
    monkeypatch.setattr(settings, "agent_intake_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "intake_worker_token", "test-worker-token-0123456789abcdef")
    row = ProjectDiscussion(
        user_id=executor.id,
        project_id=project.id,
        chat_id=executor.telegram_id,
        status="running",
        messages=[{"role": "user", "text": "Legacy nonowner QA run"}],
        pending_text="Legacy nonowner QA run",
        pending_images=[],
        lease_id="00000000-0000-4000-8000-000000000001",
        lease_until=datetime.now(UTC) + timedelta(minutes=1),
        revision=1,
        notified_revision=0,
    )
    session.add(row)
    await session.commit()
    response = await client.post(
        f"/api/v1/project-discussions/{row.id}/result",
        json={
            "revision": row.revision,
            "lease_id": row.lease_id,
            "thread_id": "thr_nonowner",
            "response": "Must be discarded",
        },
        headers=worker_headers(),
    )
    assert response.status_code == 409
    await session.refresh(row)
    assert row.status == "failed" and row.lease_id is None and row.thread_id is None


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
        "owner_initiated": True,
        "full_access": False,
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


async def test_other_project_discussion_does_not_get_diagnostics(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project(session, project, monkeypatch)
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id)
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
    assert work["project_key"] == "task-manager"
    assert work["diagnostics_enabled"] is False
    denied = await client.get(
        f"/api/v1/project-discussions/{discussion_id}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert denied.status_code == 404


async def _lease_for_project(
    client: AsyncClient, project: Project, manager: User, key: str, repo: str, branch: str
) -> dict:
    project.key = key
    project.repo_full_name = repo
    project.default_branch = branch
    created = await client.post(
        "/api/v1/project-discussions",
        json={"project_id": project.id, "chat_id": manager.telegram_id},
        headers=bot_headers(manager),
    )
    discussion_id = created.json()["id"]
    await client.post(
        f"/api/v1/project-discussions/{discussion_id}/messages",
        json={"text": "Bazani tekshiring"},
        headers=bot_headers(manager),
    )
    work = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    assert work["project_key"] == key
    return work


async def test_owner_gets_full_diagnostics_for_every_listed_project(
    client: AsyncClient,
    session: AsyncSession,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "ketoshop_diagnostics_enabled", False)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id)
    monkeypatch.setattr(settings, "agent_diagnostics_enabled", True)
    monkeypatch.setattr(settings, "agent_diagnostics_projects", frozenset({"qurbot"}))
    monkeypatch.setattr(settings, "agent_diagnostics_project_ids", {"qurbot": project.id})
    work = await _lease_for_project(
        client, project, manager, "qurbot", "muradjanov-dev/qurbot", "master"
    )
    assert work["diagnostics_enabled"] is True
    context = await client.get(
        f"/api/v1/project-discussions/{work['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert context.status_code == 200
    assert context.json() == {
        "authorized": True,
        "owner_initiated": True,
        "full_access": True,
        "project_key": "qurbot",
        "project_id": project.id,
        "actor_id": manager.id,
        "active": True,
        "revision": work["revision"],
    }


async def test_full_diagnostics_need_the_fixed_project_id_and_catalog_repository(
    client: AsyncClient,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "ketoshop_diagnostics_enabled", False)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id)
    monkeypatch.setattr(settings, "agent_diagnostics_enabled", True)
    monkeypatch.setattr(settings, "agent_diagnostics_projects", frozenset({"qurbot"}))

    # No fixed id configured for the key: no full access.
    monkeypatch.setattr(settings, "agent_diagnostics_project_ids", {})
    missing = await _lease_for_project(
        client, project, manager, "qurbot", "muradjanov-dev/qurbot", "master"
    )
    assert missing["diagnostics_enabled"] is False

    # An id that belongs to some other project row.
    monkeypatch.setattr(
        settings, "agent_diagnostics_project_ids", {"qurbot": project.id + 1000}
    )
    await client.post(
        f"/api/v1/project-discussions/{missing['id']}/result",
        json={
            "revision": missing["revision"],
            "lease_id": missing["lease_id"],
            "thread_id": "thr_a",
            "response": "Tayyor.",
        },
        headers=worker_headers(),
    )
    await client.post(
        f"/api/v1/project-discussions/{missing['id']}/messages",
        json={"text": "Yana"},
        headers=bot_headers(manager),
    )
    other = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    assert other["diagnostics_enabled"] is False
    denied = await client.get(
        f"/api/v1/project-discussions/{other['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": other["lease_id"]},
    )
    assert denied.status_code == 404

    # Right id, but the project row was re-pointed away from the catalog repo.
    monkeypatch.setattr(settings, "agent_diagnostics_project_ids", {"qurbot": project.id})
    project.default_branch = "attacker-branch"
    repointed = await client.get(
        f"/api/v1/project-discussions/{other['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": other["lease_id"]},
    )
    assert repointed.status_code == 404
    project.default_branch = "master"
    allowed = await client.get(
        f"/api/v1/project-discussions/{other['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": other["lease_id"]},
    )
    assert allowed.status_code == 200 and allowed.json()["full_access"] is True


def test_project_ids_setting_parses_a_comma_list_and_ignores_bad_entries() -> None:
    from app.core.config import Settings

    parsed = Settings._parse_project_ids("ketoshop:3, qurbot:5,bad,x:0,y:abc")
    assert parsed == {"ketoshop": 3, "qurbot": 5}
    assert Settings._parse_project_ids("") == {}


async def test_full_diagnostics_need_the_flag_the_listing_and_the_owner(
    client: AsyncClient,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "ketoshop_diagnostics_enabled", False)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id)
    monkeypatch.setattr(settings, "agent_diagnostics_enabled", True)
    monkeypatch.setattr(settings, "agent_diagnostics_projects", frozenset({"kans-shop"}))
    monkeypatch.setattr(settings, "agent_diagnostics_project_ids", {"qurbot": project.id})
    # Not in AGENT_DIAGNOSTICS_PROJECTS.
    work = await _lease_for_project(
        client, project, manager, "qurbot", "muradjanov-dev/qurbot", "master"
    )
    assert work["diagnostics_enabled"] is False
    denied = await client.get(
        f"/api/v1/project-discussions/{work['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": work["lease_id"]},
    )
    assert denied.status_code == 404


async def test_full_diagnostics_switch_off_and_non_owner_are_denied(
    client: AsyncClient,
    project: Project,
    manager: User,
    monkeypatch,
) -> None:
    await ready_project_flags(monkeypatch)
    monkeypatch.setattr(settings, "ketoshop_diagnostics_enabled", False)
    monkeypatch.setattr(settings, "agent_diagnostics_projects", frozenset({"qurbot"}))
    monkeypatch.setattr(settings, "agent_diagnostics_project_ids", {"qurbot": project.id})
    # Listed, but the master switch is off.
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id)
    monkeypatch.setattr(settings, "agent_diagnostics_enabled", False)
    work = await _lease_for_project(
        client, project, manager, "qurbot", "muradjanov-dev/qurbot", "master"
    )
    assert work["diagnostics_enabled"] is False

    # Switch on, listed, but the actor is not the owner.
    monkeypatch.setattr(settings, "agent_diagnostics_enabled", True)
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id + 1)
    await client.post(
        f"/api/v1/project-discussions/{work['id']}/result",
        json={
            "revision": work["revision"],
            "lease_id": work["lease_id"],
            "thread_id": "thr_x",
            "response": "Tayyor.",
        },
        headers=worker_headers(),
    )
    await client.post(
        f"/api/v1/project-discussions/{work['id']}/messages",
        json={"text": "Yana"},
        headers=bot_headers(manager),
    )
    again = (
        await client.post("/api/v1/project-discussions/lease", headers=worker_headers())
    ).json()
    assert again["diagnostics_enabled"] is False
    denied = await client.get(
        f"/api/v1/project-discussions/{again['id']}/diagnostic-context",
        headers=worker_headers() | {"X-Intake-Lease-ID": again["lease_id"]},
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
