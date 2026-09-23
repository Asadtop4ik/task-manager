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
            "status": "pr_ready",
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
