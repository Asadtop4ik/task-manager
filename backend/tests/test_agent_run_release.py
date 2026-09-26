import hashlib
import json
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_runs
from app.core.config import settings
from app.db.models import AgentRun, AgentRunAction, Project, User
from app.services.agent_repos import repository_for
from tests.conftest import auth

_SHA = "a" * 40
_CALLBACK = {"X-Agent-Callback-Token": "test-callback-token"}


async def _open_run(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    *,
    status: str = "pr_opened",
    ci_status: str | None = "pending",
    review_status: str | None = "pending",
) -> AgentRun:
    project.key = "task-manager"
    project.repo_full_name = "Asadtop4ik/task-manager"
    project.default_branch = "main"
    await session.commit()
    task = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Review PR",
            "description": "Keep the release safe.",
        },
        headers=auth(manager),
    )
    assert task.status_code == 201
    run = AgentRun(
        run_id=str(uuid4()),
        task_id=task.json()["id"],
        task_revision="b" * 64,
        repo_full_name="Asadtop4ik/task-manager",
        base_branch="main",
        mode="pr",
        status=status,
        ci_status=ci_status,
        ci_verified_sha=_SHA if ci_status == "success" else None,
        ci_url=(
            "https://github.com/Asadtop4ik/task-manager/actions/runs/4"
            if ci_status == "success"
            else None
        ),
        head_sha=_SHA,
        pr_url="https://github.com/Asadtop4ik/task-manager/pull/4",
        review_status=review_status,
        review_sha=_SHA if review_status == "clean" else None,
        review_summary="No actionable findings." if review_status == "clean" else None,
        review_findings=[] if review_status == "clean" else None,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def _worker() -> dict[str, str]:
    return {"X-Agent-Worker-Token": settings.service_token}


def test_qa_repository_catalog_entry_requires_explicit_include_flag() -> None:
    assert repository_for("agent-qa", "Asadtop4ik/agent-qa", "main") is None
    qa = repository_for("agent-qa", "Asadtop4ik/agent-qa", "main", include_qa=True)
    assert qa is not None and qa.qa_only and qa.private
    assert qa.pr_ci_jobs == ("PR CI",)


@pytest.mark.asyncio
async def test_ci_success_waits_for_clean_review_on_same_head(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(client, session, manager, project)
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def verify_pr(current, number: str, sha: str) -> None:
        assert number == "4" and sha == _SHA

    async def verify_ci(current, sha: str, conclusion: str, url: str) -> None:
        assert sha == _SHA and conclusion == "success" and url.endswith("/4")

    async def current_head(current) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_verify_pr", verify_pr)
    monkeypatch.setattr(agent_runs, "_verify_pr_ci", verify_ci)
    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    ci = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/ci-result",
        json={
            "sha": _SHA,
            "conclusion": "success",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/4",
        },
        headers=_CALLBACK,
    )
    assert ci.status_code == 200
    assert ci.json()["status"] == "pr_opened"
    assert ci.json()["pr_ready_at"] is None

    blocked = await client.get(f"/api/v1/agent-runs/{run.run_id}", headers=auth(manager))
    assert blocked.status_code == 200
    assert blocked.json()["actions"]["merge"]["available"] is False

    stale = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/review-result",
        json={"sha": "c" * 40, "state": "clean", "summary": "Clean", "findings": []},
        headers=_CALLBACK,
    )
    assert stale.status_code == 409
    review = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/review-result",
        json={"sha": _SHA, "state": "clean", "summary": "Clean", "findings": []},
        headers=_CALLBACK,
    )
    assert review.status_code == 200
    assert review.json()["status"] == "pr_ready"
    assert review.json()["review_status"] == "clean"
    assert review.json()["review_sha"] == _SHA


@pytest.mark.asyncio
async def test_p3_review_is_advisory_but_p2_still_blocks_merge(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_opened",
        ci_status="success",
        review_status="pending",
    )
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def verify_pr(current, number: str, sha: str) -> None:
        assert number == "4" and sha == _SHA

    async def current_head(current) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_verify_pr", verify_pr)
    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    advisory = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/review-result",
        json={
            "sha": _SHA,
            "state": "advisory",
            "summary": "One low priority suggestion.",
            "findings": [
                {
                    "severity": "P3",
                    "title": "Prefer a clearer name",
                    "evidence": "The current name is correct but vague.",
                }
            ],
        },
        headers=_CALLBACK,
    )
    assert advisory.status_code == 200
    assert advisory.json()["status"] == "pr_ready"
    assert advisory.json()["review_status"] == "advisory"
    detail = await client.get(f"/api/v1/agent-runs/{run.run_id}", headers=auth(manager))
    assert detail.json()["actions"]["merge"]["available"] is True
    assert detail.json()["review"]["findings"][0]["severity"] == "P3"

    blocking = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/review-result",
        json={
            "sha": _SHA,
            "state": "findings",
            "summary": "A correctness issue remains.",
            "findings": [
                {
                    "severity": "P2",
                    "title": "Missing input guard",
                    "evidence": "An empty value reaches the unsafe branch.",
                }
            ],
        },
        headers=_CALLBACK,
    )
    assert blocking.status_code == 200
    assert blocking.json()["status"] == "pr_opened"
    detail = await client.get(f"/api/v1/agent-runs/{run.run_id}", headers=auth(manager))
    assert detail.json()["actions"]["merge"]["available"] is False


@pytest.mark.asyncio
async def test_owner_merge_is_idempotent_and_rejects_stale_head(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    executor: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_ready",
        ci_status="success",
        review_status="clean",
    )
    monkeypatch.setattr(settings, "github_agent_token", "task-manager-write-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def current_head(current) -> tuple[str, bool]:
        return _SHA, True

    dispatched: list[str] = []

    async def dispatch(current, action, payload) -> None:
        dispatched.append(action.action_id)

    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    monkeypatch.setattr(agent_runs, "_dispatch_release_action", dispatch)
    base = f"/api/v1/agent-runs/{run.run_id}/merge"
    forbidden = await client.post(
        base,
        json={"expected_head_sha": _SHA, "action_id": str(uuid4())},
        headers=auth(executor),
    )
    assert forbidden.status_code == 403

    stale = await client.post(
        base,
        json={"expected_head_sha": "b" * 40, "action_id": str(uuid4())},
        headers=auth(manager),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_head"
    assert stale.json()["error"]["current_head_sha"] == _SHA

    action_id = str(uuid4())
    body = {"expected_head_sha": _SHA, "action_id": action_id}
    first = await client.post(base, json=body, headers=auth(manager))
    second = await client.post(base, json=body, headers=auth(manager))
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "in_progress"
    assert first.json()["action_id"] == action_id
    assert dispatched == [action_id]


@pytest.mark.asyncio
async def test_transient_dispatch_failure_can_retry_with_same_action_id(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_ready",
        ci_status="success",
        review_status="clean",
    )
    monkeypatch.setattr(settings, "github_agent_token", "task-manager-write-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def current_head(current) -> tuple[str, bool]:
        return _SHA, True

    dispatched: list[str] = []

    async def dispatch(current, action, payload) -> None:
        dispatched.append(action.action_id)
        if len(dispatched) == 1:
            raise agent_runs.httpx.ConnectError("connection reset")

    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    monkeypatch.setattr(agent_runs, "_dispatch_release_action", dispatch)
    action_id = str(uuid4())
    body = {"expected_head_sha": _SHA, "action_id": action_id}
    first = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/merge", json=body, headers=auth(manager)
    )
    assert first.status_code == 200
    assert first.json()["status"] == "retryable"
    action_status = await client.get(
        f"/api/v1/agent-runs/{run.run_id}/actions/{action_id}", headers=_CALLBACK
    )
    assert action_status.status_code == 200
    assert action_status.json()["status"] == "retryable"
    second = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/merge", json=body, headers=auth(manager)
    )
    third = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/merge", json=body, headers=auth(manager)
    )
    assert second.status_code == third.status_code == 200
    assert second.json()["status"] == third.json()["status"] == "in_progress"
    assert dispatched == [action_id, action_id]
    actions = (
        await session.scalars(
            select(AgentRunAction).where(AgentRunAction.agent_run_id == run.id)
        )
    ).all()
    assert len(actions) == 1 and actions[0].status == "in_progress"


@pytest.mark.asyncio
async def test_qa_merge_workflow_rejection_retries_with_same_action_id_when_still_ready(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_ready",
        ci_status="success",
        review_status="clean",
    )
    run.repo_full_name = "Asadtop4ik/agent-qa"
    run.base_branch = "main"
    run.pr_url = "https://github.com/Asadtop4ik/agent-qa/pull/3"
    await session.commit()
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_repository", "Asadtop4ik/agent-qa")
    monkeypatch.setattr(settings, "github_agent_token", "task-manager-token")
    monkeypatch.setattr(settings, "github_agent_qa_token", "qa-read-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    head_lookups = 0

    async def current_head(current) -> tuple[str, bool]:
        nonlocal head_lookups
        head_lookups += 1
        assert current.repo_full_name == "Asadtop4ik/agent-qa"
        if head_lookups == 1:
            raise agent_runs.httpx.ConnectError("temporary GitHub lookup failure")
        return _SHA, True

    dispatched: list[str] = []

    async def dispatch(current, action, payload) -> None:
        dispatched.append(action.action_id)

    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    monkeypatch.setattr(agent_runs, "_dispatch_release_action", dispatch)
    action_id = str(uuid4())
    request_data = {"expected_head_sha": _SHA}
    session.add(
        AgentRunAction(
            action_id=action_id,
            agent_run_id=run.id,
            kind="merge",
            request_hash=hashlib.sha256(
                json.dumps(request_data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            request_data=request_data,
            status="in_progress",
        )
    )
    await session.commit()

    rejected = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/action-result",
        json={
            "action_id": action_id,
            "status": "rejected",
            "head_sha": _SHA,
            "message": "Checks permission unavailable",
        },
        headers=_CALLBACK,
    )
    assert rejected.status_code == 200
    action_status = await client.get(
        f"/api/v1/agent-runs/{run.run_id}/actions/{action_id}", headers=_CALLBACK
    )
    assert action_status.json()["status"] == "retryable"

    retried = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/merge",
        json={"expected_head_sha": _SHA, "action_id": action_id},
        headers=auth(manager),
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "in_progress"
    assert head_lookups == 2
    assert dispatched == [action_id]


@pytest.mark.asyncio
async def test_stale_qa_merge_rejection_stays_rejected_and_invalidates_evidence(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_ready",
        ci_status="success",
        review_status="clean",
    )
    run.repo_full_name = "Asadtop4ik/agent-qa"
    run.base_branch = "main"
    run.pr_url = "https://github.com/Asadtop4ik/agent-qa/pull/3"
    await session.commit()
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_repository", "Asadtop4ik/agent-qa")
    monkeypatch.setattr(settings, "github_agent_token", "task-manager-token")
    monkeypatch.setattr(settings, "github_agent_qa_token", "qa-read-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def stale_head(current) -> tuple[str, bool]:
        return "c" * 40, True

    dispatched: list[str] = []

    async def dispatch(current, action, payload) -> None:
        dispatched.append(action.action_id)

    monkeypatch.setattr(agent_runs, "_current_pr_head", stale_head)
    monkeypatch.setattr(agent_runs, "_dispatch_release_action", dispatch)
    action_id = str(uuid4())
    request_data = {"expected_head_sha": _SHA}
    session.add(
        AgentRunAction(
            action_id=action_id,
            agent_run_id=run.id,
            kind="merge",
            request_hash=hashlib.sha256(
                json.dumps(request_data, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            request_data=request_data,
            status="in_progress",
        )
    )
    await session.commit()

    rejected = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/action-result",
        json={
            "action_id": action_id,
            "status": "rejected",
            "head_sha": _SHA,
            "message": "PR head changed",
        },
        headers=_CALLBACK,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "pr_opened"
    assert rejected.json()["head_sha"] == "c" * 40
    assert rejected.json()["ci_verified_sha"] is None
    assert rejected.json()["review_status"] == "pending"
    action_status = await client.get(
        f"/api/v1/agent-runs/{run.run_id}/actions/{action_id}", headers=_CALLBACK
    )
    assert action_status.json()["status"] == "rejected"

    repeated = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/merge",
        json={"expected_head_sha": _SHA, "action_id": action_id},
        headers=auth(manager),
    )
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "rejected"
    assert dispatched == []


@pytest.mark.asyncio
async def test_same_sha_correction_requires_new_review_but_keeps_ci(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(
        client,
        session,
        manager,
        project,
        status="pr_opened",
        ci_status="success",
        review_status="findings",
    )
    monkeypatch.setattr(settings, "github_agent_token", "task-manager-write-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def current_head(current) -> tuple[str, bool]:
        return _SHA, True

    async def dispatch(current, action, payload) -> None:
        assert action.kind == "correction"
        assert payload["instruction"] == "Please reconsider P2 with this evidence."

    async def verify_pr(current, number: str, sha: str) -> None:
        assert number == "4" and sha == _SHA

    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    monkeypatch.setattr(agent_runs, "_dispatch_release_action", dispatch)
    monkeypatch.setattr(agent_runs, "_verify_pr", verify_pr)
    body = {
        "expected_head_sha": _SHA,
        "action_id": str(uuid4()),
        "instruction": "Please reconsider P2 with this evidence.",
    }
    requested = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/corrections", json=body, headers=auth(manager)
    )
    assert requested.status_code == 200
    assert requested.json()["status"] == "in_progress"
    action = requested.json()["action_id"]
    completed = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/action-result",
        json={"action_id": action, "status": "completed", "head_sha": _SHA},
        headers=_CALLBACK,
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "pr_opened"
    assert completed.json()["ci_verified_sha"] == _SHA
    assert completed.json()["review_status"] == "pending"
    assert completed.json()["review_sha"] is None


@pytest.mark.asyncio
async def test_owner_notice_message_id_is_scoped_to_owner_private_chat(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
) -> None:
    run = await _open_run(client, session, manager, project)
    run.telegram_message_id = 77
    await session.commit()
    response = await client.post(
        f"/api/v1/agent-runs/{run.run_id}/owner-notified",
        json={"message_id": 99},
        headers=_worker(),
    )
    assert response.status_code == 204
    await session.refresh(run)
    assert run.telegram_message_id == 77
    assert run.owner_notice_chat_id == manager.telegram_id
    assert run.owner_notice_message_id == 99


@pytest.mark.asyncio
async def test_qa_deploy_callback_is_scoped_and_requires_matching_readiness_evidence(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    run = await _open_run(client, session, manager, project, status="merged")
    run.repo_full_name = "Asadtop4ik/agent-qa"
    run.base_branch = "main"
    run.pr_url = "https://github.com/Asadtop4ik/agent-qa/pull/4"
    run.merged_sha = "b" * 40
    action_id = str(uuid4())
    session.add(
        AgentRunAction(
            action_id=action_id,
            agent_run_id=run.id,
            kind="merge",
            request_hash="c" * 64,
            request_data={"expected_head_sha": _SHA},
            status="completed",
            result={"head_sha": _SHA, "merge_sha": "b" * 40},
        )
    )
    await session.commit()
    monkeypatch.setattr(settings, "agent_qa_enabled", True)
    monkeypatch.setattr(settings, "agent_qa_callback_token", "qa-only-token-0123456789abcdef")
    monkeypatch.setattr(settings, "agent_qa_ready_url", "http://127.0.0.1:18082/ready")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def verify_deployment(current, sha: str) -> str:
        assert current.repo_full_name == "Asadtop4ik/agent-qa"
        assert sha == "b" * 40
        return _SHA

    monkeypatch.setattr(agent_runs, "_verify_deployment", verify_deployment)

    dispatch_url = f"/api/v1/agent-runs/{run.run_id}/qa-deploy-dispatch-result"
    failed_dispatch = await client.post(
        dispatch_url,
        json={
            "action_id": action_id,
            "sha": "b" * 40,
            "status": "failed",
            "message": "Actions:write is missing",
        },
        headers=_CALLBACK,
    )
    assert failed_dispatch.status_code == 200
    assert failed_dispatch.json()["qa_deploy_dispatch_status"] == "failed"
    assert failed_dispatch.json()["qa_deploy_dispatch_error"] == "Actions:write is missing"
    retried_dispatch = await client.post(
        dispatch_url,
        json={"action_id": action_id, "sha": "b" * 40, "status": "dispatched"},
        headers=_CALLBACK,
    )
    assert retried_dispatch.status_code == 200
    assert retried_dispatch.json()["qa_deploy_dispatch_status"] == "dispatched"
    assert retried_dispatch.json()["qa_deploy_dispatch_error"] is None

    authorization_url = f"/api/v1/agent-runs/{run.run_id}/qa-deployment-authorization"
    authorization_body = {
        "action_id": action_id,
        "expected_head_sha": _SHA,
        "merge_sha": "b" * 40,
    }
    unauthorized_sha = await client.post(
        authorization_url,
        json=authorization_body | {"merge_sha": "d" * 40},
        headers={"X-Agent-QA-Callback-Token": "qa-only-token-0123456789abcdef"},
    )
    assert unauthorized_sha.status_code == 409
    authorization = await client.post(
        authorization_url,
        json=authorization_body,
        headers={"X-Agent-QA-Callback-Token": "qa-only-token-0123456789abcdef"},
    )
    assert authorization.status_code == 200
    assert authorization.json()["authorized"] is True
    assert authorization.json()["merge_sha"] == "b" * 40

    url = f"/api/v1/agent-runs/{run.run_id}/qa-deployed"
    body = {
        "sha": "b" * 40,
        "github_run_url": "https://github.com/Asadtop4ik/agent-qa/actions/runs/17",
        "ready_url": "http://127.0.0.1:18082/ready",
        "ready_status": "ready",
        "ready_sha": "b" * 40,
    }
    denied = await client.post(url, json=body, headers={"X-Agent-QA-Callback-Token": "wrong"})
    assert denied.status_code == 401
    stale = await client.post(
        url,
        json=body | {"ready_sha": "a" * 40},
        headers={"X-Agent-QA-Callback-Token": "qa-only-token-0123456789abcdef"},
    )
    assert stale.status_code == 409
    deployed = await client.post(
        url,
        json=body,
        headers={"X-Agent-QA-Callback-Token": "qa-only-token-0123456789abcdef"},
    )
    assert deployed.status_code == 200
    assert deployed.json()["status"] == "deployed"
    assert deployed.json()["qa_ready_sha"] == "b" * 40
    assert deployed.json()["qa_ready_url"] == "http://127.0.0.1:18082/ready"
    replay = await client.post(
        url,
        json=body,
        headers={"X-Agent-QA-Callback-Token": "qa-only-token-0123456789abcdef"},
    )
    assert replay.status_code == 200 and replay.json()["status"] == "deployed"
