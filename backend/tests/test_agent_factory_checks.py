"""Acceptance-check factory plumbing: the lease's `acceptance`/`owner_note`/
`check_gate`, `AgentRunCallback.check_gate`, the new stage names and the
owner-only `owner_note` on a retried attempt."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import AgentIntake, AgentRun, Task, User
from app.db.models import Project as ProjectModel
from tests.conftest import auth
from tests.test_agent_runs_local import (
    _CALLBACK,
    _PR_URL,
    _SHA,
    _SVC,
    _local_run,
    _mark_ci_green,
    _open_local_pr,
    _run_row,
)

_GATE = {
    "status": "passed",
    "total": 3,
    "passed": 3,
    "fixups": 1,
    "failed": [],
    "acceptance_modified": False,
    "author_input_tokens": 1200,
    "author_output_tokens": 300,
    "fixup_input_tokens": 400,
    "fixup_output_tokens": 50,
    "author_model": "gpt-6.1-sol",
    "fixup_model": "gpt-6.1-sol",
}


async def _add_intake(
    session: AsyncSession, run_id: str, user: User, brief: dict[str, object]
) -> None:
    row = await _run_row(session, run_id)
    task = await session.get(Task, row.task_id)
    assert task is not None
    session.add(
        AgentIntake(
            user_id=user.id,
            project_id=task.project_id,
            chat_id=user.telegram_id,
            text="intake",
            mode="pr",
            status="confirmed",
            brief=brief,
            task_id=task.id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.commit()


async def _lease(client: AsyncClient) -> dict:
    resp = await client.post("/api/v1/agent-runs/lease", json={"lane": "code"}, headers=_SVC)
    assert resp.status_code == 200
    return resp.json()


async def _fail_run(client: AsyncClient, run_id: str, lease_id: str, error: str) -> None:
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "failed", "error": error},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


# ------------------------------------------------------------ lease: acceptance


async def test_implement_lease_carries_the_brief_acceptance(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await _add_intake(
        session, run_id, manager, {"title": "T", "goal": "G", "acceptance": ["a one", "b two"]}
    )
    body = await _lease(client)
    assert body["kind"] == "implement"
    assert body["acceptance"] == ["a one", "b two"]
    assert body["owner_note"] is None
    assert body["check_gate"] is None


async def test_implement_lease_acceptance_is_empty_without_an_intake(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    await _local_run(client, session, manager, project, monkeypatch)
    assert (await _lease(client))["acceptance"] == []


@pytest.mark.parametrize(
    "acceptance", ["not a list", [1, 2], ["", "   "], None, ["x" * 251]], ids=str
)
async def test_implement_lease_acceptance_ignores_an_invalid_brief(
    acceptance,
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await _add_intake(session, run_id, manager, {"acceptance": acceptance})
    assert (await _lease(client))["acceptance"] == []


async def test_review_lease_has_no_acceptance_but_carries_the_check_gate(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    await _add_intake(session, run_id, manager, {"acceptance": ["a one"]})
    lease_id = (await _lease(client))["lease_id"]

    from app.api.v1 import agent_runs

    async def fake_verify_pr(run, number, sha) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    opened = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": _PR_URL,
            "head_sha": _SHA,
            "check_gate": _GATE,
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert opened.status_code == 200
    await _mark_ci_green(client, monkeypatch, run_id)

    body = await _lease(client)
    assert body["kind"] == "review"
    assert body["acceptance"] == []
    assert body["owner_note"] is None
    assert body["check_gate"] == _GATE


async def test_review_lease_check_gate_is_null_when_none_was_reported(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _mark_ci_green(client, monkeypatch, run_id)
    body = await _lease(client)
    assert body["kind"] == "review"
    assert body["check_gate"] is None


# ------------------------------------------------------------ callback.check_gate


async def test_callback_persists_check_gate_on_pr_opened(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    from app.api.v1 import agent_runs

    async def fake_verify_pr(run, number, sha) -> None:
        return None

    monkeypatch.setattr(agent_runs, "_verify_pr", fake_verify_pr)
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "pr_opened",
            "pr_url": _PR_URL,
            "head_sha": _SHA,
            "check_gate": _GATE,
            "some_future_field": 1,  # the outer callback stays tolerant
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    assert resp.json()["check_gate"] == _GATE
    row = await _run_row(session, run_id)
    assert row.check_gate == _GATE
    status_resp = await client.get(f"/api/v1/agent-runs/{run_id}/status", headers=_CALLBACK)
    assert status_resp.json()["check_gate"] == _GATE


async def test_callback_persists_check_gate_on_failed_with_defaults(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={
            "run_id": run_id,
            "status": "failed",
            "error": "checks failed",
            "check_gate": {
                "status": "failed",
                "total": 2,
                "passed": 1,
                "fixups": 2,
                "failed": ["check two"],
            },
        },
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 200
    row = await _run_row(session, run_id)
    assert row.status == "failed"
    assert row.check_gate is not None
    assert row.check_gate["status"] == "failed"
    assert row.check_gate["failed"] == ["check two"]
    assert row.check_gate["acceptance_modified"] is False
    assert row.check_gate["author_model"] is None


async def test_callback_without_check_gate_leaves_it_null(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    assert (await _run_row(session, run_id)).check_gate is None


def _gate(**overrides: object) -> dict[str, object]:
    return {**_GATE, **overrides}


@pytest.mark.parametrize(
    "bad",
    [
        _gate(status="bogus"),
        _gate(unexpected=True),
        _gate(total=-1),
        _gate(total=101),
        _gate(fixups=21),
        _gate(failed=["x"] * 51),
        _gate(failed=["x" * 201]),
        _gate(failed="check two"),
        _gate(passed=4),
        _gate(total=1, passed=1, failed=["a", "b"]),
        _gate(acceptance_modified="maybe"),
        _gate(author_model="m" * 33),
        _gate(fixup_output_tokens=-5),
        _gate(author_input_tokens="many"),
    ],
    ids=lambda bad: next(
        (f"{k}={str(v)[:12]}" for k, v in bad.items() if _GATE.get(k) != v), "base"
    ),
)
async def test_callback_rejects_an_invalid_check_gate(
    bad: dict[str, object],
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/callback",
        json={"run_id": run_id, "status": "failed", "error": "x", "check_gate": bad},
        headers={**_CALLBACK, "X-Agent-Lease-ID": lease_id},
    )
    assert resp.status_code == 422
    row = await _run_row(session, run_id)
    assert row.status == "running"
    assert row.check_gate is None


# ------------------------------------------------------------ stages


@pytest.mark.parametrize(
    "stage", ["acceptance_authored", "check_gate_passed", "check_gate_failed"]
)
async def test_new_stage_names_are_accepted(
    stage: str,
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    resp = await client.post(
        f"/api/v1/agent-runs/{run_id}/stage",
        json={"lease_id": lease_id, "stage": stage},
        headers=_SVC,
    )
    assert resp.status_code == 204


# ------------------------------------------------------------ owner_note


async def _spec_conflict_attempt(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> tuple[int, str]:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    await _fail_run(client, run_id, lease_id, "SPEC_CONFLICT: brief and acceptance disagree")
    row = await _run_row(session, run_id)
    return row.task_id, run_id


async def test_owner_note_round_trips_on_a_new_attempt(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, first_run = await _spec_conflict_attempt(
        client, session, manager, project, monkeypatch
    )
    assert (await _run_row(session, first_run)).owner_note is None

    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}",
        json={"owner_note": "  Follow the spec as written.  "},
        headers=auth(manager),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["attempt_index"] == 2
    assert "owner_note" not in body  # never on the public AgentRunOut
    row = await session.scalar(select(AgentRun).where(AgentRun.run_id == body["run_id"]))
    assert row is not None and row.owner_note == "Follow the spec as written."

    lease = await _lease(client)
    assert lease["kind"] == "implement"
    assert lease["run_id"] == body["run_id"]
    assert lease["attempt_index"] == 2
    assert lease["owner_note"] == "Follow the spec as written."


async def test_attempt_cap_still_applies_with_an_owner_note(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, _ = await _spec_conflict_attempt(client, session, manager, project, monkeypatch)
    second = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert second.status_code == 201
    lease_id = (await _lease(client))["lease_id"]
    await _fail_run(client, second.json()["run_id"], lease_id, "SPEC_CONFLICT: again")
    third = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert third.status_code == 409


async def test_owner_note_is_owner_only(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, _ = await _spec_conflict_attempt(client, session, manager, project, monkeypatch)
    # manager keeps Codex access but is no longer the owner.
    monkeypatch.setattr(settings, "owner_telegram_id", manager.telegram_id + 1)
    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert resp.status_code == 403
    # No attempt was created by the refused request.
    runs = (await session.scalars(select(AgentRun).where(AgentRun.task_id == task_id))).all()
    assert len(runs) == 1
    # A plain retry without a note is still allowed for that user.
    plain = await client.post(f"/api/v1/agent-runs/tasks/{task_id}", headers=auth(manager))
    assert plain.status_code == 201
    assert (await _run_row(session, plain.json()["run_id"])).owner_note is None


async def test_owner_note_rejected_while_the_new_attempt_is_active(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, _ = await _spec_conflict_attempt(client, session, manager, project, monkeypatch)
    first = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}",
        json={"owner_note": "one"},
        headers=auth(manager),
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}",
        json={"owner_note": "two"},
        headers=auth(manager),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "spec_conflict_not_pending"
    row = await _run_row(session, first.json()["run_id"])
    await session.refresh(row)
    assert row.owner_note == "one"


async def test_owner_note_rejected_after_the_task_was_edited(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, _ = await _spec_conflict_attempt(client, session, manager, project, monkeypatch)
    edited = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"description": "the spec changed"},
        headers=auth(manager),
    )
    assert edited.status_code == 200
    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "spec_conflict_not_pending"
    runs = (await session.scalars(select(AgentRun).where(AgentRun.task_id == task_id))).all()
    assert len(runs) == 1


@pytest.mark.parametrize(
    "error", ["Codex timed out", "SPEC_CONFLICTING input", "SPEC_CONFLICT", "spec_conflict: x"]
)
async def test_owner_note_requires_a_spec_conflict_failure(
    error: str,
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    await _fail_run(client, run_id, lease_id, error)
    task_id = (await _run_row(session, run_id)).task_id
    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "spec_conflict_not_pending"


@pytest.mark.parametrize("error", ["SPEC_CONFLICT: x", "SPEC_CONFLICT (brief vs acceptance)"])
async def test_owner_note_accepts_the_tight_spec_conflict_prefixes(
    error: str,
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    run_id = await _local_run(client, session, manager, project, monkeypatch)
    lease_id = (await _lease(client))["lease_id"]
    await _fail_run(client, run_id, lease_id, error)
    task_id = (await _run_row(session, run_id)).task_id
    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": "go"}, headers=auth(manager)
    )
    assert resp.status_code == 201


async def test_correction_lease_carries_acceptance_and_owner_note(
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    from uuid import uuid4

    from app.api.v1 import agent_runs

    run_id, _ = await _open_local_pr(client, session, manager, project, monkeypatch)
    await _add_intake(session, run_id, manager, {"acceptance": ["a one"]})
    row = await _run_row(session, run_id)
    row.owner_note = "Follow the spec."
    await session.commit()

    async def fake_current_head(run) -> tuple[str, bool]:
        return _SHA, True

    monkeypatch.setattr(agent_runs, "_current_pr_head", fake_current_head)
    req = await client.post(
        f"/api/v1/agent-runs/{run_id}/corrections",
        json={"expected_head_sha": _SHA, "action_id": str(uuid4()), "instruction": "fix it"},
        headers=auth(manager),
    )
    assert req.status_code == 200
    body = await _lease(client)
    assert body["kind"] == "correction"
    assert body["acceptance"] == ["a one"]
    assert body["owner_note"] == "Follow the spec."
    assert body["check_gate"] is None


@pytest.mark.parametrize("note", ["", "   ", "x" * 501])
async def test_owner_note_is_validated(
    note: str,
    client: AsyncClient,
    session: AsyncSession,
    manager: User,
    project: ProjectModel,
    monkeypatch,
) -> None:
    task_id, _ = await _spec_conflict_attempt(client, session, manager, project, monkeypatch)
    resp = await client.post(
        f"/api/v1/agent-runs/tasks/{task_id}", json={"owner_note": note}, headers=auth(manager)
    )
    assert resp.status_code == 422
