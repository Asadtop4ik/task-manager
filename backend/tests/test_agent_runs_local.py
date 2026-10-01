"""The local agent-svc executor: leasing, heartbeats, stage reports, and the
GitHub-path divergences (`/callback`, `/ci-result`, corrections, `/cancel`)
that only apply to a run with `executor == "local"`."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import agent_runs
from app.core.config import Settings, settings
from app.db.models import AgentEvent, AgentRun, AgentRunAction, Project, Task, User
from app.schemas.agent_intake import IntakeBrief
from app.schemas.agent_run import AgentLeaseRequest, AgentWorkOut
from tests.conftest import auth

_SHA = "a" * 40
_PR_URL = "https://github.com/Asadtop4ik/task-manager/pull/9"
_CALLBACK = {"X-Agent-Callback-Token": "test-callback-token"}
_SVC = {"X-Agent-Svc-Token": "x" * 32}


def _worker() -> dict[str, str]:
    return {"X-Agent-Worker-Token": settings.service_token}


async def _ready_project(session: AsyncSession, project: Project) -> None:
    project.key = "task-manager"
    project.repo_full_name = "Asadtop4ik/task-manager"
    project.default_branch = "main"
    await session.commit()


def _local_setup(monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_agent_token", "test-github-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")
    monkeypatch.setattr(settings, "agent_svc_token", "x" * 32)
    monkeypatch.setattr(settings, "agent_local_executor_projects", frozenset({"task-manager"}))


async def _run_row(session: AsyncSession, run_id: str) -> AgentRun:
    row = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    assert row is not None
    return row


async def _local_run(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> str:
    """Create a task and delegate it; assert it took the local-executor path."""
    await _ready_project(session, project)
    _local_setup(monkeypatch)

    async def fail_dispatch(run, task) -> int:
        raise AssertionError("a local-executor run must never call repository_dispatch")

    monkeypatch.setattr(agent_runs, "_dispatch", fail_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix the menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    started = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert started.status_code == 201
    assert started.json()["executor"] == "local"
    assert started.json()["status"] == "dispatched"
    return started.json()["run_id"]


async def _open_local_pr(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> tuple[str, str]:
    """Create a local run, take its implement lease, and report pr_opened."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    lease_id = leased.json()["lease_id"]

    async def fake_verify_pr(run, number, sha) -> None:
        assert sha == _SHA

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    opened = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "pr_opened", "pr_url": _PR_URL, "head_sha": _SHA},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert opened.status_code == 200
    assert opened.json()["status"] == "pr_opened"
    return run_id, lease_id


async def _mark_ci_green(client: AsyncClient, monkeypatch, run_id: str) -> None:
    async def fake_verify_pr(run, number, sha) -> None:
        assert sha == _SHA

    async def fake_verify_pr_ci(run, sha, conclusion, url) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    monkeypatch.setattr(agent_runs, "_verify_pr_ci", fake_verify_pr_ci)
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/ci-result",
        json={
            "sha": _SHA,
            "conclusion": "success",
            "github_run_url": "https://github.com/Asadtop4ik/task-manager/actions/runs/1",
        },
        headers=_CALLBACK,
    )
    assert resp.status_code == 200


# --------------------------------------------------------------- executor selection


async def test_local_project_dispatches_locally_without_github(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _local_run(client, session, manager, project, monkeypatch)


async def test_flag_off_keeps_the_github_path(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    monkeypatch.setattr(settings, "github_agent_token", "test-github-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")
    # agent_local_executor_projects left at its default: empty.
    assert settings.agent_local_executor_projects == frozenset()

    async def fake_dispatch(run, task) -> int:
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix the menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    started = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert started.status_code == 201
    assert started.json()["executor"] == "github"


async def test_fast_mode_never_uses_the_local_executor(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _local_setup(monkeypatch)
    monkeypatch.setattr(settings, "agent_fast_enabled", True)
    dispatched: list[str] = []

    async def fake_dispatch(run, task) -> int:
        dispatched.append(run.run_id)
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix the menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    started = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"mode": "fast"}, headers=auth(manager)
    )
    assert started.status_code == 201
    assert started.json()["executor"] == "github"
    assert dispatched == [started.json()["run_id"]]


# --------------------------------------------------------------- lease endpoint


async def test_lease_is_hidden_when_the_token_setting_is_empty(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "agent_svc_token", "")
    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 404


async def test_lease_returns_204_when_nothing_to_do(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_svc_token", "x" * 32)
    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 204


async def test_lease_rejects_an_invalid_lane_with_no_side_effects(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """agent-svc's own self-check relies on an invalid body never touching
    the database: FastAPI must reject it before the handler (and its auth
    check) ever runs."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    resp = await client.post(
        "/api/v1/agent-runs/lease", json={"lane": "not-code"}, headers=_SVC
    )
    assert resp.status_code == 422
    row = await _run_row(session, run_id)
    assert row.status == "dispatched"
    assert row.lease_id is None
    assert row.attempts == 0

    ok = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert ok.status_code == 200
    assert ok.json()["run_id"] == run_id
    assert ok.json()["attempts"] == 1


async def test_lease_rejects_a_wrong_token_when_the_feature_is_enabled(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "agent_svc_token", "x" * 32)
    resp = await client.post(
        "/api/v1/agent-runs/lease",
        json={"lane": "code"},
        headers={"X-Agent-Svc-Token": "y" * 32},
    )
    assert resp.status_code == 401


def test_agent_svc_auth_rejects_a_non_ascii_token_without_crashing(monkeypatch) -> None:
    # httpx (and most real clients/proxies) refuse to even transmit a
    # non-ASCII header value, so this exercises the auth function directly:
    # hmac.compare_digest raises TypeError for a non-ASCII `str` operand,
    # and that must become a 401, not an unhandled 500.
    monkeypatch.setattr(settings, "agent_svc_token", "x" * 32)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        agent_runs._agent_svc_auth("café" + "x" * 28)
    assert excinfo.value.status_code == 401


async def test_two_concurrent_lease_calls_only_one_wins(
    client: AsyncClient,
    engine,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
) -> None:
    """Two independent DB sessions racing `with_for_update(skip_locked=True)`
    against the same real Postgres must never hand out the same run twice."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s1, maker() as s2:
        results = await asyncio.gather(
            agent_runs.lease_agent_work(AgentLeaseRequest(lane="code"), s1, "x" * 32),
            agent_runs.lease_agent_work(AgentLeaseRequest(lane="code"), s2, "x" * 32),
        )
    outcomes = [
        "leased" if isinstance(result, AgentWorkOut) else result.status_code
        for result in results
    ]
    assert sorted(outcomes, key=str) == sorted(["leased", 204], key=str)
    leased_result = next(result for result in results if isinstance(result, AgentWorkOut))
    assert leased_result.run_id == run_id


async def test_implement_lease_marks_running_and_increments_attempts_then_skip_locked(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "implement"
    assert body["run_id"] == run_id
    assert body["attempts"] == 1
    assert body["attempt_index"] == 1
    assert body["branch"]
    assert body["pr_url"] is None
    assert body["head_sha"] is None
    assert body["action_id"] is None
    assert body["complexity"] is None
    assert body["relevant_files"] == []

    row = await _run_row(session, run_id)
    assert row.status == "running"
    assert row.lease_id == body["lease_id"]
    assert row.runner_started_at is not None

    events = (
        await session.scalars(
            select(AgentEvent.status)
            .join(AgentRun, AgentEvent.agent_run_id == AgentRun.id)
            .where(AgentRun.run_id == run_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events == ["pending", "dispatched", "running"]

    # The only local run now holds a live lease: a second lease call must
    # skip it (SKIP LOCKED / no-live-lease filter) and find nothing else.
    second = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert second.status_code == 204


async def test_unedited_task_is_leased_with_the_exact_title_and_description(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    _local_setup(monkeypatch)

    async def fail_dispatch(run, task) -> int:
        raise AssertionError("a local-executor run must never call repository_dispatch")

    monkeypatch.setattr(agent_runs, "_dispatch", fail_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Exact approved title",
            "description": "Exact approved body",
        },
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    started = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert started.status_code == 201

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    assert leased.json()["title"] == "Exact approved title"
    assert leased.json()["description"] == "Exact approved body"


async def test_task_edited_after_dispatch_fails_the_implement_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Security fix: a task stays editable while its run is queued to be
    leased. Anyone with edit rights changing the title/description after the
    owner approved dispatch must not have that new text handed to agent-svc
    as if it were approved — the run must fail instead."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    task_id = row.task_id

    edited = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"title": "Sneaky new scope after approval"},
        headers=auth(manager),
    )
    assert edited.status_code == 200

    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 204

    failed = await _run_row(session, run_id)
    assert failed.status == "failed"
    assert failed.error == "Task tasdiqlangandan keyin o'zgartirildi; qayta yuboring."
    assert failed.lease_id is None
    assert failed.notified_at is None

    events = (
        await session.scalars(
            select(AgentEvent.status)
            .join(AgentRun, AgentEvent.agent_run_id == AgentRun.id)
            .where(AgentRun.run_id == run_id)
            .order_by(AgentEvent.id)
        )
    ).all()
    assert events[-1] == "failed"


async def test_stale_task_revision_is_skipped_and_the_next_run_is_leased(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """The lease scan must move on to the next candidate instead of
    returning the stale run (or 204-ing while good work waits behind it)."""
    stale_run_id = await _local_run(client, session, manager, project, monkeypatch)
    good_run_id = await _local_run(client, session, manager, project, monkeypatch)

    stale_row = await _run_row(session, stale_run_id)
    edited = await client.patch(
        f"/api/v1/tasks/{stale_row.task_id}",
        json={"description": "changed after the owner approved it"},
        headers=auth(manager),
    )
    assert edited.status_code == 200

    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 200
    assert resp.json()["run_id"] == good_run_id

    failed = await _run_row(session, stale_run_id)
    assert failed.status == "failed"
    good = await _run_row(session, good_run_id)
    assert good.status == "running"


async def test_heartbeat_extends_lease_then_detects_mismatch_and_cancellation(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    old_until = leased.json()["lease_until"]

    ok = await client.post(
        f"/api/v1/agent-runs/{run_id}/heartbeat",
        json={"lease_id": lease_id},
        headers=_SVC,
    )
    assert ok.status_code == 200
    assert ok.json()["lease_until"] >= old_until

    mismatch = await client.post(
        f"/api/v1/agent-runs/{run_id}/heartbeat",
        json={"lease_id": "not-the-lease"},
        headers=_SVC,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "lease_mismatch"

    cancelled = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=auth(manager))
    assert cancelled.status_code == 200
    after_cancel = await client.post(
        f"/api/v1/agent-runs/{run_id}/heartbeat",
        json={"lease_id": lease_id},
        headers=_SVC,
    )
    assert after_cancel.status_code == 409
    assert after_cancel.json()["detail"] == "cancelled"


async def test_heartbeat_reports_lease_expired_distinctly_from_mismatch(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    expired = await client.post(
        f"/api/v1/agent-runs/{run_id}/heartbeat",
        json={"lease_id": lease_id},
        headers=_SVC,
    )
    assert expired.status_code == 409
    assert expired.json()["detail"] == "lease_expired"


async def test_heartbeat_refuses_past_the_total_lease_duration_cap(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """A worker that keeps heartbeating without ever finishing must still be
    made to give the lease up once its hard total-duration cap passes, even
    though each individual heartbeat renewed lease_until just fine."""
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    row = await _run_row(session, run_id)
    # lease_until is still comfortably in the future; only the issuance time
    # is old enough to trip the 60-minute cap.
    row.lease_issued_at = datetime.now(UTC) - timedelta(minutes=61)
    await session.commit()

    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/heartbeat",
        json={"lease_id": lease_id},
        headers=_SVC,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "lease_expired"


async def test_lease_reclaims_a_lease_past_its_total_duration_cap_even_if_renewed(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    row = await _run_row(session, run_id)
    # lease_until was just renewed (not stale by itself); only the total
    # duration since issuance has passed the cap.
    row.lease_until = datetime.now(UTC) + timedelta(minutes=4)
    row.lease_issued_at = datetime.now(UTC) - timedelta(minutes=61)
    await session.commit()

    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 200
    assert resp.json()["run_id"] == run_id
    # Reclaimed (attempts=1, below the implement cap) and immediately
    # re-leased within this same call: attempts is now 2.
    assert resp.json()["attempts"] == 2


async def test_stage_report_is_recorded_and_requires_a_matching_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    ok = await client.post(
        f"/api/v1/agent-runs/{run_id}/stage",
        json={"lease_id": lease_id, "stage": "workspace_ready"},
        headers=_SVC,
    )
    assert ok.status_code == 204
    rows = (
        await session.scalars(
            select(AgentEvent)
            .join(AgentRun, AgentEvent.agent_run_id == AgentRun.id)
            .where(AgentRun.run_id == run_id, AgentEvent.phase == "stage")
        )
    ).all()
    assert len(rows) == 1 and rows[0].status == "workspace_ready"

    mismatch = await client.post(
        f"/api/v1/agent-runs/{run_id}/stage",
        json={"lease_id": "wrong", "stage": "codex_started"},
        headers=_SVC,
    )
    assert mismatch.status_code == 409


async def test_expired_implement_lease_is_retried_then_fails_after_two_attempts(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    first = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert first.json()["attempts"] == 1

    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    second = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert second.status_code == 200
    assert second.json()["kind"] == "implement"
    assert second.json()["attempts"] == 2
    assert second.json()["run_id"] == run_id

    row2 = await _run_row(session, run_id)
    row2.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    third = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert third.status_code == 204
    failed = await _run_row(session, run_id)
    assert failed.status == "failed"
    assert failed.error == "agent-svc javob bermadi"
    assert failed.lease_id is None


# --------------------------------------------------------------- callback / ci / review


async def test_callback_requires_the_matching_lease_for_local_runs(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    async def fake_verify_pr(run, number, sha) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    body = {"run_id": run_id, "status": "pr_opened", "pr_url": _PR_URL, "head_sha": _SHA}

    missing = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback", json=body, headers=_CALLBACK
    )
    assert missing.status_code == 409

    wrong = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json=body,
        headers={**_CALLBACK, "X-Agent-Lease-ID": "not-it"},
    )
    assert wrong.status_code == 409

    ok = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json=body,
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "pr_opened"
    row = await _run_row(session, run_id)
    assert row.lease_id is None


async def test_callback_rejects_an_expired_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    body = {"run_id": run_id, "status": "pr_opened", "pr_url": _PR_URL, "head_sha": _SHA}
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json=body,
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 409
    unchanged = await _run_row(session, run_id)
    assert unchanged.pr_url is None


async def test_ci_result_on_local_run_skips_external_review_and_review_lease_follows(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fail_external_review(run, pr_number) -> None:
        raise AssertionError("a local run must not dispatch an independent review")

    monkeypatch.setattr(agent_runs, "_dispatch_external_review", fail_external_review)
    await _mark_ci_green(client, monkeypatch, run_id)

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "review"
    assert body["run_id"] == run_id
    assert body["head_sha"] == _SHA
    assert body["pr_url"] == _PR_URL
    assert body["action_id"] is None
    assert body["correction_count"] == 0
    assert body["last_correction_instruction"] is None
    assert body["last_reviewed_sha"] is None


async def test_review_lease_after_a_correction_carries_its_instruction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    row = await _run_row(session, run_id)
    now = datetime.now(UTC)
    reviewed = "b" * 40
    for index, (status_, instruction, expected) in enumerate(
        [
            ("completed", "older fix", "c" * 40),
            ("completed", "newest fix", reviewed),
            ("rejected", "never applied", "d" * 40),
        ]
    ):
        session.add(
            AgentRunAction(
                action_id=str(uuid4()),
                agent_run_id=row.id,
                kind="correction",
                request_hash=str(index) * 64,
                request_data={"expected_head_sha": expected, "instruction": instruction},
                status=status_,
                created_at=now - timedelta(minutes=10 - index),
            )
        )
    await session.commit()

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "review"
    assert body["correction_count"] == 2
    assert body["last_correction_instruction"] == "newest fix"
    assert body["last_reviewed_sha"] == reviewed


async def test_review_lease_after_a_same_head_correction_has_no_reviewed_sha(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    row = await _run_row(session, run_id)
    session.add(
        AgentRunAction(
            action_id=str(uuid4()),
            agent_run_id=row.id,
            kind="correction",
            request_hash="9" * 64,
            request_data={"expected_head_sha": _SHA, "instruction": "reconsider"},
            status="completed",
        )
    )
    await session.commit()

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    body = leased.json()
    assert body["kind"] == "review"
    assert body["correction_count"] == 1
    assert body["last_reviewed_sha"] is None


async def test_review_result_clears_lease_and_recomputes_pr_ready(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    async def fake_verify_pr(run, number, sha) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    result = await client.post(
        f"/api/v1/agent-runs/{run_id}/review-result",
        json={"sha": _SHA, "state": "clean", "summary": "Looks fine.", "findings": []},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "pr_ready"
    row = await _run_row(session, run_id)
    assert row.lease_id is None


async def test_review_result_releases_a_matching_lease_on_a_stale_head(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    result = await client.post(
        f"/api/v1/agent-runs/{run_id}/review-result",
        json={"sha": "b" * 40, "state": "clean", "summary": "x", "findings": []},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert result.status_code == 409
    row = await _run_row(session, run_id)
    assert row.lease_id is None


async def test_review_result_releases_a_matching_lease_on_wrong_status(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Defense in depth for the review-result endpoint itself: even if the
    run has moved on from under a still-live review lease by some other
    path than the ones already closed off, a matching lease must never sit
    there burning a retry when the endpoint 409s."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    row = await _run_row(session, run_id)
    row.status = "correction_running"
    await session.commit()

    result = await client.post(
        f"/api/v1/agent-runs/{run_id}/review-result",
        json={"sha": _SHA, "state": "clean", "summary": "x", "findings": []},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert result.status_code == 409
    released = await _run_row(session, run_id)
    assert released.lease_id is None
    assert released.status == "correction_running"


def _run_stub(**overrides: object) -> AgentRun:
    base: dict[str, object] = {
        "pr_url": _PR_URL,
        "status": "pr_opened",
        "ci_status": "success",
        "ci_verified_sha": _SHA,
        "head_sha": _SHA,
        "review_sha": None,
    }
    base.update(overrides)
    return AgentRun(**base)


def test_review_needed_matches_the_lease_endpoint_sql_condition() -> None:
    # This is the exact boundary `/lease` step 2 encodes in SQL; kept here as
    # a plain-Python pin so the two definitions cannot silently drift apart.
    assert agent_runs._review_needed(_run_stub()) is True
    assert agent_runs._review_needed(_run_stub(status="pr_ready")) is True
    assert agent_runs._review_needed(_run_stub(pr_url=None)) is False
    assert agent_runs._review_needed(_run_stub(status="correction_running")) is False
    assert agent_runs._review_needed(_run_stub(ci_status="pending")) is False
    assert agent_runs._review_needed(_run_stub(ci_verified_sha=None)) is False
    assert agent_runs._review_needed(_run_stub(ci_verified_sha="b" * 40)) is False
    assert agent_runs._review_needed(_run_stub(review_sha=_SHA)) is False
    assert agent_runs._review_needed(_run_stub(review_sha="b" * 40)) is True


async def _persisted_run(
    session: AsyncSession, project: Project, **overrides: object
) -> AgentRun:
    task = Task(project_id=project.id, title="Race")
    session.add(task)
    await session.flush()
    base: dict[str, object] = {
        "run_id": str(uuid4()),
        "task_id": task.id,
        "task_revision": "r" * 64,
        "repo_full_name": "Asadtop4ik/task-manager",
        "base_branch": "main",
        "mode": "pr",
        "executor": "local",
        "status": "pr_opened",
        "pr_url": _PR_URL,
        "head_sha": _SHA,
        "ci_status": "success",
        "ci_verified_sha": _SHA,
    }
    base.update(overrides)
    run = AgentRun(**base)
    session.add(run)
    await session.commit()
    return await _run_row(session, run.run_id)


async def test_reclaim_does_not_finalize_a_review_when_a_correction_has_taken_over(
    session: AsyncSession, project: Project
) -> None:
    """Probed race (a): an owner correction request flips run.status to
    correction_running independent of any live review lease (a review
    never holds run.status). If that review lease's own expiry-cap reclaim
    still ran, it must never clobber the correction back to pr_opened or
    fabricate a review_status="error" on top of it."""
    await _ready_project(session, project)
    run = await _persisted_run(
        session,
        project,
        status="correction_running",
        review_sha=None,
        lease_kind="review",
        lease_id=str(uuid4()),
        lease_until=datetime.now(UTC) - timedelta(minutes=1),
        review_attempts=2,
        review_attempts_sha=_SHA,
    )

    await agent_runs._reclaim_expired_lease(session, run, datetime.now(UTC))
    await session.commit()

    row = await _run_row(session, run.run_id)
    assert row.status == "correction_running"
    assert row.review_status is None
    assert row.lease_id is None


async def test_reclaim_does_not_mark_a_never_reviewed_new_head_as_review_error(
    session: AsyncSession, project: Project
) -> None:
    """Probed race (b): a new head landed (e.g. a correction completed)
    while a stale review lease still tracked the old head's attempt count.
    That new head has never been reviewed at all and must not be finalized
    as a failed review just because the old lease's cap was reached."""
    await _ready_project(session, project)
    new_head = "c" * 40
    run = await _persisted_run(
        session,
        project,
        status="pr_opened",
        head_sha=new_head,
        review_sha=None,
        lease_kind="review",
        lease_id=str(uuid4()),
        lease_until=datetime.now(UTC) - timedelta(minutes=1),
        review_attempts=2,
        review_attempts_sha=_SHA,  # the OLD head this lease was tracking
    )
    assert run.review_attempts_sha != run.head_sha

    await agent_runs._reclaim_expired_lease(session, run, datetime.now(UTC))
    await session.commit()

    row = await _run_row(session, run.run_id)
    assert row.review_status is None
    assert row.head_sha == new_head
    assert row.lease_id is None


async def test_lease_finds_a_new_reviewable_run_behind_more_than_twenty_reviewed_ones(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Regression for a Python-side filter after a bounded SQL LIMIT: with 20
    already-reviewed local runs ahead of it in id order, a fresh reviewable
    run must still be found (the condition now lives entirely in SQL)."""
    await _ready_project(session, project)
    _local_setup(monkeypatch)
    for i in range(20):
        task = Task(project_id=project.id, title=f"Reviewed {i}")
        session.add(task)
        await session.flush()
        session.add(
            AgentRun(
                run_id=str(uuid4()),
                task_id=task.id,
                task_revision="r" * 64,
                repo_full_name="Asadtop4ik/task-manager",
                base_branch="main",
                mode="pr",
                status="pr_ready",
                executor="local",
                ci_status="success",
                ci_verified_sha=_SHA,
                head_sha=_SHA,
                pr_url=f"https://github.com/Asadtop4ik/task-manager/pull/{100 + i}",
                review_status="clean",
                review_sha=_SHA,
            )
        )
    await session.commit()

    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    assert leased.json()["kind"] == "review"
    assert leased.json()["run_id"] == run_id


# --------------------------------------------------------------- corrections / cancel


async def test_correction_request_releases_a_live_review_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """A live review lease never blocks run.status (it stays pr_opened the
    whole time), so a correction request can legitimately arrive while one
    is outstanding. It must release that lease immediately rather than
    leaving it to expire and later clobber the correction's state."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    review_lease = await client.post(
        "/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC
    )
    assert review_lease.json()["kind"] == "review"

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    action_id = str(uuid4())
    correction = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": action_id, "instruction": "fix it"},
        headers=auth(manager),
    )
    assert correction.status_code == 200
    assert correction.json()["status"] == "in_progress"

    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.lease_id is None  # the review lease was released, not left dangling
    assert row.lease_kind is None

    # A second correction request is rejected — there is exactly one
    # in-progress correction action, never two.
    second = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4()), "instruction": "again"},
        headers=auth(manager),
    )
    assert second.status_code == 409

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    assert leased.json()["kind"] == "correction"
    assert leased.json()["action_id"] == action_id


async def test_local_correction_is_leased_instead_of_dispatched(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fail_dispatch_release(run, action, payload) -> None:
        raise AssertionError("a local correction must not use repository_dispatch")

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_dispatch_release_action", fail_dispatch_release)
    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)

    action_id = str(uuid4())
    req = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={
            "expected_head_sha": _SHA,
            "action_id": action_id,
            "instruction": "Please rename this helper.",
        },
        headers=auth(manager),
    )
    assert req.status_code == 200
    # The run/action transition immediately, exactly like a successful
    # GitHub dispatch would — agent-svc has not leased it yet.
    assert req.json()["status"] == "in_progress"
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "correction"
    assert body["action_id"] == action_id
    assert body["instruction"] == "Please rename this helper."
    assert body["expected_head_sha"] == _SHA
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"


async def test_correction_lease_carries_the_ci_status_and_run_url(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """agent-svc fetches the failed job log for a correction from `ci_url`,
    and only when `ci_status` is `failure` for the leased head."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    row = await _run_row(session, run_id)
    ci_url = "https://github.com/Asadtop4ik/task-manager/actions/runs/42"
    row.ci_status = "failure"
    row.ci_url = ci_url
    await session.commit()

    req = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={
            "expected_head_sha": _SHA,
            "action_id": str(uuid4()),
            "instruction": "CI failed",
        },
        headers=auth(manager),
    )
    assert req.status_code == 200

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "correction"
    assert body["head_sha"] == _SHA
    assert body["ci_status"] == "failure"
    assert body["ci_url"] == ci_url


async def test_task_edited_while_a_correction_is_queued_rejects_only_the_correction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """`_issue_lease` reports the task's current title/description for a
    correction lease too, so the same staleness check applies. The PR is
    already open, so only the correction is rejected; the run goes back to
    `pr_opened` instead of failing."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    action_id = str(uuid4())
    correction = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": action_id, "instruction": "fix it"},
        headers=auth(manager),
    )
    assert correction.status_code == 200

    row = await _run_row(session, run_id)
    edited = await client.patch(
        f"/api/v1/tasks/{row.task_id}",
        json={"title": "Changed after the correction was requested"},
        headers=auth(manager),
    )
    assert edited.status_code == 200

    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 204

    kept = await _run_row(session, run_id)
    assert kept.status == "pr_opened"
    assert kept.lease_id is None

    action = (
        await session.scalars(
            select(AgentRunAction).where(AgentRunAction.action_id == action_id)
        )
    ).first()
    assert action is not None
    assert action.status == "rejected"
    assert action.result == {
        "message": "Task tasdiqlangandan keyin o'zgartirildi; qayta yuboring."
    }


async def test_cancel_and_merge_are_rejected_while_a_local_correction_is_queued(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """A local correction must transition the run immediately (blocker fix):
    otherwise the PR sits "open" while queued, and a concurrent cancel,
    merge, or second correction request could interfere before agent-svc
    ever gets to lease it."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]
    review_result = await client.post(
        f"/api/v1/agent-runs/{run_id}/review-result",
        json={"sha": _SHA, "state": "clean", "summary": "Looks fine.", "findings": []},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert review_result.status_code == 200
    assert review_result.json()["status"] == "pr_ready"

    correction_id = str(uuid4())
    correction = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": correction_id, "instruction": "fix it"},
        headers=auth(manager),
    )
    assert correction.status_code == 200
    assert correction.json()["status"] == "in_progress"

    cancel = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=auth(manager))
    assert cancel.status_code == 409

    merge = await client.post(
        f"/api/v1/agent-runs/{run_id}/merge",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4())},
        headers=auth(manager),
    )
    assert merge.status_code == 409

    second_correction = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4()), "instruction": "again"},
        headers=auth(manager),
    )
    assert second_correction.status_code == 409


async def test_local_correction_action_result_requires_lease_then_clears_it(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_dispatch_release_action", lambda *a: None)
    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    action_id = str(uuid4())
    await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": action_id, "instruction": "fix it"},
        headers=auth(manager),
    )
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    lease_id = leased.json()["lease_id"]

    result_body = {"action_id": action_id, "status": "completed", "head_sha": _SHA}
    missing = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result", json=result_body, headers=_CALLBACK
    )
    assert missing.status_code == 409

    ok = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json=result_body,
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert ok.status_code == 200
    row = await _run_row(session, run_id)
    assert row.lease_id is None

    # An idempotent replay after the lease is gone must not need it again.
    replay = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result", json=result_body, headers=_CALLBACK
    )
    assert replay.status_code == 200


async def test_cancel_local_run_never_calls_github_cancel(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)

    async def fail_cancel(run) -> None:
        raise AssertionError("a local run must not call GitHub to cancel a workflow run")

    monkeypatch.setattr(agent_runs, "_cancel_github", fail_cancel)
    resp = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=auth(manager))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    row = await _run_row(session, run_id)
    assert row.lease_id is None


# --------------------------------------------------------------- watchdog


async def test_watchdog_fails_a_dispatched_run_that_agent_svc_never_leased(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    row.created_at = datetime.now(UTC) - timedelta(minutes=31)
    await session.commit()

    resp = await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    assert resp.status_code == 200
    failed = await _run_row(session, run_id)
    assert failed.status == "failed"
    assert failed.error == "agent-svc javob bermadi"


async def test_watchdog_leaves_github_runs_alone_no_matter_how_stale(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    await _ready_project(session, project)
    monkeypatch.setattr(settings, "github_agent_token", "test-github-token")
    monkeypatch.setattr(settings, "agent_callback_token", "test-callback-token")

    async def fake_dispatch(run, task) -> int:
        return 204

    monkeypatch.setattr(agent_runs, "_dispatch", fake_dispatch)
    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Fix the menu"},
        headers=auth(manager),
    )
    task_id = created.json()["id"]
    started = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert started.json()["executor"] == "github"
    run_id = started.json()["run_id"]

    row = await _run_row(session, run_id)
    row.created_at = datetime.now(UTC) - timedelta(hours=2)
    row.lease_id = "not-a-real-lease"
    row.lease_until = datetime.now(UTC) - timedelta(hours=1)
    row.lease_kind = "implement"
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    unchanged = await _run_row(session, run_id)
    assert unchanged.status == "dispatched"
    assert unchanged.lease_id == "not-a-real-lease"


async def test_watchdog_releases_a_single_stale_implement_lease_without_failing(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.json()["attempts"] == 1
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    released = await _run_row(session, run_id)
    assert released.status == "dispatched"
    assert released.lease_id is None
    assert released.attempts == 1


async def test_watchdog_fails_an_implement_run_after_two_stale_leases(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()
    await client.get("/api/v1/agent-runs/notifications", headers=_worker())

    second = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert second.json()["attempts"] == 2
    row2 = await _run_row(session, run_id)
    row2.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    failed = await _run_row(session, run_id)
    assert failed.status == "failed"
    assert failed.error == "agent-svc javob bermadi"
    assert failed.lease_id is None


async def test_watchdog_releases_a_single_stale_review_lease_leaving_the_pr_open(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.json()["kind"] == "review"
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    released = await _run_row(session, run_id)
    assert released.lease_id is None
    assert released.status == "pr_opened"
    assert released.review_status != "error"
    assert released.review_attempts == 1


async def test_watchdog_marks_review_error_after_two_stale_review_leases(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()
    await client.get("/api/v1/agent-runs/notifications", headers=_worker())

    second = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert second.json()["kind"] == "review"
    row2 = await _run_row(session, run_id)
    row2.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    finalized = await _run_row(session, run_id)
    assert finalized.lease_id is None
    assert finalized.status == "pr_opened"
    assert finalized.review_status == "error"
    assert finalized.review_sha == _SHA


async def test_watchdog_releases_a_single_stale_correction_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4()), "instruction": "fix"},
        headers=auth(manager),
    )
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.json()["kind"] == "correction"
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    released = await _run_row(session, run_id)
    assert released.lease_id is None
    assert released.status == "correction_running"


async def test_watchdog_rejects_correction_after_two_stale_correction_leases(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4()), "instruction": "fix"},
        headers=auth(manager),
    )
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()
    await client.get("/api/v1/agent-runs/notifications", headers=_worker())

    second = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert second.json()["kind"] == "correction"
    row2 = await _run_row(session, run_id)
    row2.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    finalized = await _run_row(session, run_id)
    assert finalized.lease_id is None
    assert finalized.status == "pr_opened"


async def test_watchdog_liveness_rule_spares_a_queued_dispatched_run(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """A run queued behind other healthy local work must not be failed just
    because it has waited past the age threshold."""
    busy_run_id = await _local_run(client, session, manager, project, monkeypatch)
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    busy = await _run_row(session, busy_run_id)
    assert busy.heartbeat_at is not None  # freshly leased: still "alive" evidence

    created = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.id, "title": "Second"},
        headers=auth(manager),
    )
    queued_task_id = created.json()["id"]
    queued = await client.post(
        f"/api/v1/agent-runs/tasks/{queued_task_id}", headers=auth(manager)
    )
    queued_run_id = queued.json()["run_id"]
    row = await _run_row(session, queued_run_id)
    row.created_at = datetime.now(UTC) - timedelta(minutes=31)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    still_queued = await _run_row(session, queued_run_id)
    assert still_queued.status == "dispatched"

    # Once the busy job's heartbeat is also stale, agent-svc looks dead and
    # the queued run is failed.
    busy_row = await _run_row(session, busy_run_id)
    busy_row.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    busy_row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()
    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    now_failed = await _run_row(session, queued_run_id)
    assert now_failed.status == "failed"


async def test_watchdog_times_out_a_correction_that_was_never_leased(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """agent-svc died before ever calling /lease for a queued local
    correction: the same liveness-gated timeout as an unclaimed dispatched
    run rejects the action and releases the run — it does not fail the
    whole run outright, since there is an open PR to protect."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    action_id = str(uuid4())
    await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": action_id, "instruction": "fix"},
        headers=auth(manager),
    )
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.lease_id is None
    row.updated_at = datetime.now(UTC) - timedelta(minutes=31)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    timed_out = await _run_row(session, run_id)
    assert timed_out.status == "pr_opened"

    action = (
        await session.scalars(
            select(AgentRunAction).where(
                AgentRunAction.agent_run_id == timed_out.id,
                AgentRunAction.action_id == action_id,
            )
        )
    ).first()
    assert action is not None
    assert action.status == "rejected"


# ------------------------------------- correction head race / orphaned corrections

_NEW_SHA = "b" * 40


async def _request_correction(
    client: AsyncClient, manager: User, run_id: str, expected: str = _SHA
) -> tuple[str, dict]:
    action_id = str(uuid4())
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": expected, "action_id": action_id, "instruction": "fix"},
        headers=auth(manager),
    )
    return action_id, {"status_code": resp.status_code, **resp.json()}


async def _lease_correction(client: AsyncClient) -> dict:
    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    assert leased.json()["kind"] == "correction"
    return leased.json()


async def _action_row(session: AsyncSession, action_id: str) -> AgentRunAction:
    await session.rollback()  # see the API's committed writes, not a stale snapshot
    row = await session.scalar(
        select(AgentRunAction)
        .where(AgentRunAction.action_id == action_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def test_correction_result_waits_for_a_lagging_github_pr_head(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Production 2026-09-30 (ab51aab4): GitHub's PR API still returned the old
    head right after agent-svc pushed, so a correct report was refused."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    calls = {"n": 0}

    async def request_time_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", request_time_head)
    action_id, created = await _request_correction(client, manager, run_id)
    assert created["status_code"] == 200
    work = await _lease_correction(client)

    async def lagging_head(run) -> tuple[str, bool]:
        calls["n"] += 1
        return (_SHA if calls["n"] <= 2 else _NEW_SHA), True

    monkeypatch.setattr(agent_runs, "_current_pr_head", lagging_head)
    monkeypatch.setattr(agent_runs, "_PR_HEAD_SETTLE_DELAYS_S", (0.0, 0.0, 0.0, 0.0))
    done = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json={"action_id": action_id, "status": "completed", "head_sha": _NEW_SHA},
        headers={**_CALLBACK, "X-Agent-Lease-ID": work["lease_id"]},
    )
    assert done.status_code == 200, done.text
    assert calls["n"] == 3  # two stale reads, then the pushed head
    row = await _run_row(session, run_id)
    assert row.head_sha == _NEW_SHA
    assert row.status == "pr_opened"
    assert row.lease_id is None
    assert (await _action_row(session, action_id)).status == "completed"


async def test_correction_result_still_409s_when_github_never_reports_the_head(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """The security check is unchanged: a head GitHub never shows as the PR
    head is refused after the bounded wait, the lease stays live, and the same
    report succeeds once GitHub catches up."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    calls = {"n": 0, "head": _SHA}

    async def current_head(run) -> tuple[str, bool]:
        calls["n"] += 1
        return calls["head"], True

    monkeypatch.setattr(agent_runs, "_current_pr_head", current_head)
    monkeypatch.setattr(agent_runs, "_PR_HEAD_SETTLE_DELAYS_S", (0.0, 0.0, 0.0, 0.0))
    action_id, _created = await _request_correction(client, manager, run_id)
    work = await _lease_correction(client)
    headers = {**_CALLBACK, "X-Agent-Lease-ID": work["lease_id"]}
    body = {"action_id": action_id, "status": "completed", "head_sha": _NEW_SHA}

    calls["n"] = 0
    refused = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result", json=body, headers=headers
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "correction PR head changed before recording"
    assert calls["n"] == 5  # 1 + one re-read per settle delay: bounded
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.lease_id == work["lease_id"]  # not a lost lease

    calls["head"] = _NEW_SHA
    retried = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result", json=body, headers=headers
    )
    assert retried.status_code == 200
    assert (await _run_row(session, run_id)).head_sha == _NEW_SHA


async def test_run_refresh_during_a_correction_does_not_orphan_its_action(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """The owner UI polls GET /agent-runs/{id}. It saw the correction's own
    push as a "new head" and flipped the run to pr_opened under the still
    in_progress action, which then could not be recorded nor re-leased."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, _created = await _request_correction(client, manager, run_id)
    work = await _lease_correction(client)

    async def pushed_head(run) -> tuple[str, bool]:
        return _NEW_SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", pushed_head)
    detail = await client.get(f"/api/v1/agent-runs/{run_id}", headers=auth(manager))
    assert detail.status_code == 200
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.head_sha == _SHA

    done = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json={"action_id": action_id, "status": "completed", "head_sha": _NEW_SHA},
        headers={**_CALLBACK, "X-Agent-Lease-ID": work["lease_id"]},
    )
    assert done.status_code == 200, done.text
    assert (await _run_row(session, run_id)).head_sha == _NEW_SHA


async def test_second_correction_after_a_stuck_first_is_leased_and_processed(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Run ab51aab4 replay: correction #1's lease died unreported while its
    commit landed; the owner's correction #2 (against the new head) must
    supersede it and be leased, never sit in_progress forever."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    first_id, _ = await _request_correction(client, manager, run_id)
    await _lease_correction(client)

    # While #1's lease is live, a second request is refused: never two at once.
    blocked_id, blocked = await _request_correction(client, manager, run_id)
    assert blocked["status_code"] == 409
    assert await session.get(AgentRunAction, blocked_id) is None

    # agent-svc dies without reporting; the lease expires. GitHub moved on.
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    async def new_head(run) -> tuple[str, bool]:
        return _NEW_SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", new_head)
    second_id, second = await _request_correction(client, manager, run_id, _NEW_SHA)
    assert second["status_code"] == 200, second
    assert second["status"] == "in_progress"

    first = await _action_row(session, first_id)
    assert first.status == "rejected"
    assert first.result == {"message": agent_runs._CORRECTION_SUPERSEDED_ERROR}
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.head_sha == _NEW_SHA

    work = await _lease_correction(client)
    assert work["action_id"] == second_id
    assert work["expected_head_sha"] == _NEW_SHA


async def test_in_progress_correction_on_a_drifted_run_is_leased_again(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Rows already corrupted in production: in_progress correction, run back
    at pr_opened, no lease. /lease must pick it up instead of ignoring it."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, _ = await _request_correction(client, manager, run_id)
    row = await _run_row(session, run_id)
    row.status = "pr_opened"
    await session.commit()

    work = await _lease_correction(client)
    assert work["action_id"] == action_id
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"
    assert row.lease_kind == "correction"
    assert (await _action_row(session, action_id)).attempts == 1


async def test_a_new_correction_supersedes_an_orphaned_one_and_is_leased(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    first_id, _ = await _request_correction(client, manager, run_id)
    row = await _run_row(session, run_id)
    row.status = "pr_opened"  # drifted: in_progress action, run no longer correction_running
    await session.commit()

    second_id, second = await _request_correction(client, manager, run_id)
    assert second["status_code"] == 200
    assert (await _action_row(session, first_id)).status == "rejected"
    work = await _lease_correction(client)
    assert work["action_id"] == second_id


async def test_lease_never_hands_out_two_in_progress_corrections_of_one_run(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Rows written before the one-at-a-time guard can hold two in_progress
    corrections: only the newest is leased, the older is rejected."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    first_id, _ = await _request_correction(client, manager, run_id)
    row = await _run_row(session, run_id)
    second = AgentRunAction(
        action_id=str(uuid4()),
        agent_run_id=row.id,
        kind="correction",
        request_hash="0" * 64,
        request_data={"expected_head_sha": _SHA, "instruction": "again"},
        status="in_progress",
    )
    session.add(second)
    await session.commit()
    second_id = second.action_id
    first = await _action_row(session, first_id)
    first.created_at = datetime.now(UTC) - timedelta(minutes=5)
    # An `accepted` duplicate is outside the lease scan's page entirely: the
    # bulk supersede after the pick is what guarantees it cannot survive.
    hidden = AgentRunAction(
        action_id=str(uuid4()),
        agent_run_id=row.id,
        kind="correction",
        request_hash="2" * 64,
        request_data={"expected_head_sha": _SHA, "instruction": "hidden"},
        status="accepted",
    )
    session.add(hidden)
    await session.commit()
    hidden_id = hidden.action_id

    work = await _lease_correction(client)
    assert work["action_id"] == second_id
    assert (await _action_row(session, first_id)).status == "rejected"
    assert (await _action_row(session, hidden_id)).status == "rejected"


async def test_watchdog_rejects_an_orphaned_correction_on_a_drifted_run(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """agent-svc is dead and the run drifted to pr_opened beside an
    in_progress correction: the watchdog must still recover it."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, _ = await _request_correction(client, manager, run_id)
    row = await _run_row(session, run_id)
    row.status = "pr_opened"
    await session.commit()
    action = await _action_row(session, action_id)
    action.updated_at = datetime.now(UTC) - timedelta(minutes=31)
    await session.commit()

    resp = await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    assert resp.status_code == 200
    rejected = await _action_row(session, action_id)
    assert rejected.status == "rejected"
    assert (await _run_row(session, run_id)).status == "pr_opened"


# ------------------------------------------- review round: takeover / heal / settle


async def _orphan_setup(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
    *,
    status: str = "pr_opened",
) -> tuple[str, str]:
    """A run with an in_progress correction whose status drifted to `status`."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, created = await _request_correction(client, manager, run_id)
    assert created["status_code"] == 200
    row = await _run_row(session, run_id)
    row.status = status
    await session.commit()
    return run_id, action_id


@pytest.mark.parametrize("terminal", ["merged", "cancelled", "failed", "deployed"])
async def test_refused_correction_never_resurrects_a_finished_run(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
    terminal: str,
) -> None:
    run_id, action_id = await _orphan_setup(
        client, session, manager, project, monkeypatch, status=terminal
    )
    row = await _run_row(session, run_id)
    row.lease_id = str(uuid4())
    row.lease_kind = "correction"
    row.lease_until = datetime.now(UTC) + timedelta(minutes=3)
    await session.commit()
    lease_id = row.lease_id

    # GitHub reports a different head too: adopting it must not revive the run.
    async def moved(run) -> tuple[str, bool]:
        return _NEW_SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", moved)
    _new_id, refused = await _request_correction(client, manager, run_id)
    assert refused["status_code"] == 409
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == terminal  # never pushed back to pr_opened
    assert row.head_sha == _SHA
    plan = await agent_runs._plan_local_correction_takeover(session, row, "x")
    assert plan.stuck == [] and not plan.live and not plan.expired_lease
    assert row.lease_id == lease_id  # a (live) lease is never cleared here
    assert (await _action_row(session, action_id)).status == "in_progress"


async def test_refused_correction_request_has_no_side_effects_on_an_orphan(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """Stale expected head / closed PR: the request is refused, so the old
    in_progress correction must not be superseded nor the run touched."""
    run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)

    _id, stale = await _request_correction(client, manager, run_id, _NEW_SHA)
    assert stale["status_code"] == 409

    async def closed(run) -> tuple[str, bool]:
        return _SHA, False

    monkeypatch.setattr(agent_runs, "_current_pr_head", closed)
    _id, not_open = await _request_correction(client, manager, run_id)
    assert not_open["status_code"] == 409

    assert (await _action_row(session, action_id)).status == "in_progress"
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "pr_opened"


async def test_refused_request_keeps_a_correction_running_run_with_an_expired_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, _ = await _request_correction(client, manager, run_id)
    work = await _lease_correction(client)
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    _id, refused = await _request_correction(client, manager, run_id, _NEW_SHA)  # stale
    assert refused["status_code"] == 409
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "correction_running"
    assert row.lease_id == work["lease_id"]
    assert (await _action_row(session, action_id)).status == "in_progress"


async def test_live_correction_lease_on_a_drifted_run_is_left_alone(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    row.lease_id = str(uuid4())
    row.lease_kind = "correction"
    row.lease_until = datetime.now(UTC) + timedelta(minutes=3)
    await session.commit()
    lease_id = row.lease_id

    _id, refused = await _request_correction(client, manager, run_id)
    assert refused["status_code"] == 409  # never two at once
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.lease_id == lease_id
    assert row.lease_kind == "correction"
    assert (await _action_row(session, action_id)).status == "in_progress"


async def test_heal_rejects_an_orphan_when_a_merge_is_in_flight(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, action_id = await _orphan_setup(
        client, session, manager, project, monkeypatch, status="pr_ready"
    )
    row = await _run_row(session, run_id)
    session.add(
        AgentRunAction(
            action_id=str(uuid4()),
            agent_run_id=row.id,
            kind="merge",
            request_hash="1" * 64,
            request_data={"expected_head_sha": _SHA},
            status="in_progress",
        )
    )
    await session.commit()

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 204  # nothing leased: no correction races the merge
    assert (await _action_row(session, action_id)).status == "rejected"
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "pr_ready"
    assert row.lease_id is None


async def test_heal_rejects_an_orphan_whose_expected_head_is_no_longer_the_run_head(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    row.head_sha = _NEW_SHA
    await session.commit()

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 204
    assert (await _action_row(session, action_id)).status == "rejected"
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "pr_opened"


async def test_heal_rejects_a_stale_orphan(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    _run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)
    action = await _action_row(session, action_id)
    action.updated_at = datetime.now(UTC) - timedelta(minutes=31)
    await session.commit()

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 204
    assert (await _action_row(session, action_id)).status == "rejected"


async def test_cancel_retires_an_orphaned_correction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)

    async def close_pr(run) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_close_pr", close_pr)
    resp = await client.post(f"/api/v1/agent-runs/{run_id}/cancel", headers=auth(manager))
    assert resp.status_code == 200
    assert (await _action_row(session, action_id)).status == "rejected"
    assert (await _action_row(session, action_id)).result == {
        "message": "Run cancelled; correction not applied"
    }


async def test_merge_result_retires_an_open_correction_with_a_merge_message(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, action_id = await _orphan_setup(
        client, session, manager, project, monkeypatch, status="pr_ready"
    )
    row = await _run_row(session, run_id)
    merge_id = str(uuid4())
    session.add(
        AgentRunAction(
            action_id=merge_id,
            agent_run_id=row.id,
            kind="merge",
            request_hash="2" * 64,
            request_data={"expected_head_sha": _SHA},
            status="in_progress",
        )
    )
    await session.commit()

    async def verified(run, sha) -> str:
        return _SHA

    monkeypatch.setattr(agent_runs, "_verify_deployment", verified)
    monkeypatch.setattr(agent_runs, "_owner_release_supported", lambda run: True)
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json={
            "action_id": merge_id,
            "status": "completed",
            "head_sha": _SHA,
            "merge_sha": "c" * 40,
        },
        headers=_CALLBACK,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "merged"
    assert (await _action_row(session, action_id)).result == {
        "message": "PR merged; correction not applied"
    }


@pytest.mark.parametrize("merge_status", ["accepted", "in_progress"])
async def test_correction_request_is_refused_while_a_merge_is_open(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
    merge_status: str,
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    row = await _run_row(session, run_id)
    merge_id = str(uuid4())
    session.add(
        AgentRunAction(
            action_id=merge_id,
            agent_run_id=row.id,
            kind="merge",
            request_hash="3" * 64,
            request_data={"expected_head_sha": _SHA},
            status=merge_status,
        )
    )
    await session.commit()

    action_id, refused = await _request_correction(client, manager, run_id)
    assert refused["status_code"] == 409
    assert refused["error"]["message"] == "merge in progress"
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "pr_opened"  # never flipped to correction_running
    assert row.lease_id is None
    created = (
        await session.scalars(
            select(AgentRunAction).where(AgentRunAction.action_id == action_id)
        )
    ).first()
    assert created is None

    # Once the merge action is finished, a correction is accepted again.
    merge = await _action_row(session, merge_id)
    merge.status = "rejected"
    await session.commit()
    await session.refresh(manager)
    _id, allowed = await _request_correction(client, manager, run_id)
    assert allowed["status_code"] == 200


async def _open_pr_with_merge(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: Project,
    monkeypatch,
    *,
    merge_status: str,
    expected_head: str = _SHA,
    age: timedelta = timedelta(0),
) -> tuple[str, str]:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    row = await _run_row(session, run_id)
    merge_id = str(uuid4())
    merge = AgentRunAction(
        action_id=merge_id,
        agent_run_id=row.id,
        kind="merge",
        request_hash="4" * 64,
        request_data={"expected_head_sha": expected_head},
        status=merge_status,
    )
    session.add(merge)
    await session.commit()
    if age:
        merge.updated_at = datetime.now(UTC) - age
        await session.commit()
    return run_id, merge_id


async def test_retryable_merge_never_blocks_a_correction_and_is_superseded(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, merge_id = await _open_pr_with_merge(
        client, session, manager, project, monkeypatch, merge_status="retryable"
    )
    _action_id, created = await _request_correction(client, manager, run_id)
    assert created["status_code"] == 200
    merge = await _action_row(session, merge_id)
    assert merge.status == "rejected"
    assert merge.result == {"message": "superseded by correction request"}


async def test_stale_accepted_merge_does_not_block_a_correction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _merge_id = await _open_pr_with_merge(
        client,
        session,
        manager,
        project,
        monkeypatch,
        merge_status="accepted",
        age=timedelta(minutes=31),
    )
    _action_id, created = await _request_correction(client, manager, run_id)
    assert created["status_code"] == 200


async def test_accepted_merge_for_an_older_head_does_not_block_a_correction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _merge_id = await _open_pr_with_merge(
        client,
        session,
        manager,
        project,
        monkeypatch,
        merge_status="accepted",
        expected_head=_NEW_SHA,
    )
    _action_id, created = await _request_correction(client, manager, run_id)
    assert created["status_code"] == 200


async def test_head_change_rejects_stale_retryable_merges(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, merge_id = await _open_pr_with_merge(
        client, session, manager, project, monkeypatch, merge_status="retryable"
    )

    # GitHub now reports a different head than the retryable merge asked for.
    async def moved(run) -> tuple[str, bool]:
        return _NEW_SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", moved)
    resp = await client.get(f"/api/v1/agent-runs/{run_id}", headers=auth(manager))
    assert resp.status_code == 200
    merge = await _action_row(session, merge_id)
    assert merge.status == "rejected"
    assert merge.result == {"message": "PR head changed; merge request no longer applies"}


async def test_pr_head_read_is_hard_capped_even_when_github_trickles(monkeypatch) -> None:
    """One `_current_pr_head` call must end within its timeout, however slowly
    the (fake) GitHub answers: per-phase httpx timeouts alone would not."""

    class SlowClient:
        def __init__(self, *args, **kwargs) -> None:
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self) -> "SlowClient":
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def get(self, *args, **kwargs):
            await asyncio.sleep(30)

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", SlowClient)
    monkeypatch.setattr(agent_runs, "_headers", lambda repo: {})
    run = _run_stub(repo_full_name="Asadtop4ik/task-manager")
    token = agent_runs._pr_head_timeout_s.set(0.2)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(agent_runs.HTTPException) as excinfo:
            await agent_runs._current_pr_head(run)
        assert excinfo.value.status_code == 502  # never a bare httpx error (-> 500)
        assert asyncio.get_running_loop().time() - started < 2.0
    finally:
        agent_runs._pr_head_timeout_s.reset(token)


async def test_settle_keeps_the_last_good_read_when_a_later_call_times_out(
    monkeypatch,
) -> None:
    calls = {"n": 0}

    async def flaky(run) -> tuple[str, bool]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _SHA, True
        raise agent_runs.HTTPException(
            status_code=502, detail="GitHub PR status is unavailable"
        )

    monkeypatch.setattr(agent_runs, "_current_pr_head", flaky)
    monkeypatch.setattr(agent_runs, "_PR_HEAD_SETTLE_DELAYS_S", (0.0, 0.0))
    assert await agent_runs._settled_pr_head(None, _NEW_SHA) == (_SHA, True)  # type: ignore[arg-type]
    assert calls["n"] == 3


async def test_settled_pr_head_one_slow_call_cannot_exceed_its_cap(monkeypatch) -> None:
    timeouts: list[object] = []

    class SlowClient:
        def __init__(self, *args, **kwargs) -> None:
            timeouts.append(kwargs.get("timeout"))

        async def __aenter__(self) -> "SlowClient":
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def get(self, *args, **kwargs):
            await asyncio.sleep(30)

    monkeypatch.setattr(agent_runs.httpx, "AsyncClient", SlowClient)
    monkeypatch.setattr(agent_runs, "_headers", lambda repo: {})
    monkeypatch.setattr(agent_runs, "_PR_HEAD_SETTLE_CALL_TIMEOUT_S", 0.2)
    run = _run_stub(repo_full_name="Asadtop4ik/task-manager")
    started = asyncio.get_running_loop().time()
    with pytest.raises(agent_runs.HTTPException) as excinfo:
        await agent_runs._settled_pr_head(run, _NEW_SHA)
    assert excinfo.value.status_code == 502
    assert asyncio.get_running_loop().time() - started < 2.0
    timeout = timeouts[0]
    assert isinstance(timeout, agent_runs.httpx.Timeout)
    assert max(timeout.connect, timeout.read, timeout.write, timeout.pool) <= 0.5


async def test_retire_open_corrections_rejects_orphans_and_drops_the_lease(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """What a merge result does to a run, so no orphan survives a merged run."""
    run_id, action_id = await _orphan_setup(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    row.lease_id = str(uuid4())
    row.lease_kind = "correction"
    row.lease_until = datetime.now(UTC) + timedelta(minutes=3)
    await session.commit()

    await agent_runs._retire_open_corrections(session, row, "retired")
    await session.commit()
    assert (await _action_row(session, action_id)).status == "rejected"
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.lease_id is None


async def test_correction_result_for_a_closed_pr_is_a_distinct_409_without_waiting(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)

    async def head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", head)
    action_id, _ = await _request_correction(client, manager, run_id)
    work = await _lease_correction(client)
    calls = {"n": 0}

    async def closed(run) -> tuple[str, bool]:
        calls["n"] += 1
        return _SHA, False

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(agent_runs, "_current_pr_head", closed)
    monkeypatch.setattr(agent_runs.asyncio, "sleep", fake_sleep)
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json={"action_id": action_id, "status": "completed", "head_sha": _NEW_SHA},
        headers={**_CALLBACK, "X-Agent-Lease-ID": work["lease_id"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "correction PR is no longer open"
    assert calls["n"] == 1 and slept == []
    # The 409 is terminal for agent-svc (LeaseLost), so our side is settled
    # too: action rejected, lease gone, run back to an open-PR state.
    action = await _action_row(session, action_id)
    assert action.status == "rejected"
    assert action.result == {"message": "PR is no longer open"}
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.status == "pr_opened"
    assert row.lease_id is None and row.lease_kind is None
    # A replay of the same result is now an idempotent no-op, not a new 409.
    replay = await client.post(
        f"/api/v1/agent-runs/{run_id}/action-result",
        json={"action_id": action_id, "status": "completed", "head_sha": _NEW_SHA},
        headers={**_CALLBACK, "X-Agent-Lease-ID": work["lease_id"]},
    )
    assert replay.status_code == 200


async def test_settle_respects_its_wall_clock_deadline(monkeypatch) -> None:
    """Sleep and clock are patched with a fake timeline (not zeroed): the wait
    never exceeds the budget, however long the configured delays are."""
    timeline = {"now": 0.0}
    slept: list[float] = []
    reads: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        timeline["now"] += delay

    async def never_settles(run) -> tuple[str, bool]:
        reads.append(timeline["now"])
        timeline["now"] += 0.4  # GitHub call time counts against the budget too
        return _SHA, True

    monkeypatch.setattr(agent_runs.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent_runs, "_settle_clock", lambda: timeline["now"])
    monkeypatch.setattr(agent_runs, "_current_pr_head", never_settles)
    monkeypatch.setattr(agent_runs, "_PR_HEAD_SETTLE_DELAYS_S", (10.0, 10.0, 10.0))

    head, is_open = await agent_runs._settled_pr_head(None, _NEW_SHA)  # type: ignore[arg-type]
    assert (head, is_open) == (_SHA, True)
    assert timeline["now"] <= agent_runs._PR_HEAD_SETTLE_BUDGET_S + 0.5
    assert sum(slept) <= agent_runs._PR_HEAD_SETTLE_BUDGET_S
    assert slept[0] < 10.0  # capped to what is left of the budget


async def test_settle_default_schedule_fits_well_inside_agent_svcs_timeout(
    monkeypatch,
) -> None:
    assert (
        sum(agent_runs._PR_HEAD_SETTLE_DELAYS_S) <= agent_runs._PR_HEAD_SETTLE_BUDGET_S <= 6.0
    )


async def test_github_executor_run_detail_still_adopts_a_new_head_during_correction(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    """The suppression is for the local executor only; legacy runs keep the
    previous recovery."""
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    row = await _run_row(session, run_id)
    row.executor = "github"
    row.status = "correction_running"
    await session.commit()

    async def pushed(run) -> tuple[str, bool]:
        return _NEW_SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", pushed)
    resp = await client.get(f"/api/v1/agent-runs/{run_id}", headers=auth(manager))
    assert resp.status_code == 200
    row = await _run_row(session, run_id)
    await session.refresh(row)
    assert row.head_sha == _NEW_SHA
    assert row.status == "pr_opened"


# --------------------------------------------------------------- IntakeBrief validation


def _brief(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"title": "T", "goal": "G", "acceptance": ["ok"]}
    base.update(overrides)
    return base


def test_intake_brief_accepts_valid_relevant_files_and_complexity() -> None:
    brief = IntakeBrief(
        **_brief(complexity="simple", relevant_files=["app/main.py", "a/b-c.txt"])
    )
    assert brief.complexity == "simple"
    assert brief.relevant_files == ["app/main.py", "a/b-c.txt"]


def test_intake_brief_defaults_when_absent() -> None:
    brief = IntakeBrief(**_brief())
    assert brief.complexity is None
    assert brief.relevant_files == []


@pytest.mark.parametrize(
    "bad_path", ["../etc/passwd", "/etc/passwd", "app/../secret", "bad path.py", ""]
)
def test_intake_brief_rejects_unsafe_relevant_file_paths(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        IntakeBrief(**_brief(relevant_files=[bad_path]))


def test_intake_brief_rejects_more_than_twelve_relevant_files() -> None:
    with pytest.raises(ValidationError):
        IntakeBrief(**_brief(relevant_files=[f"f{i}.py" for i in range(13)]))


def test_intake_brief_rejects_unknown_complexity() -> None:
    with pytest.raises(ValidationError):
        IntakeBrief(**_brief(complexity="medium"))


# --------------------------------------------------------------- settings validation


# These construct Settings straight from the process environment (not from
# constructor kwargs) with monkeypatch.setenv, because pydantic-settings
# reads and JSON-decodes real env vars through a different path than plain
# kwargs — a plain comma list like "task-manager" is not valid JSON, and the
# whole API failed to boot the moment this env var was set to anything at
# all before NoDecode was added (see app/core/config.py). A kwargs-only test
# would not have caught that.


def test_local_executor_env_unset_keeps_the_feature_off(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_LOCAL_EXECUTOR_PROJECTS", raising=False)
    result = Settings(_env_file=None)
    assert result.agent_local_executor_projects == frozenset()


def test_local_executor_env_empty_string_keeps_the_feature_off(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_EXECUTOR_PROJECTS", "")
    result = Settings(_env_file=None)
    assert result.agent_local_executor_projects == frozenset()


def test_local_executor_env_single_project_key(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_EXECUTOR_PROJECTS", "task-manager")
    monkeypatch.setenv("AGENT_SVC_TOKEN", "x" * 32)
    result = Settings(_env_file=None)
    assert result.agent_local_executor_projects == frozenset({"task-manager"})


def test_local_executor_env_comma_list_with_spaces(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_EXECUTOR_PROJECTS", "task-manager, agent-qa")
    monkeypatch.setenv("AGENT_SVC_TOKEN", "x" * 32)
    result = Settings(_env_file=None)
    assert result.agent_local_executor_projects == frozenset({"task-manager", "agent-qa"})


def test_local_executor_env_unknown_project_key_fails(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_EXECUTOR_PROJECTS", "not-a-real-project")
    monkeypatch.setenv("AGENT_SVC_TOKEN", "x" * 32)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_local_executor_env_short_token_fails(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_LOCAL_EXECUTOR_PROJECTS", "task-manager")
    monkeypatch.setenv("AGENT_SVC_TOKEN", "short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_local_executor_settings_reject_a_short_token() -> None:
    with pytest.raises(ValidationError):
        Settings(AGENT_LOCAL_EXECUTOR_PROJECTS="task-manager", AGENT_SVC_TOKEN="short")


def test_local_executor_settings_reject_an_unknown_project_key() -> None:
    with pytest.raises(ValidationError):
        Settings(AGENT_LOCAL_EXECUTOR_PROJECTS="not-a-real-project", AGENT_SVC_TOKEN="x" * 32)


def test_local_executor_settings_accept_known_project_keys() -> None:
    result = Settings(
        AGENT_LOCAL_EXECUTOR_PROJECTS="task-manager,agent-qa", AGENT_SVC_TOKEN="x" * 32
    )
    assert result.agent_local_executor_projects == frozenset({"task-manager", "agent-qa"})
