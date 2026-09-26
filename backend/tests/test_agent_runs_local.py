"""The local agent-svc executor: leasing, heartbeats, stage reports, and the
GitHub-path divergences (`/callback`, `/ci-result`, corrections, `/cancel`)
that only apply to a run with `executor == "local"`."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent_runs
from app.core.config import Settings, settings
from app.db.models import AgentEvent, AgentRun, Project, User
from app.schemas.agent_intake import IntakeBrief
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


# --------------------------------------------------------------- corrections / cancel


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
    assert req.json()["status"] == "accepted"

    leased = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert leased.status_code == 200
    body = leased.json()
    assert body["kind"] == "correction"
    assert body["action_id"] == action_id
    assert body["instruction"] == "Please rename this helper."
    assert body["expected_head_sha"] == _SHA
    row = await _run_row(session, run_id)
    assert row.status == "correction_running"


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


async def test_watchdog_fails_a_run_whose_lease_is_far_expired(
    client: AsyncClient, session: AsyncSession, manager: User, project: Project, monkeypatch
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    row = await _run_row(session, run_id)
    row.lease_until = datetime.now(UTC) - timedelta(minutes=16)
    await session.commit()

    await client.get("/api/v1/agent-runs/notifications", headers=_worker())
    failed = await _run_row(session, run_id)
    assert failed.status == "failed"
    assert failed.lease_id is None


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
