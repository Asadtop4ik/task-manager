"""The `/agent-ops/*` lane: storing Codex proposals inside a local-executor
callback, owner decisions, leasing, applying results, settling the run, the
72h TTL sweep and cancel cascade.

Reuses the local-executor test fixtures from `test_agent_runs_local.py`
(`_local_setup`, `_ready_project`, `_local_run`, `_run_row`) for the
callback-storage tests, and constructs `AgentRun`/`AgentOpsRequest` rows
directly for the lease/result/decision/settle/TTL tests — the same style
`test_agent_runs.py` uses for scenarios that do not need a full dispatch.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
    decided_at: datetime | None = None,
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
        decided_at=decided_at,
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


# ------------------------------------------------------------- concurrency
#
# These reproduce the two races an adversarial review found in the first
# version of this feature (P2-1 write skew, P2-2 cancel/lease clobber), using
# genuinely interleaved async tasks against separate raw sessions/connections
# rather than the single shared `session` fixture (`AsyncSession` does not
# support concurrent use from two coroutines at once, and sequential calls on
# one session/transaction cannot reproduce a cross-transaction race at all).


async def test_concurrent_reject_and_apply_still_settle_the_run(
    engine: AsyncEngine, session: AsyncSession, project: Project
) -> None:
    """P2-1: decide (reject B) and report-result (apply A) run concurrently
    for two ops rows on the same `ops_pending` run. Before the run-locking
    fix, each transaction's `settle_run` call could compute "still has an
    open row" from its own pre-commit snapshot of the *other* row, and the
    run would never settle at all. Locking the run first (see
    `agent_ops.lock_run_for_ops_id`) serializes the two, so whichever commits
    second always sees the first's already-committed result."""
    task = await _make_task(session, project)
    run, row_a = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="applying", key="ADMIN_TG_IDS"
    )
    row_b = AgentOpsRequest(
        request_uuid=str(uuid4()),
        agent_run_id=run.id,
        position=2,
        project_key="qurbot",
        kind="env_set",
        key="SUPER_ADMIN_TG_IDS",
        op="list_add",
        value="1111111111",
        reason="r",
        request_hash="0" * 64,
        status="proposed",
    )
    session.add(row_b)
    await session.commit()
    await session.refresh(row_b)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def reject_b() -> None:
        async with maker() as s:
            locked_run = await agent_ops.lock_run_for_ops_id(s, row_b.id)
            assert locked_run is not None
            b = await s.scalar(
                select(AgentOpsRequest).where(AgentOpsRequest.id == row_b.id).with_for_update()
            )
            assert b is not None
            b.status = "rejected"
            await agent_ops.settle_run(s, locked_run)
            await s.commit()

    async def apply_a() -> None:
        async with maker() as s:
            locked_run = await agent_ops.lock_run_for_ops_id(s, row_a.id)
            assert locked_run is not None
            a = await s.scalar(
                select(AgentOpsRequest).where(AgentOpsRequest.id == row_a.id).with_for_update()
            )
            assert a is not None
            a.status = "applied"
            await agent_ops.settle_run(s, locked_run)
            await s.commit()

    await asyncio.gather(reject_b(), apply_a())

    async with maker() as verify:
        final_run = await verify.get(AgentRun, run.id)
        assert final_run is not None and final_run.status == "ops_applied"
        final_task = await verify.get(Task, task.id)
        assert final_task is not None and final_task.status == TaskStatus.DONE


async def test_reclaim_stale_self_heals_a_stuck_ops_pending_run(
    session: AsyncSession, project: Project
) -> None:
    """Belt-and-suspenders sibling of the test above: whatever state an
    `ops_pending` run is found in with no `proposed`/`approved`/`applying`
    row left, the watchdog sweep settles it — even if nothing else ever
    called `settle_run` for it."""
    task = await _make_task(session, project)
    run, _row_a = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="applied", key="ADMIN_TG_IDS"
    )
    session.add(
        AgentOpsRequest(
            request_uuid=str(uuid4()),
            agent_run_id=run.id,
            position=2,
            project_key="qurbot",
            kind="env_set",
            key="SUPER_ADMIN_TG_IDS",
            op="list_add",
            value="1111111111",
            reason="r",
            request_hash="0" * 64,
            status="rejected",
        )
    )
    await session.commit()
    await agent_ops.reclaim_stale(session, datetime.now(UTC))
    await session.commit()
    await session.refresh(run)
    assert run.status == "ops_applied"


async def test_cancel_bulk_update_never_clobbers_a_concurrently_leased_row(
    engine: AsyncEngine, session: AsyncSession, project: Project
) -> None:
    """P2-2: stage the exact interleaving the review used to reproduce the
    original bug — `cancel_agent_run` locks the run and reads the ops rows
    it *thinks* are still open, then (concurrently, in a fully separate and
    already-committed transaction) `POST /agent-ops/lease` picks the same
    row up and marks it `applying`, and only then does cancel actually
    write. The old code wrote a blind `status = "cancelled"` from its stale
    Python object; the fix is a bulk `UPDATE ... WHERE status IN (...)`,
    which leaves an already-`applying` row untouched because the row no
    longer matches the WHERE clause by the time the UPDATE runs."""
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as s1:
        r = await s1.scalar(select(AgentRun).where(AgentRun.id == run.id).with_for_update())
        assert r is not None

        async with maker() as s2:
            leased = await s2.scalar(
                select(AgentOpsRequest)
                .where(AgentOpsRequest.id == row.id, AgentOpsRequest.status == "approved")
                .with_for_update()
            )
            assert leased is not None
            leased.status = "applying"
            leased.lease_id = "leased-in-test"
            await s2.commit()

        await s1.execute(
            update(AgentOpsRequest)
            .where(
                AgentOpsRequest.agent_run_id == r.id,
                AgentOpsRequest.status.in_(("proposed", "approved")),
            )
            .values(status="cancelled")
        )
        r.status = "cancelled"
        await s1.commit()

    async with maker() as verify:
        final_row = await verify.get(AgentOpsRequest, row.id)
        assert final_row is not None
        assert final_row.status == "applying"
        assert final_row.lease_id == "leased-in-test"
        final_run = await verify.get(AgentRun, run.id)
        assert final_run is not None and final_run.status == "cancelled"


async def test_lease_skips_a_run_cancel_is_concurrently_locking(
    client: AsyncClient,
    engine: AsyncEngine,
    session: AsyncSession,
    project: Project,
    monkeypatch,
) -> None:
    """P2-2, the other direction: while another transaction holds the run's
    `FOR UPDATE` lock (as `cancel_agent_run` does for its whole body),
    `POST /agent-ops/lease` must never proceed past it — `skip_locked=True`
    on the joined `(AgentOpsRequest, AgentRun)` lock means it is skipped
    (204) rather than blocked or, worse, racing ahead of the cancel."""
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s1:
        locked = await s1.scalar(
            select(AgentRun).where(AgentRun.id == run.id).with_for_update()
        )
        assert locked is not None
        resp = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
        assert resp.status_code == 204
        await s1.commit()


# ---------------------------------------------------------- notified_at (P2-3)


async def test_result_sets_notified_at_none_on_applied_and_failed(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    run.notified_at = datetime.now(UTC)
    await session.commit()
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
    await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "applied"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    await session.refresh(run)
    assert run.notified_at is None


async def test_retry_does_not_touch_notified_at(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    run, _row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="approved"
    )
    sentinel = datetime.now(UTC) - timedelta(minutes=5)
    run.notified_at = sentinel
    await session.commit()
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "retry", "code": "busy"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.json()["status"] == "approved"
    await session.refresh(run)
    assert run.notified_at is not None


async def test_reclaim_ttl_keys_approved_rows_on_decided_at(
    session: AsyncSession, project: Project
) -> None:
    """P2-3: a `proposed` row's TTL clock starts at `created_at`; once
    approved, it restarts at `decided_at` — an owner who approves right
    before the deadline should not have their approval race the sweep."""
    task = await _make_task(session, project)
    stale_cutoff = datetime.now(UTC) - timedelta(hours=73)
    # Proposed a long time ago, but only just approved: must survive.
    _run, row = await _run_with_ops_row(
        session,
        task,
        run_status="ops_pending",
        row_status="approved",
        created_at=stale_cutoff,
        decided_at=datetime.now(UTC),
    )
    await agent_ops.reclaim_stale(session, datetime.now(UTC))
    await session.commit()
    await session.refresh(row)
    assert row.status == "approved"


async def test_reclaim_ttl_cancels_a_stale_approved_row_and_clears_notified_at(
    session: AsyncSession, project: Project
) -> None:
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(
        session,
        task,
        run_status="ops_pending",
        row_status="approved",
        decided_at=datetime.now(UTC) - timedelta(hours=73),
    )
    run.notified_at = datetime.now(UTC)
    await session.commit()
    await agent_ops.reclaim_stale(session, datetime.now(UTC))
    await session.commit()
    await session.refresh(row)
    await session.refresh(run)
    assert row.status == "cancelled"
    assert run.notified_at is None


async def test_reclaim_expired_applying_sets_notified_at_on_final_failure(
    session: AsyncSession, project: Project
) -> None:
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="applying", attempts=3
    )
    row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    run.notified_at = datetime.now(UTC)
    await session.commit()
    await agent_ops.reclaim_stale(session, datetime.now(UTC))
    await session.commit()
    await session.refresh(row)
    await session.refresh(run)
    assert row.status == "failed"
    assert run.status == "failed"
    assert run.notified_at is None


async def test_lease_reclaims_its_own_expired_applying_row_before_picking(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    """P3-9: a single `/lease` call recovers a stuck `applying` row and
    hands out a fresh lease in the same request — it never has to wait for
    the separate watchdog sweep."""
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(
        session, task, run_status="ops_pending", row_status="applying", attempts=1
    )
    row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    row.lease_id = "stale-lease"
    await session.commit()
    resp = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ops_id"] == row.id
    assert body["attempts"] == 2
    assert body["lease_id"] != "stale-lease"


# --------------------------------------------------------- misc P3 regressions


async def test_callback_idempotent_replay_for_ops_applied(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """P3-4: a crash-recovery resend of the original `ops_pending` callback
    after the run has already settled to `ops_applied` is a harmless replay,
    not an error."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": [_proposal()]},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    run = await _run_row(session, run_id)
    run.status = "ops_applied"
    await session.commit()
    replay = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": [_proposal()]},
        headers=_CALLBACK,  # no lease header — must not be required for a replay
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "ops_applied"


async def test_decision_denylist_branch_settles_the_run(
    session: AsyncSession, project: Project, manager: User, client: AsyncClient
) -> None:
    """P3-5: the last open row on an `ops_pending` run getting force-marked
    `invalid` by the approval-time denylist re-check must still settle the
    run (to `failed`), not leave it stuck waiting forever."""
    task = await _make_task(session, project)
    run, row = await _run_with_ops_row(
        session,
        task,
        run_status="ops_pending",
        row_status="proposed",
        key="ADMIN_TG_IDS",
    )
    # Simulate a bad row that slipped past `store_proposals` (defense in
    # depth only — `store_proposals` itself never lets this happen).
    row.key = "DATABASE_URL"
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
    assert resp.json()["detail"] == "denylisted"
    await session.refresh(row)
    await session.refresh(run)
    assert row.status == "invalid"
    assert run.status == "failed"


async def test_failed_callback_stores_allowed_proposals_as_cancelled(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """P3-6: an implement failure has nothing left to apply — an otherwise
    `proposed`-eligible row is stored `cancelled` with `policy_reason
    "run_failed"`, never leaving a dead approve button on a finished run,
    and `ops_controls_available` stays false."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "failed",
            "error": "Agent ishi xato bilan tugadi",
            "ops_requests": [_proposal()],
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    run = await _run_row(session, run_id)
    rows = await agent_ops.list_for_run(session, run.id)
    assert rows[0].status == "cancelled"
    assert rows[0].policy_reason == "run_failed"
    notices = await client.get(
        "/api/v1/agent-runs/notifications",
        headers={"X-Agent-Worker-Token": settings.service_token},
    )
    [notice] = [item for item in notices.json() if item["run_id"] == run_id]
    assert notice["ops_controls_available"] is False


async def test_decision_replay_after_run_cancelled_is_idempotent(
    session: AsyncSession, project: Project, manager: User, client: AsyncClient
) -> None:
    """P3-7: an approve that later got swept up in an unrelated run
    cancellation is still a valid replay of that same `action_id` — not a
    409 `conflict`."""
    task = await _make_task(session, project)
    _run, row = await _run_with_ops_row(session, task, run_status="ops_pending")
    action_id = str(uuid4())
    body = {"decision": "approve", "request_hash": row.request_hash, "action_id": action_id}
    first = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision", json=body, headers=auth(manager)
    )
    assert first.status_code == 200
    row.status = "cancelled"
    await session.commit()
    replay = await client.post(
        f"/api/v1/agent-ops/{row.id}/decision", json=body, headers=auth(manager)
    )
    assert replay.status_code == 200
    assert replay.json()["ops_request"]["status"] == "cancelled"


async def test_restart_services_and_control_chars_are_hardened(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """P3-8: a bad `restart_services` entry is a 422, and control characters
    in `reason`/`policy_reason` are stripped rather than stored verbatim."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    bad = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "ops_pending",
            "ops_requests": [_proposal(restart_services=["Qurbot Web!"])],
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert bad.status_code == 422

    ok = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "ops_pending",
            "ops_requests": [
                _proposal(reason="add\x00admin now", restart_services=["qurbot-web"])
            ],
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert ok.status_code == 200
    run = await _run_row(session, run_id)
    rows = await agent_ops.list_for_run(session, run.id)
    assert rows[0].reason == "addadminnow"


async def test_result_code_status_consistency_enforced(
    client: AsyncClient, session: AsyncSession, project: Project, monkeypatch
) -> None:
    """P3-8: `status`/`code` combinations outside the fixed vocabulary's
    pairing (e.g. `applied` with a `busy` code) are rejected as 422."""
    _configure_tokens(monkeypatch)
    task = await _make_task(session, project)
    await _run_with_ops_row(session, task, run_status="ops_pending", row_status="approved")
    leased = await client.post("/api/v1/agent-ops/lease", headers=_SVC)
    ops_id, lease_id = leased.json()["ops_id"], leased.json()["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-ops/{ops_id}/result",
        json={"status": "applied", "code": "busy"},
        headers={**_SVC, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 422


def test_ops_request_out_excludes_lease_fields() -> None:
    """P3-12: the ops-apply lease is svc-only (`AgentOpsWorkOut`); the
    owner-facing `AgentOpsRequestOut` (embedded in `AgentRunDetailOut` and
    `AgentNotificationOut`) must never carry it."""
    from app.schemas.agent_run import AgentOpsRequestOut

    assert "lease_id" not in AgentOpsRequestOut.model_fields
    assert "lease_until" not in AgentOpsRequestOut.model_fields


async def test_task_delete_blocked_while_ops_pending(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """P3-12: an `ops_pending` run counts as an active run for the
    task-delete guard, same as `pr_opened`/`pr_ready`."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "ops_pending", "ops_requests": [_proposal()]},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    run = await _run_row(session, run_id)
    resp = await client.delete(f"/api/v1/tasks/{run.task_id}", headers=auth(manager))
    assert resp.status_code == 409
