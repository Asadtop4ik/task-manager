"""Lease, apply-result and owner-decision endpoints for one Codex-proposed,
owner-approved env change.

Separate router from `agent_runs.py` (the spec keeps this file boundary so
the two can be reviewed independently); the svc-token auth helper is shared
by importing it from there rather than duplicating a security check.
"""

import zlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, OwnerUser
from app.api.v1.agent_runs import _agent_svc_auth, _load_run_detail, _run_detail_with_ops
from app.db.models import AgentOpsRequest, AgentRun
from app.schemas.agent_ops import (
    AgentOpsDecisionIn,
    AgentOpsDecisionOut,
    AgentOpsResultIn,
    AgentOpsWorkOut,
)
from app.schemas.agent_run import AgentRunDetailOut
from app.services import agent_events, agent_ops

router = APIRouter(prefix="/agent-ops", tags=["agent-ops"])

# Fixed bigint key for `pg_advisory_xact_lock`, held for the whole
# `/agent-ops/lease` transaction so "is anything already applying" + "pick
# and lease the next approved row" is atomic across concurrent agent-svc
# lease calls — the one thing that keeps at most one request `applying`
# globally. Deterministic (crc32 of a fixed label) purely so it is easy to
# recompute and verify rather than trust a magic literal; it carries no other
# meaning and must never change once deployed (a change is a silent no-op
# split of the lock into two).
_APPLY_LOCK_KEY = zlib.crc32(b"agent_ops_apply_lock")


class AgentOpsResultAck(BaseModel):
    status: str


def _require_live_ops_lease(row: AgentOpsRequest, lease_id: str | None) -> None:
    now = datetime.now(UTC)
    if (
        row.lease_id is None
        or lease_id != row.lease_id
        or row.lease_until is None
        or row.lease_until < now
    ):
        raise HTTPException(status_code=409, detail="lease_mismatch")


@router.post("/lease", response_model=AgentOpsWorkOut)
async def lease_ops_work(
    session: DbSession,
    x_agent_svc_token: str | None = Header(default=None),
) -> AgentOpsWorkOut | Response:
    """Hand agent-svc's ops lane at most one approved request to apply.

    Gate: the owning run is `executor == "local"` and either has an open PR
    that is already verified `deployed` (consuming code is live — apply
    after, never before, a successful deploy) or has no PR at all and is
    `ops_pending` (a no-patch run applies immediately once approved). At
    most one request is ever `applying` globally, enforced by holding a
    transaction-scoped advisory lock across the whole check-and-lease.
    """
    _agent_svc_auth(x_agent_svc_token)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _APPLY_LOCK_KEY})
    now = datetime.now(UTC)

    already_applying = await session.scalar(
        select(AgentOpsRequest.id).where(AgentOpsRequest.status == "applying").limit(1)
    )
    if already_applying is not None:
        await session.commit()
        return Response(status_code=204)

    row = await session.scalar(
        select(AgentOpsRequest)
        .join(AgentRun, AgentRun.id == AgentOpsRequest.agent_run_id)
        .where(
            AgentOpsRequest.status == "approved",
            AgentRun.executor == "local",
            or_(
                (AgentRun.pr_url.is_not(None)) & (AgentRun.status == "deployed"),
                (AgentRun.pr_url.is_(None)) & (AgentRun.status == "ops_pending"),
            ),
        )
        .options(selectinload(AgentOpsRequest.agent_run))
        .order_by(AgentOpsRequest.id)
        .limit(1)
        .with_for_update(of=[AgentOpsRequest], skip_locked=True)
    )
    if row is None:
        await session.commit()
        return Response(status_code=204)

    run = row.agent_run
    row.status = "applying"
    row.lease_id = str(uuid4())
    row.lease_until = now + timedelta(minutes=15)
    row.attempts += 1
    await session.commit()
    return AgentOpsWorkOut(
        ops_id=row.id,
        request_uuid=UUID(row.request_uuid),
        run_id=run.run_id,
        lease_id=row.lease_id,
        lease_until=row.lease_until,
        project_key=row.project_key,
        repo_full_name=run.repo_full_name,
        kind=row.kind,
        key=row.key,
        op=row.op,
        value=row.value,
        request_hash=row.request_hash,
        deployed_sha=run.deployed_sha,
        attempts=row.attempts,
    )


@router.post("/{ops_id}/result", response_model=AgentOpsResultAck)
async def report_ops_result(
    ops_id: int,
    payload: AgentOpsResultIn,
    session: DbSession,
    x_agent_svc_token: str | None = Header(default=None),
    x_agent_lease_id: str | None = Header(default=None),
) -> AgentOpsResultAck:
    """The applying root helper's outcome, relayed by agent-svc. Never the
    callback token — only the ops lease this same lane just took out."""
    _agent_svc_auth(x_agent_svc_token)
    row = await session.scalar(
        select(AgentOpsRequest)
        .where(AgentOpsRequest.id == ops_id)
        .options(selectinload(AgentOpsRequest.agent_run).selectinload(AgentRun.task))
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="ops request not found")
    if row.status in {"applied", "failed"}:
        # Idempotent replay: agent-svc retried after the first response was
        # lost, but the applying root helper's result already landed.
        return AgentOpsResultAck(status=row.status)
    if row.status != "applying":
        raise HTTPException(status_code=409, detail="ops request is not applying")
    _require_live_ops_lease(row, x_agent_lease_id)

    result_payload: dict[str, object] = {
        "code": payload.code,
        "message": payload.message,
        "rolled_back": payload.rolled_back,
        "image_tag": payload.image_tag,
    }
    run = row.agent_run
    row.lease_id = None
    row.lease_until = None
    if payload.status == "applied":
        row.status = "applied"
        row.result = result_payload
        row.applied_at = datetime.now(UTC)
        agent_events.record(session, run, status="ops_applied", phase="ops")
    elif payload.status == "retry" and row.attempts < 3:
        row.status = "approved"
    else:
        # Either an explicit `failed`, or a `retry` that already used up its
        # 3 attempts (see `POST /agent-ops/lease`, which increments on every
        # lease) — both end the request the same way.
        row.status = "failed"
        row.result = result_payload
        agent_events.record(
            session, run, status="ops_failed", phase="ops", error=payload.message
        )

    await agent_ops.settle_run(session, run)
    await session.commit()
    return AgentOpsResultAck(status=row.status)


@router.get("/{ops_id}", response_model=AgentRunDetailOut)
async def get_ops_request_run(
    ops_id: int, session: DbSession, owner: OwnerUser
) -> AgentRunDetailOut:
    """The owning run's detail (including `ops_requests`, with values), keyed
    by one ops request rather than the run's own `run_id`. The Telegram
    callback data behind an ops decision button carries only `ops_id` and a
    hash prefix — never `run_id`, to stay inside the 64-byte payload limit —
    so the bot refreshes the card and builds `decide_ops_request`'s
    `action_id` through this endpoint."""
    run = await session.scalar(
        select(AgentRun)
        .join(AgentOpsRequest, AgentOpsRequest.agent_run_id == AgentRun.id)
        .where(AgentOpsRequest.id == ops_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="ops request not found")
    return await _load_run_detail(session, run)


@router.post("/{ops_id}/decision", response_model=AgentOpsDecisionOut)
async def decide_ops_request(
    ops_id: int,
    payload: AgentOpsDecisionIn,
    session: DbSession,
    owner: OwnerUser,
) -> AgentOpsDecisionOut:
    row = await session.scalar(
        select(AgentOpsRequest)
        .where(AgentOpsRequest.id == ops_id)
        .options(selectinload(AgentOpsRequest.agent_run).selectinload(AgentRun.task))
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="ops request not found")
    run = row.agent_run
    action_id = str(payload.action_id)

    if row.decision_action_id == action_id:
        # Idempotent replay of the exact same owner tap. `status` can only
        # have progressed from `approved` (never from `rejected`), so it is
        # exact evidence of which decision this action_id was for.
        was_approve = row.status in {"approved", "applying", "applied", "failed"}
        if (payload.decision == "approve") == was_approve:
            # `updated_at` is server-computed (`onupdate=func.now()`); a
            # prior UPDATE in this same session can leave it expired, and a
            # bare attribute read outside a greenlet context would crash.
            await session.refresh(row)
            return AgentOpsDecisionOut(
                ops_request=agent_ops.ops_out([row], run.run_id, include_values=True)[0],
                run=await _run_detail_with_ops(session, run),
            )
        raise HTTPException(status_code=409, detail="conflict")
    # A different action_id than the one already recorded (or none recorded
    # yet) deciding a row that has moved on falls through to the ordinary
    # `not_pending` check below — "conflict" is reserved for the one case
    # above, the *same* action_id replayed with a *different* decision.
    if row.status != "proposed":
        raise HTTPException(status_code=409, detail="not_pending")
    if row.request_hash != payload.request_hash:
        raise HTTPException(status_code=409, detail="stale_request")
    if run.status in {"cancelled", "failed"}:
        raise HTTPException(status_code=409, detail="run_finished")

    # Defense in depth: re-check the backend's own denylist at approval time,
    # in case a bug or a future looser check ever let a bad key through
    # `store_proposals`.
    if agent_ops.SECRET_KEY_RE.fullmatch(row.key):
        row.status = "invalid"
        row.policy_reason = "denylisted key"
        await session.commit()
        raise HTTPException(status_code=409, detail="denylisted")

    now = datetime.now(UTC)
    row.decision_action_id = action_id
    row.decided_by_user_id = owner.id
    row.decided_at = now
    if payload.decision == "approve":
        row.status = "approved"
        agent_events.record(session, run, status="ops_approved", phase="ops")
    else:
        row.status = "rejected"
        agent_events.record(session, run, status="ops_rejected", phase="ops")
        await agent_ops.settle_run(session, run)
    await session.commit()
    await session.refresh(row)
    return AgentOpsDecisionOut(
        ops_request=agent_ops.ops_out([row], run.run_id, include_values=True)[0],
        run=await _run_detail_with_ops(session, run),
    )
