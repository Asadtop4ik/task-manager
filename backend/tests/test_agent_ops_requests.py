"""The `/agent-ops/*` lane: storing Codex proposals inside a local-executor
callback, owner decisions, leasing, applying results, settling the run, the
72h TTL sweep and cancel cascade.

Reuses the local-executor test fixtures from `test_agent_runs_local.py`
(`_local_setup`, `_ready_project`, `_local_run`, `_run_row`) for the
callback-storage tests, and constructs `AgentRun`/`AgentOpsRequest` rows
directly for the lease/result/decision/settle/TTL tests — the same style
`test_agent_runs.py` uses for scenarios that do not need a full dispatch.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_runs
from app.core.config import settings
from app.db.enums import TaskStatus
from app.db.models import AgentOpsRequest, AgentRun, Project, Task, User
from app.schemas.agent_run import AgentRunOut
from app.services import agent_ops
from tests.conftest import auth
from tests.test_agent_runs_local import _CALLBACK, _PR_URL, _SHA, _SVC, _local_run, _run_row

# --------------------------------------------------------------------- helpers


def _proposal(
    *,
    key: str = "ADMIN_TG_IDS",
    op: str = "list_add",
    value: str = "5339875840",
    policy: str = "allowed",
    policy_reason: str = "",
    reason: str = "owner asked for a new admin",
    restart_services: list[str] | None = None,
) -> dict:
    return {
        "kind": "env_set",
        "key": key,
        "op": op,
        "value": value,
        "reason": reason,
        "policy": policy,
        "policy_reason": policy_reason,
        "restart_services": (
            restart_services if restart_services is not None else ["qurbot-web"]
        ),
    }


async def _make_task(
    session: AsyncSession, project: Project, *, status=TaskStatus.IN_PROGRESS
) -> Task:
    task = Task(project_id=project.id, title="Ops task", status=status)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _run_with_ops_row(
    session: AsyncSession,
    task: Task,
    *,
    run_status: str,
    pr_url: str | None = None,
    head_sha: str | None = None,
    deployed_sha: str | None = None,
    row_status: str = "proposed",
    key: str = "ADMIN_TG_IDS",
    op: str = "list_add",
    value: str = "5339875840",
    project_key: str = "qurbot",
    created_at: datetime | None = None,
    attempts: int = 0,
) -> tuple[AgentRun, AgentOpsRequest]:
    run = AgentRun(
        run_id=str(uuid4()),
        task_id=task.id,
        # Unique per call: several runs against the same task in one test
        # would otherwise collide on `uq_agent_run_task_attempt`.
        task_revision=uuid4().hex,
        repo_full_name="muradjanov-dev/qurbot",
        base_branch="master",
        mode="pr",
        status=run_status,
        executor="local",
        pr_url=pr_url,
        head_sha=head_sha,
        deployed_sha=deployed_sha,
    )
    session.add(run)
    await session.flush()
    row = AgentOpsRequest(
        request_uuid=str(uuid4()),
        agent_run_id=run.id,
        position=1,
        project_key=project_key,
        kind="env_set",
        key=key,
        op=op,
        value=value,
        reason="test reason",
        restart_services=["qurbot-web"],
        request_hash=agent_ops.request_hash(
            run_id=run.run_id,
            project_key=project_key,
            kind="env_set",
            key=key,
            op=op,
            value=value,
        ),
        status=row_status,
        attempts=attempts,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.commit()
    await session.refresh(run)
    await session.refresh(row)
    return run, row


def _configure_tokens(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_svc_token", "x" * 32)
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")


# ------------------------------------------------------------- request_hash


def test_request_hash_golden_vector() -> None:
    """Golden vector cross-checked against `scripts/agent_ops_policy.py`
    (WP-D0). Literal hex, recomputed independently with hashlib in a shell
    when this test was written — never derived from the function itself."""
    digest = agent_ops.request_hash(
        run_id="00000000-0000-4000-8000-000000000001",
        project_key="qurbot",
        kind="env_set",
        key="ADMIN_TG_IDS",
        op="list_add",
        value="5339875840",
    )
    assert digest == "6f374a3b95d9eb840a36960be9eb88a702042b509bacce9e08911f55ee4b74ed"


def test_agent_run_out_has_no_ops_fields() -> None:
    assert "ops_requests" not in AgentRunOut.model_fields
    assert "value" not in AgentRunOut.model_fields


# --------------------------------------------------------- callback storage


async def test_proposals_stored_and_denylist_invalid(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    lease_id = leased.json()["lease_id"]

    async def fake_verify_pr(run, number, sha) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    proposals = [
        _proposal(key="ADMIN_TG_IDS", value="5339875840"),
        _proposal(key="API_TOKEN", value="abcXYZ123", reason="rotate the token"),
    ]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": _PR_URL,
            "head_sha": _SHA,
            "ops_requests": proposals,
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    run = await _run_row(session, run_id)
    rows = await agent_ops.list_for_run(session, run.id)
    assert [row.key for row in rows] == ["ADMIN_TG_IDS", "API_TOKEN"]
    assert rows[0].status == "proposed"
    assert rows[1].status == "invalid" and rows[1].policy_reason == "denylisted key"
    assert rows[0].request_hash == agent_ops.request_hash(
        run_id=run_id,
        project_key="task-manager",
        kind="env_set",
        key="ADMIN_TG_IDS",
        op="list_add",
        value="5339875840",
    )


async def test_more_than_three_proposals_rejected(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    proposals = [_proposal(key=f"KEY{i}A") for i in range(4)]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": proposals},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 422
    run = await _run_row(session, run_id)
    assert await agent_ops.list_for_run(session, run.id) == []


async def test_bad_value_characters_rejected(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    for bad_value in ("5339875840\n", "abc$def", "http://evil.example"):
        resp = await client.post(
            f"/api/v1/agent-runs/{run_id}/callback",
            json={
                "run_id": run_id,
                "status": "ops_pending",
                "ops_requests": [_proposal(value=bad_value)],
            },
            headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
        )
        assert resp.status_code == 422, bad_value


async def test_github_executor_ops_rejected(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run_id = str(uuid4())
    session.add(
        AgentRun(
            run_id=run_id,
            task_id=task.id,
            task_revision="a" * 64,
            repo_full_name="Asadtop4ik/task-manager",
            base_branch="main",
            mode="pr",
            status="running",
            executor="github",
        )
    )
    await session.commit()
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "pr_opened", "ops_requests": [_proposal()]},
        headers=_CALLBACK,
    )
    assert resp.status_code == 409


async def test_local_run_without_lease_rejects_ops(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": _PR_URL,
            "head_sha": _SHA,
            "ops_requests": [_proposal()],
        },
        headers=_CALLBACK,  # no X-Agent-Lease-ID
    )
    assert resp.status_code == 409


async def test_ops_pending_transition(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": [_proposal()]},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ops_pending"
    run = await _run_row(session, run_id)
    assert run.lease_id is None
    assert run.finished_at is not None
    task = await session.get(Task, run.task_id)
    assert task is not None and task.status == TaskStatus.IN_PROGRESS


async def test_ops_pending_without_eligible_proposal_rejected(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "ops_pending",
            "ops_requests": [_proposal(policy="denied", policy_reason="not allowlisted")],
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 400
    run = await _run_row(session, run_id)
    assert run.status != "ops_pending"


async def test_list_task_runs_never_carries_ops_values(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "ops_pending",
            "ops_requests": [_proposal(value="5339875840")],
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    run = await _run_row(session, run_id)
    listed = await client.get(f"/api/v1/agent-runs/tasks/{run.task_id}", headers=auth(manager))
    assert listed.status_code == 200
    body = listed.json()
    assert "5339875840" not in json.dumps(body)
    assert all("ops_requests" not in item for item in body)


# ------------------------------------------------------------- cancel cascade


async def test_cancel_cascades_proposed_rows_to_cancelled(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": [_proposal()]},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    run = await _run_row(session, run_id)
    rows = await agent_ops.list_for_run(session, run.id)
    assert rows[0].status == "proposed"
    cancel = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=auth(manager))
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    await session.refresh(rows[0])
    assert rows[0].status == "cancelled"


# ------------------------------------------------------------------ decision


async def test_decision_owner_only(
    client: AsyncClient, session: AsyncSession, project: Project, executor: User
) -> None:
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    resp = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision",
        json={
            "decision": "approve",
            "request_hash": row.request_hash,
            "action_id": str(uuid4()),
        },
        headers=auth(executor),
    )
    assert resp.status_code == 403


async def test_decision_hash_mismatch(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User
) -> None:
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    resp = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision",
        json={"decision": "approve", "request_hash": "0" * 64, "action_id": str(uuid4())},
        headers=auth(manager),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stale_request"


async def test_decision_idempotent_replay_then_conflicting_decision(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User
) -> None:
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    action_id = str(uuid4())
    body = {"decision": "approve", "request_hash": row.request_hash, "action_id": action_id}
    first = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision", json=body, headers=auth(manager)
    )
    assert first.status_code == 200
    assert first.json()["ops_request"]["status"] == "approved"
    assert first.json()["run"]["ops_requests"][0]["value"] == row.value
    replay = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision", json=body, headers=auth(manager)
    )
    assert replay.status_code == 200
    assert replay.json()["ops_request"]["status"] == "approved"
    conflicting = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision",
        json={**body, "decision": "reject"},
        headers=auth(manager),
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"] == "conflict"


async def test_decision_not_pending_once_already_decided(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User
) -> None:
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    body = {"decision": "approve", "request_hash": row.request_hash, "action_id": str(uuid4())}
    first = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision", json=body, headers=auth(manager)
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision",
        json={
            "decision": "approve",
            "request_hash": row.request_hash,
            "action_id": str(uuid4()),
        },
        headers=auth(manager),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "not_pending"


async def test_decision_refused_once_run_is_finished(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User
) -> None:
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    run.status = "cancelled"
    await session.commit()
    resp = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision",
        json={
            "decision": "approve",
            "request_hash": row.request_hash,
            "action_id": str(uuid4()),
        },
        headers=auth(manager),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "run_finished"


async def test_get_ops_request_returns_run_detail_with_values(
    client: AsyncClient, session: AsyncSession, project: Project, manager: User
) -> None:
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    resp = await client.get(f"/api/v1/agent-ops/{row.id}", headers=auth(manager))
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run.run_id
    assert body["ops_requests"][0]["value"] == row.value
    assert (
        await client.get("/api/v1/agent-ops/999999", headers=auth(manager))
    ).status_code == 404


# --------------------------------------------------------------- lease gating


async def test_lease_returns_204_when_pr_run_not_yet_deployed(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    await _run_with_ops_row(
        session,
        task,
        run_status="pr_ready",
        pr_url=_PR_URL,
        head_sha=_SHA,
        row_status="approved",
    )
    resp = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    assert resp.status_code == 204


async def test_lease_gates_on_deployed_and_enforces_single_applying(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    deployed_run, deployed_row = await _run_with_ops_row(
        session,
        task,
        run_status="deployed",
        pr_url=_PR_URL,
        head_sha=_SHA,
        deployed_sha=_SHA,
        row_status="approved",
    )
    await _run_with_ops_row(session, task, run_status="ops_pending", row_status="approved")

    first = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    assert first.status_code == 200
    data = first.json()
    assert data["ops_id"] == deployed_row.id
    assert data["run_id"] == deployed_run.run_id
    assert data["deployed_sha"] == _SHA
    assert data["attempts"] == 1

    # A second approved+eligible row exists (the ops_pending run's), but only
    # one request may ever be `applying` globally.
    second = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    assert second.status_code == 204


# --------------------------------------------------------------------- result


async def test_result_requires_svc_token_and_live_lease(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    await _run_with_ops_row(session, task, run_status="ops_pending", row_status="approved")
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    assert leased.status_code == 200
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]

    refused = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "applied"},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert refused.status_code == 401

    mismatched = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "applied"},
        headers={**_SVC, "X-Agent-Lease-ID": "not-the-lease"},
    )
    assert mismatched.status_code == 409

    ok = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "applied", "image_tag": "sha-abc"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "applied"


async def test_retry_cap_fails_the_request(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    await _run_with_ops_row(session, task, run_status="ops_pending", row_status="approved")

    for attempt in (1, 2, 3):
        leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
        assert leased.status_code == 200
        assert leased.json()["attempts"] == attempt
        ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
        result = await client.post(
            f"/api/v1/agent-ops/{ops_id}/result",
            json={"status": "retry", "code": "busy", "message": "compose busy"},
            headers={**_SVC, "X-Agent-Lease-ID": lease_id},
        )
        assert result.status_code == 200
        expected = "approved" if attempt < 3 else "failed"
        assert result.json()["status"] == expected


# ---------------------------------------------------------------------- settle


async def test_settle_applied_marks_task_done(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
    result = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "applied", "image_tag": "sha-abc"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    assert result.status_code == 200
    await session.refresh(task)
    assert task.status == TaskStatus.DONE
    run_row = await session.get(AgentRun, run.id)
    assert run_row is not None and run_row.status == "ops_applied"


async def test_settle_failed_marks_task_blocked(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
    result = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "failed", "code": "precondition", "message": "key missing"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    await session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    run_row = await session.get(AgentRun, run.id)
    assert run_row is not None and run_row.status == "failed"
    assert run_row.error == "Ops so‘rovlari qo‘llanmadi"


# ------------------------------------------------------------------------- TTL


async def test_ttl_cancels_a_stale_proposed_row(
    client: AsyncClient, session: AsyncSession, project: Project
) -> None:
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(
        session,
        task,
        run_status="ops_pending",
        row_status="proposed",
        created_at=datetime.now(UTC) - timedelta(hours=73),
    )
    resp = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert resp.status_code == 200
    await session.refresh(row)
    assert row.status == "cancelled"


# --------------------------------------------------------------- notifications


async def test_notification_reports_ops_pending_count_and_values(
    client: AsyncClient, session: AsyncSession, project: Project
) -> None:
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="proposed"
    )
    resp = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    assert resp.status_code == 200
    [notice] = [item for item in resp.json() if item["run_id"] == run.run_id]
    assert notice["ops_pending_count"] == 1
    assert notice["ops_controls_available"] is True
    assert notice["ops_requests"][0]["value"] == "5339875840"
