import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_runs
from app.core.config import settings
from app.db.models import AgentEvent, AgentRun, Project, User
from tests.conftest import auth


async def _ready_project(session: AsyncSession, project: Project) -> None:
    project.key = "task-manager"
    project.repo_full_name = "Asadtop4ik/task-manager"
    project.default_branch = "main"
    await session.commit()


async def test_public_project_can_dispatch_pr_but_not_fast(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    project.key = "qurbot"
    project.repo_full_name = "muradjanov-dev/qurbot"
    project.default_branch = "master"
    await session.commit()
    _credentials(monkeypatch)
    monkeypatch.setattr(
        settings,
        "github_agent_allowed_repos",
        "Asadtop4ik/task-manager,muradjanov-dev/qurbot",
    )
    monkeypatch.setattr(settings, "github_public_agent_token", "public-read-token")

    async def fake_dispatch(run, task) -> int:
        assert run.repo_full_name == "muradjanov-dev/qurbot"
        assert run.base_branch == "master"
        assert task.project.key == "qurbot"
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Clarify catalog"},
            headers=auth(manager),
        )
    ).json()["id"]
    start_url = f"/api/v1/agent-runs/tasks/{task_id}"
    disabled = await client.post(start_url, json={"mode": "pr"}, headers=auth(manager))
    assert disabled.status_code == 503
    monkeypatch.setattr(settings, "agent_public_enabled", True)
    fast = await client.post(start_url, json={"mode": "fast"}, headers=auth(manager))
    assert fast.status_code == 409
    pr = await client.post(start_url, json={"mode": "pr"}, headers=auth(manager))
    assert pr.status_code == 201 and pr.json()["repo_full_name"] == "muradjanov-dev/qurbot"
    run_id = pr.json()["run_id"]
    started = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "running",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert started.status_code == 200 and started.json()["status"] == "running"
    events = (
        await session.scalars(
            select(AgentEvent.status)
            .join(AgentRun, AgentEvent.agent_run_id == AgentRun.id)
            .where(AgentRun.run_id == run_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events == ["pending", "dispatching", "dispatched", "running"]


async def test_public_dispatch_uses_the_private_control_repository(monkeypatch) -> None:
    from app.db.models import AgentRun, Task

    requests: list[tuple[str, dict]] = []

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def json(self):
            return {"private": False}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            requests.append((url, {}))
            return FakeResponse(200)

        async def post(self, url, **kwargs):
            requests.append((url, kwargs["json"]))
            return FakeResponse(204)

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    project = Project(
        key="qurbot",
        name="Qurbot",
        repo_full_name="muradjanov-dev/qurbot",
        default_branch="master",
    )
    task = Task(id=17, project=project, title="Update catalog", description="Clarify labels")
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000017",
        task_id=17,
        task_revision="a" * 64,
        repo_full_name="muradjanov-dev/qurbot",
        base_branch="master",
        mode="pr",
        status="pending",
    )
    assert await agent_runs._dispatch(run, task) == 204
    assert requests[0][0].endswith("/repos/muradjanov-dev/qurbot")
    assert requests[1][0].endswith("/repos/Asadtop4ik/task-manager/dispatches")
    assert requests[1][1]["event_type"] == "agent_public_task"
    assert requests[1][1]["client_payload"]["repo_full_name"] == "muradjanov-dev/qurbot"


async def test_public_pr_cancellation_uses_only_the_public_repo_token(monkeypatch) -> None:
    from app.db.models import AgentRun

    monkeypatch.setattr(settings, "github_agent_token", "task-manager-only")
    monkeypatch.setattr(settings, "github_public_agent_token", "three-public-repos-only")
    seen: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def patch(self, url, **kwargs):
            seen.append(kwargs["headers"]["Authorization"])
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: Client())
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000098",
        task_id=1,
        task_revision="a" * 64,
        repo_full_name="muradjanov-dev/qurbot",
        base_branch="master",
        mode="pr",
        status="pr_ready",
        pr_url="https://github.com/muradjanov-dev/qurbot/pull/7",
    )
    await agent_runs._close_pr(run)
    assert seen == ["Bearer three-public-repos-only"]


async def test_external_merge_notice_does_not_mark_task_done_before_deploy(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    from app.db.enums import TaskStatus
    from app.db.models import AgentRun, Task

    project.key = "qurbot"
    project.repo_full_name = "muradjanov-dev/qurbot"
    project.default_branch = "master"
    await session.commit()
    _credentials(monkeypatch)
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Public agent task"},
            headers=auth(manager),
        )
    ).json()["id"]
    task = await session.get(Task, task_id)
    assert task is not None
    task.status = TaskStatus.REVIEW
    run_id = "00000000-0000-0000-0000-000000000099"
    session.add(
        AgentRun(
            run_id=run_id,
            task_id=task_id,
            task_revision="a" * 64,
            repo_full_name="muradjanov-dev/qurbot",
            base_branch="master",
            mode="pr",
            status="pr_ready",
            pr_url="https://github.com/muradjanov-dev/qurbot/pull/7",
            head_sha="b" * 40,
            notified_at=None,
        )
    )
    await session.commit()

    async def fake_verify(run, sha: str) -> str:
        assert run.repo_full_name == "muradjanov-dev/qurbot"
        assert sha == "c" * 40
        return "b" * 40

    monkeypatch.setattr(agent_runs, "_verify_deployment", fake_verify)
    headers = {"X-Agent-Callback-Token": "test-callback-token"}
    assert (await client.get("/api/v1/agent-runs/external-pending")).status_code == 401
    pending = await client.get("/api/v1/agent-runs/external-pending", headers=headers)
    assert pending.status_code == 200 and pending.json()[0]["run_id"] == run_id
    assert pending.json()[0]["id"] > 0 and pending.json()[0]["notified"] is False
    db_run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    assert db_run is not None
    db_run.status = "pr_opened"
    await session.commit()
    premature = await client.post(
        f"/api/v1/agent-runs/{run_id}/deployed",
        json={
            "sha": "c" * 40,
            "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/123",
        },
        headers=headers,
    )
    assert premature.status_code == 409
    db_run.status = "pr_ready"
    await session.commit()
    merged = await client.post(
        f"/api/v1/agent-runs/{run_id}/merged",
        json={"sha": "c" * 40},
        headers=headers,
    )
    assert merged.status_code == 200
    assert merged.json()["status"] == "merged" and merged.json()["merged_sha"] == "c" * 40
    assert merged.json()["merged_at"] is not None
    await session.refresh(task)
    assert task.status == TaskStatus.REVIEW
    notices = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert notices.json()[0]["status"] == "merged"
    deployed = await client.post(
        f"/api/v1/agent-runs/{run_id}/deployed",
        json={
            "sha": "c" * 40,
            "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/123",
        },
        headers=headers,
    )
    assert deployed.status_code == 200 and deployed.json()["status"] == "deployed"
    await session.refresh(task)
    assert task.status == TaskStatus.DONE


def _credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_agent_token", "test-github-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")
    monkeypatch.setattr(settings, "agent_fast_enabled", True)


async def test_delegation_is_idempotent_and_visible(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    calls: list[str] = []

    async def fake_dispatch(run, task) -> int:
        calls.append(run.run_id)
        assert task.title == "Fix menu"
        assert run.repo_full_name == "Asadtop4ik/task-manager"
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    first = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    second = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert first.status_code == 201
    assert first.json()["status"] == "dispatched"
    assert second.json()["run_id"] == first.json()["run_id"]
    assert calls == [first.json()["run_id"]]
    listed = await client.get(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert [run["run_id"] for run in listed.json()] == calls


async def test_only_task_owner_can_delegate(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    executor: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    response = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(executor))
    assert response.status_code == 403


async def test_missing_repo_or_credentials_does_not_dispatch(
    client: AsyncClient, manager: User, project: Project, monkeypatch
) -> None:
    _credentials(monkeypatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    response = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert response.status_code == 409
    listed = await client.get(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert listed.json() == []


async def test_pr_callback_requires_token_and_matching_pr(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)

    async def fake_dispatch(run, task) -> int:
        return 204

    async def fake_verify(run, number: str, sha: str) -> None:
        assert number == "17"
        assert sha == "a" * 40
        assert run.repo_full_name == "Asadtop4ik/task-manager"

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify)

    async def fake_ci_verify(run, sha: str, conclusion: str, url: str) -> None:
        assert sha == "a" * 40
        assert conclusion == "success"
        assert url.endswith("/actions/runs/99")

    async def fake_current_head(run) -> tuple[str, bool]:
        return run.head_sha or "", True

    monkeypatch.setattr(agent_runs, "_verify_pr_ci", fake_ci_verify)
    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{created.json()['id']}", headers=auth(manager)
        )
    ).json()
    url = f"/api/v1/agent-runs/{run['run_id']}/callback"
    started = await client.post(
        url,
        json={"run_id": run["run_id"], "status": "running"},
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert started.status_code == 200 and started.json()["runner_started_at"] is not None
    payload = {
        "run_id": run["run_id"],
        "status": "pr_opened",
        "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/17",
        "head_sha": "a" * 40,
        "input_tokens": 6194,
        "cached_input_tokens": 4000,
        "output_tokens": 280,
    }
    assert (await client.post(url, json=payload)).status_code == 401
    bad_repo = payload | {"pr_url": "https://github.com/other/repo/pull/17"}
    assert (
        await client.post(
            url,
            json=bad_repo,
            headers={"X-Agent-Callback-Token": "test-callback-token"},
        )
    ).status_code == 400
    accepted = await client.post(
        url, json=payload, headers={"X-Agent-Callback-Token": "test-callback-token"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "pr_opened"
    assert accepted.json()["head_sha"] == "a" * 40
    assert accepted.json()["input_tokens"] == 6194
    assert accepted.json()["cached_input_tokens"] == 4000
    assert accepted.json()["output_tokens"] == 280
    assert accepted.json()["pr_opened_at"] is not None
    assert accepted.json()["pr_ready_at"] is None
    assert (
        await client.get(f"/api/v1/tasks/{created.json()['id']}", headers=auth(manager))
    ).json()["status"] == "in_progress"

    worker_headers = {"X-Agent-Worker-Token": settings.service_token}
    notices = await client.get("/api/v1/agent-runs/notifications", headers=worker_headers)
    assert notices.status_code == 200
    assert notices.json() == []
    ci = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/ci-result",
        json={
            "sha": "a" * 40,
            "conclusion": "success",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/99",
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert ci.status_code == 200 and ci.json()["status"] == "pr_opened"
    assert ci.json()["ci_verified_sha"] == "a" * 40
    assert ci.json()["pr_ready_at"] is None
    assert (
        await client.get(f"/api/v1/tasks/{created.json()['id']}", headers=auth(manager))
    ).json()["status"] == "review"
    review = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/review-result",
        json={"sha": "a" * 40, "state": "clean", "summary": "Clean", "findings": []},
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert review.status_code == 200 and review.json()["status"] == "pr_ready"
    assert review.json()["pr_ready_at"] is not None
    notices = await client.get("/api/v1/agent-runs/notifications", headers=worker_headers)
    assert len(notices.json()) == 1 and notices.json()[0]["status"] == "pr_ready"
    acknowledged = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/notified",
        json={"message_id": 42},
        headers=worker_headers,
    )
    assert acknowledged.status_code == 204

    async def fake_deployment_verify(run, sha: str) -> str:
        assert run.pr_url == payload["pr_url"]
        assert sha == "b" * 40
        return "c" * 40

    monkeypatch.setattr(agent_runs, "_verify_deployment", fake_deployment_verify)
    deployment = {
        "sha": "b" * 40,
        "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
    }
    deployed = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/deployed",
        json=deployment,
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert deployed.status_code == 200
    assert deployed.json()["status"] == "deployed"
    assert deployed.json()["deployed_sha"] == "b" * 40
    assert deployed.json()["head_sha"] == "c" * 40
    assert deployed.json()["deployed_at"] is not None
    assert (
        await client.get(f"/api/v1/tasks/{created.json()['id']}", headers=auth(manager))
    ).json()["status"] == "done"
    notices = await client.get("/api/v1/agent-runs/notifications", headers=worker_headers)
    assert notices.json()[0]["status"] == "deployed"
    assert notices.json()[0]["telegram_message_id"] == 42
    assert notices.json()[0]["deployed_sha"] == "b" * 40


async def test_failed_publisher_preflight_can_recover_same_verified_pr(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Recover validated patch"},
        headers=auth(manager),
    )
    current_task = await agent_runs._task(session, task.json()["id"])
    run_id = "00000000-0000-0000-0000-000000000029"
    session.add(
        AgentRun(
            run_id=run_id,
            task_id=task.json()["id"],
            task_revision=agent_runs._revision(current_task),
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            mode="pr",
            status="failed",
            error="Trusted PR preflight failed; inspect the publisher job.",
        )
    )
    await session.commit()

    async def fake_verify(run, number: str, sha: str) -> None:
        assert number == "29" and sha == "b" * 40

    async def fake_recovered_commit(run, sha: str) -> None:
        assert run.run_id == run_id and sha == "b" * 40

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify)
    monkeypatch.setattr(agent_runs, "_verify_recovered_commit", fake_recovered_commit)
    headers = {"X-Agent-Callback-Token": "test-callback-token"}
    recovered = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/29",
            "head_sha": "b" * 40,
        },
        headers=headers,
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "pr_opened"
    assert recovered.json()["ci_status"] == "pending"
    assert recovered.json()["error"] is None
    assert recovered.json()["pr_url"].endswith("/29")

    other_run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    assert other_run is not None
    other_run.status = "failed"
    other_run.error = "Unrelated security validation failed"
    await session.commit()
    rejected = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/29",
            "head_sha": "b" * 40,
        },
        headers=headers,
    )
    assert rejected.status_code == 409


async def test_recovered_commit_requires_the_original_run_marker(monkeypatch) -> None:
    _credentials(monkeypatch)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"commit": {"message": "feat: unrelated work"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000029",
        task_id=29,
        task_revision="a" * 64,
        repo_full_name="muradjanov-dev/qurbot",
        base_branch="master",
        mode="pr",
        status="failed",
    )
    with pytest.raises(HTTPException, match="not this agent run"):
        await agent_runs._verify_recovered_commit(run, "a" * 40)


@pytest.mark.parametrize("stale_reason", ["new_run", "edited_task"])
async def test_preflight_recovery_rejects_superseded_task(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
    stale_reason: str,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    task_response = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Original request"},
        headers=auth(manager),
    )
    task_id = task_response.json()["id"]
    task = await agent_runs._task(session, task_id)
    old_run_id = "00000000-0000-0000-0000-000000000029"
    session.add(
        AgentRun(
            run_id=old_run_id,
            task_id=task_id,
            task_revision=agent_runs._revision(task),
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            status="failed",
            error="Trusted PR preflight failed; inspect the publisher job.",
        )
    )
    await session.flush()
    if stale_reason == "new_run":
        session.add(
            AgentRun(
                run_id="00000000-0000-0000-0000-000000000030",
                task_id=task_id,
                task_revision="b" * 64,
                repo_full_name="Asadtop4ik/task-manager",
                base_branch="main",
                status="running",
            )
        )
    else:
        task.description = "Changed after the failed run"
    await session.commit()

    blocked = await client.post(
        f"/api/v1/agent-runs/{old_run_id}/callback",
        json={
            "run_id": old_run_id,
            "status": "pr_opened",
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/29",
            "head_sha": "b" * 40,
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert blocked.status_code == 409


async def test_failed_ci_stays_silent_until_new_head_passes(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)

    async def fake_dispatch(run, task) -> int:
        return 204

    async def fake_verify(run, number: str, sha: str) -> None:
        assert number == "17"
        assert sha in {"a" * 40, "b" * 40}

    async def fake_ci_verify(run, sha: str, conclusion: str, url: str) -> None:
        assert url.endswith("/actions/runs/1") or url.endswith("/actions/runs/2")

    async def fake_current_head(run) -> tuple[str, bool]:
        return run.head_sha or "", True

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify)
    monkeypatch.setattr(agent_runs, "_verify_pr_ci", fake_ci_verify)
    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task.json()['id']}", headers=auth(manager)
        )
    ).json()
    base = f"/api/v1/agent-runs/{run['run_id']}"
    token = {"X-Agent-Callback-Token": "test-callback-token"}
    worker = {"X-Agent-Worker-Token": settings.service_token}
    await client.post(
        f"{base}/callback", json={"run_id": run["run_id"], "status": "running"}, headers=token
    )
    await client.post(
        f"{base}/callback",
        json={
            "run_id": run["run_id"],
            "status": "pr_opened",
            "pr_url": "https://github.com/Asadtop4ik/task-manager/pull/17",
            "head_sha": "a" * 40,
        },
        headers=token,
    )
    failed = await client.post(
        f"{base}/ci-result",
        json={
            "sha": "a" * 40,
            "conclusion": "failure",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/1",
        },
        headers=token,
    )
    assert failed.status_code == 200 and failed.json()["ci_status"] == "failure"
    assert failed.json()["status"] == "pr_opened"
    failed_notice = (
        await client.get("/api/v1/agent-runs/notifications", headers=worker)
    ).json()
    assert len(failed_notice) == 1
    assert failed_notice[0]["ci_status"] == "failure"
    assert failed_notice[0]["owner_controls_available"] is True
    assert (
        await client.get(f"/api/v1/tasks/{task.json()['id']}", headers=auth(manager))
    ).json()["status"] == "blocked"

    # A pre-gate rollout may already have sent a PR card; only that card is edited.
    db_run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run["run_id"]))
    assert db_run is not None
    db_run.telegram_message_id = 42
    await session.commit()

    waiting = await client.post(
        f"{base}/ci-result",
        json={
            "sha": "b" * 40,
            "conclusion": "pending",
        },
        headers=token,
    )
    assert waiting.status_code == 200 and waiting.json()["ci_status"] == "pending"
    pending_notices = (
        await client.get("/api/v1/agent-runs/notifications", headers=worker)
    ).json()
    assert len(pending_notices) == 1 and pending_notices[0]["status"] == "pr_opened"
    assert pending_notices[0]["telegram_message_id"] == 42
    await client.post(f"{base}/notified", json={"message_id": 42}, headers=worker)
    passed = await client.post(
        f"{base}/ci-result",
        json={
            "sha": "b" * 40,
            "conclusion": "success",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/2",
        },
        headers=token,
    )
    assert passed.status_code == 200 and passed.json()["ci_verified_sha"] == "b" * 40
    assert passed.json()["status"] == "pr_opened"
    notices = (await client.get("/api/v1/agent-runs/notifications", headers=worker)).json()
    assert len(notices) == 1 and notices[0]["ci_url"].endswith("/actions/runs/2")
    review = await client.post(
        f"{base}/review-result",
        json={"sha": "b" * 40, "state": "clean", "summary": "Clean", "findings": []},
        headers=token,
    )
    assert review.status_code == 200 and review.json()["status"] == "pr_ready"


async def test_ci_verifier_rejects_wrong_head_even_if_workflow_is_green(monkeypatch) -> None:
    from app.db.models import AgentRun

    _credentials(monkeypatch)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "head_sha": "b" * 40,
                "head_branch": "codex/task-1-00000000-0000-0000-0000-000000000001",
                "event": "pull_request",
                "path": ".github/workflows/ci.yml",
                "status": "completed",
                "conclusion": "success",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000001",
        task_id=1,
        task_revision="a" * 64,
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        status="pr_opened",
    )
    with pytest.raises(HTTPException, match="PR CI does not match this commit"):
        await agent_runs._verify_pr_ci(
            run,
            "a" * 40,
            "success",
            "https://github.com/Asadtop4ik/task-manager/actions/runs/17",
        )


async def test_ci_verifier_requires_the_catalog_job(monkeypatch) -> None:
    _credentials(monkeypatch)
    jobs = [{"name": "unrelated", "conclusion": "success"}]
    run = AgentRun(
        run_id="00000000-0000-0000-0000-000000000001",
        task_id=1,
        task_revision="a" * 64,
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        status="pr_opened",
    )

    class FakeResponse:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def json(self):
            return self.value

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if url.endswith("/jobs?per_page=100"):
                return FakeResponse({"jobs": jobs})
            return FakeResponse(
                {
                    "head_sha": "a" * 40,
                    "head_branch": "codex/task-1-00000000-0000-0000-0000-000000000001",
                    "event": "pull_request",
                    "path": ".github/workflows/ci.yml",
                    "status": "completed",
                    "conclusion": "success",
                }
            )

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    url = "https://github.com/Asadtop4ik/task-manager/actions/runs/17"
    with pytest.raises(HTTPException, match="PR CI conclusion does not match"):
        await agent_runs._verify_pr_ci(run, "a" * 40, "success", url)
    jobs[:] = [{"name": "gate", "conclusion": "success"}]
    await agent_runs._verify_pr_ci(run, "a" * 40, "success", url)


async def test_ready_notice_rechecks_current_pr_head(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Review this PR"},
        headers=auth(manager),
    )
    session.add(
        AgentRun(
            run_id="00000000-0000-0000-0000-000000000101",
            task_id=task.json()["id"],
            task_revision="a" * 64,
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            mode="pr",
            status="pr_ready",
            ci_status="success",
            ci_verified_sha="a" * 40,
            head_sha="a" * 40,
            pr_url="https://github.com/Asadtop4ik/task-manager/pull/17",
        )
    )
    await session.commit()

    async def stale_head(run) -> tuple[str, bool]:
        raise HTTPException(status_code=409, detail="PR head changed")

    monkeypatch.setattr(agent_runs, "_current_pr_head", stale_head)
    notices = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert notices.status_code == 200 and notices.json() == []


async def test_dispatch_network_error_can_retry_once(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    calls = 0

    async def fail_dispatch(run, task) -> int:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(agent_runs, "_dispatch", fail_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    url = f"/api/v1/agent-runs/tasks/{created.json()['id']}"
    assert (await client.post(url, headers=auth(manager))).status_code == 502
    assert (await client.post(url, headers=auth(manager))).status_code == 502
    assert (await client.post(url, headers=auth(manager))).status_code == 409
    assert calls == 2


async def test_failed_job_reports_reason_then_retries_once_and_can_be_cancelled(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    dispatches: list[str] = []

    async def fake_dispatch(run, task) -> int:
        dispatches.append(run.run_id)
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    start_url = f"/api/v1/agent-runs/tasks/{task_id}"
    first = (await client.post(start_url, headers=auth(manager))).json()
    callback = await client.post(
        f"/api/v1/agent-runs/{first['run_id']}/callback",
        json={
            "run_id": first["run_id"],
            "status": "failed",
            "error": "Need an exact menu label",
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert callback.status_code == 200
    assert callback.json()["error"] == "Need an exact menu label"
    task = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))
    assert task.json()["status"] == "blocked"
    notices = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert notices.json()[0]["error"] == "Need an exact menu label"

    second = await client.post(start_url, headers=auth(manager))
    assert second.status_code == 201
    assert second.json()["attempt_index"] == 2
    assert second.json()["run_id"] != first["run_id"]
    assert dispatches == [first["run_id"], second.json()["run_id"]]
    cancelled = await client.post(
        f"/api/v1/agent-runs/{second.json()['run_id']}/cancel", headers=auth(manager)
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (await client.post(start_url, headers=auth(manager))).status_code == 409


async def test_running_job_cancel_calls_github(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)

    async def fake_dispatch(run, task) -> int:
        return 204

    cancelled_ids: list[str] = []

    async def fake_cancel(run) -> None:
        cancelled_ids.append(run.run_id)

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    monkeypatch.setattr(agent_runs, "_cancel_github", fake_cancel)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task.json()['id']}", headers=auth(manager)
        )
    ).json()
    reported = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/callback",
        json={
            "run_id": run["run_id"],
            "status": "running",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/42",
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert reported.status_code == 200
    stopped = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/cancel", headers=auth(manager)
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert cancelled_ids == [run["run_id"]]


async def test_ready_pr_is_closed_before_cancellation(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)

    async def fake_dispatch(run, task) -> int:
        return 204

    async def fake_verify(run, number: str, sha: str) -> None:
        assert number == "17"

    closed: list[str] = []

    async def fake_close(run) -> None:
        closed.append(run.pr_url)

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify)
    monkeypatch.setattr(agent_runs, "_close_pr", fake_close)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix menu"},
        headers=auth(manager),
    )
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task.json()['id']}", headers=auth(manager)
        )
    ).json()
    pr_url = "https://github.com/Asadtop4ik/task-manager/pull/17"
    ready = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/callback",
        json={
            "run_id": run["run_id"],
            "status": "pr_opened",
            "pr_url": pr_url,
            "head_sha": "a" * 40,
        },
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert ready.status_code == 200
    stopped = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/cancel", headers=auth(manager)
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert closed == [pr_url]
    task_after = await client.get(f"/api/v1/tasks/{task.json()['id']}", headers=auth(manager))
    assert task_after.json()["status"] == "todo"
    status = await client.get(
        f"/api/v1/agent-runs/{run['run_id']}/status",
        headers={"X-Agent-Callback-Token": "test-callback-token"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "cancelled"


async def test_verified_pr_rejects_wrong_branch_or_sha(monkeypatch) -> None:
    """Verification uses GitHub's PR data, not the runner's claim alone."""
    _credentials(monkeypatch)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={
                    "head": {
                        "ref": "unrelated-branch",
                        "sha": "a" * 40,
                        "repo": {"full_name": "Asadtop4ik/task-manager"},
                    }
                },
                request=httpx.Request("GET", "https://api.github.com/example"),
            )

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    run = agent_runs.AgentRun(
        task_id=1,
        run_id="00000000-0000-0000-0000-000000000001",
        repo_full_name="Asadtop4ik/task-manager",
    )
    try:
        await agent_runs._verify_pr(run, "17", "a" * 40)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("wrong branch was accepted")


async def test_deployment_accepts_review_fix_only_on_original_pr_branch(monkeypatch) -> None:
    """A fixed PR head may differ from the agent's initial commit after review."""
    _credentials(monkeypatch)
    run = agent_runs.AgentRun(
        task_id=11,
        run_id="00000000-0000-0000-0000-000000000011",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        pr_url="https://github.com/Asadtop4ik/task-manager/pull/9",
        head_sha="a" * 40,
    )
    pr = {
        "merged": True,
        "merge_commit_sha": "b" * 40,
        "base": {"ref": "main"},
        "head": {
            "ref": f"codex/task-{run.task_id}-{run.run_id}",
            "sha": "c" * 40,
            "repo": {"full_name": "Asadtop4ik/task-manager"},
        },
    }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json=pr,
                request=httpx.Request("GET", "https://api.github.com/example"),
            )

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    assert await agent_runs._verify_deployment(run, "b" * 40) == "c" * 40

    pr["head"]["ref"] = "unrelated-branch"
    try:
        await agent_runs._verify_deployment(run, "b" * 40)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("wrong PR branch was accepted")


async def test_only_owner_can_request_fast_mode(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    executor: User,
    project: Project,
    monkeypatch,
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    monkeypatch.setattr(agent_runs, "_dispatch", lambda run, task: _accepted_dispatch())
    manager.can_use_codex = False  # Owner access comes from the immutable Telegram ID.
    await session.commit()
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fast copy"},
        headers=auth(manager),
    )
    started = await client.post(
        f"/api/v1/agent-runs/tasks/{task.json()['id']}",
        json={"mode": "fast"},
        headers=auth(manager),
    )
    assert started.status_code == 201 and started.json()["mode"] == "fast"
    own = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Not owner", "assignee_id": executor.id},
        headers=auth(manager),
    )
    assert (
        await client.post(
            f"/api/v1/agent-runs/tasks/{own.json()['id']}",
            json={"mode": "fast"},
            headers=auth(executor),
        )
    ).status_code == 403
    executor.can_use_codex = True
    await session.commit()
    ordinary = await client.post(
        f"/api/v1/agent-runs/tasks/{own.json()['id']}",
        json={"mode": "pr"},
        headers=auth(executor),
    )
    assert ordinary.status_code == 201 and ordinary.json()["mode"] == "pr"


async def _accepted_dispatch() -> int:
    return 204


async def test_fast_run_completes_only_after_exact_deployed_sha(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    monkeypatch.setattr(agent_runs, "_dispatch", lambda run, task: _accepted_dispatch())

    async def verify_branch(run, sha: str) -> None:
        assert run.mode == "fast" and sha == "c" * 40

    async def verify_commit(run, sha: str) -> None:
        if sha != "c" * 40:
            raise HTTPException(status_code=409, detail="wrong SHA")
        assert run.mode == "fast" and run.head_sha == sha

    monkeypatch.setattr(agent_runs, "_verify_fast_branch", verify_branch)
    monkeypatch.setattr(agent_runs, "_verify_fast_commit", verify_commit)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fast copy"},
        headers=auth(manager),
    )
    task_id = task.json()["id"]
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task_id}",
            json={"mode": "fast"},
            headers=auth(manager),
        )
    ).json()
    callback = f"/api/v1/agent-runs/{run['run_id']}/callback"
    headers = {"X-Agent-Callback-Token": "test-callback-token"}
    for status in ("running", "validating", "publishing", "deploying"):
        body = {"run_id": run["run_id"], "status": status}
        if status != "running":
            body["head_sha"] = "c" * 40
        response = await client.post(callback, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["status"] == status
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))).json()[
        "status"
    ] == "in_progress"
    wrong = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/deployed",
        json={
            "sha": "d" * 40,
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
        },
        headers=headers,
    )
    assert wrong.status_code == 409
    deployed = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/deployed",
        json={
            "sha": "c" * 40,
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
        },
        headers=headers,
    )
    assert deployed.status_code == 200 and deployed.json()["status"] == "deployed"
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))).json()[
        "status"
    ] == "done"


async def test_verified_deploy_reconciles_a_failed_publisher_callback(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    monkeypatch.setattr(agent_runs, "_dispatch", lambda run, task: _accepted_dispatch())

    async def verify_branch(run, sha: str) -> None:
        assert sha == "c" * 40

    async def verify_commit(run, sha: str) -> None:
        assert run.head_sha == sha == "c" * 40

    monkeypatch.setattr(agent_runs, "_verify_fast_branch", verify_branch)
    monkeypatch.setattr(agent_runs, "_verify_fast_commit", verify_commit)
    task_id = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project.id, "title": "Fast callback race"},
            headers=auth(manager),
        )
    ).json()["id"]
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task_id}",
            json={"mode": "fast"},
            headers=auth(manager),
        )
    ).json()
    callback = f"/api/v1/agent-runs/{run['run_id']}/callback"
    headers = {"X-Agent-Callback-Token": "test-callback-token"}
    for state in ("running", "validating", "publishing"):
        body = {"run_id": run["run_id"], "status": state}
        if state != "running":
            body["head_sha"] = "c" * 40
        assert (await client.post(callback, json=body, headers=headers)).status_code == 200
    failed = await client.post(
        callback,
        json={
            "run_id": run["run_id"],
            "status": "failed",
            "error": "publisher callback timed out",
        },
        headers=headers,
    )
    assert failed.json()["status"] == "failed"
    deployed = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/deployed",
        json={
            "sha": "c" * 40,
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
        },
        headers=headers,
    )
    assert deployed.status_code == 200 and deployed.json()["status"] == "deployed"
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))).json()[
        "status"
    ] == "done"


async def test_fast_commit_verification_rejects_a_foreign_trailer(monkeypatch) -> None:
    _credentials(monkeypatch)
    run = agent_runs.AgentRun(
        task_id=11,
        run_id="00000000-0000-0000-0000-000000000011",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="fast",
        head_sha="c" * 40,
    )
    commit = {"commit": {"message": "feat: change\n\nAgent-Run-ID: unrelated"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                json=commit,
                request=httpx.Request("GET", "https://api.github.com/example"),
            )

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    try:
        await agent_runs._verify_fast_commit(run, "c" * 40)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("foreign fast commit was accepted")
    commit["commit"]["message"] = f"feat: change\n\nAgent-Run-ID: {run.run_id}"
    await agent_runs._verify_fast_commit(run, "c" * 40)


async def test_fast_branch_verification_requires_the_exact_remote_head(monkeypatch) -> None:
    _credentials(monkeypatch)
    run = agent_runs.AgentRun(
        task_id=11,
        run_id="00000000-0000-0000-0000-000000000011",
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="fast",
    )
    remote = {"object": {"sha": "b" * 40}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kwargs):
            assert url.endswith(f"/git/ref/heads/codex/fast/task-11-{run.run_id}")
            return httpx.Response(200, json=remote, request=httpx.Request("GET", url))

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    try:
        await agent_runs._verify_fast_branch(run, "c" * 40)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("stale remote fast branch was accepted")
    remote["object"]["sha"] = "c" * 40
    await agent_runs._verify_fast_branch(run, "c" * 40)


async def test_failed_fast_validation_blocks_task_without_a_pr(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _credentials(monkeypatch)
    monkeypatch.setattr(agent_runs, "_dispatch", lambda run, task: _accepted_dispatch())

    async def verify_branch(run, sha: str) -> None:
        assert run.mode == "fast" and sha == "c" * 40

    monkeypatch.setattr(agent_runs, "_verify_fast_branch", verify_branch)
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fast failure"},
        headers=auth(manager),
    )
    task_id = task.json()["id"]
    run = (
        await client.post(
            f"/api/v1/agent-runs/tasks/{task_id}",
            json={"mode": "fast"},
            headers=auth(manager),
        )
    ).json()
    url = f"/api/v1/agent-runs/{run['run_id']}/callback"
    headers = {"X-Agent-Callback-Token": "test-callback-token"}
    assert (
        await client.post(
            url, json={"run_id": run["run_id"], "status": "running"}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.post(
            url,
            json={"run_id": run["run_id"], "status": "validating", "head_sha": "c" * 40},
            headers=headers,
        )
    ).status_code == 200
    failed = await client.post(
        url,
        json={"run_id": run["run_id"], "status": "failed", "error": "short CI failed"},
        headers=headers,
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed" and failed.json()["pr_url"] is None
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=auth(manager))).json()[
        "status"
    ] == "blocked"
