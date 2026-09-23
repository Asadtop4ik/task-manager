import httpx
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_runs
from app.core.config import settings
from app.db.models import Project, User
from tests.conftest import auth


async def _ready_project(session: AsyncSession, project: Project) -> None:
    project.repo_full_name = "Asadtop4ik/task-manager"
    project.default_branch = "main"
    await session.commit()


def _credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_agent_token", "test-github-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")


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
    payload = {
        "run_id": run["run_id"],
        "status": "pr_ready",
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
    assert accepted.json()["status"] == "pr_ready"
    assert accepted.json()["head_sha"] == "a" * 40
    assert accepted.json()["input_tokens"] == 6194
    assert accepted.json()["cached_input_tokens"] == 4000
    assert accepted.json()["output_tokens"] == 280
    assert (
        await client.get(f"/api/v1/tasks/{created.json()['id']}", headers=auth(manager))
    ).json()["status"] == "review"

    worker_headers = {"X-Agent-Worker-Token": settings.service_token}
    notices = await client.get("/api/v1/agent-runs/notifications", headers=worker_headers)
    assert notices.status_code == 200
    assert notices.json()[0]["status"] == "pr_ready"
    acknowledged = await client.post(
        f"/api/v1/agent-runs/{run['run_id']}/notified", headers=worker_headers
    )
    assert acknowledged.status_code == 204

    async def fake_deployment_verify(run, sha: str) -> None:
        assert run.pr_url == payload["pr_url"]
        assert sha == "b" * 40

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
    assert (
        await client.get(f"/api/v1/tasks/{created.json()['id']}", headers=auth(manager))
    ).json()["status"] == "done"
    notices = await client.get("/api/v1/agent-runs/notifications", headers=worker_headers)
    assert notices.json()[0]["status"] == "deployed"


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
