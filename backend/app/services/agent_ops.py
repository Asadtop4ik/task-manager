"""Store, settle and reclaim Codex-proposed env-change requests.

Shared by `app.api.v1.agent_runs` (storing proposals inside the local-executor
callback, and the watchdog's TTL sweep) and `app.api.v1.agent_ops` (leasing,
deciding and settling one request). Never imports `scripts/` — the backend
independently recomputes `request_hash` and re-checks the secret-name
denylist rather than trusting agent-svc's own copy of either.

Locking discipline, load-bearing for correctness: every path that mutates an
`AgentOpsRequest` row locks its parent `AgentRun` row FIRST — the same order
`cancel_agent_run` uses. Two ops rows on the same run can otherwise each be
decided/settled by a different concurrent request without either transaction
seeing the other's write (classic write skew under READ COMMITTED with only
the child row locked): reject B and report A applied can each compute "the
run still has open rows" from their own snapshot and neither ever settles
it. Locking the run serializes every mutation against that run, so the
second transaction always sees the first's already-committed result. A bulk
sweep (`reclaim_stale`, `lease`'s own reclaim) locks both tables in one
`SELECT ... FOR UPDATE OF ... SKIP LOCKED` statement instead of two
sequential lock statements — `SKIP LOCKED` never blocks (a partially-locked
candidate row is skipped as a whole), so it can never be the blocked half of
a deadlock regardless of which table the query plan locks first.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import ActivityKind, TaskStatus, can_transition
from app.db.models import AgentOpsRequest, AgentRun
from app.schemas.agent_run import AgentOpsProposal, AgentOpsRequestOut
from app.services import activity, agent_events

# Key names that must never reach this table regardless of what any allowlist
# says — defense in depth alongside the root-owned allowlist agent-svc reads
# and the applying root helper's own independent copy of this same rule.
# The alternation is wrapped in `.*` so `fullmatch` behaves like a substring
# search — every check in this feature goes through `re.fullmatch(..., re.ASCII)`
# on principle (see `app.schemas.agent_run` for why never a bare `search`/`match`).
SECRET_KEY_RE = re.compile(
    r".*(SECRET|TOKEN|PASSW|PWD|KEY|DSN|DATABASE|REDIS|URL|URI|HOST|ENDPOINT|"
    r"WEBHOOK|PRIVATE|CREDENTIAL|AUTH|COOKIE|SALT|JWT|API).*",
    re.ASCII,
)

# A run stops waiting on an owner decision or a lease pickup after this long;
# see `reclaim_stale`.
_TTL_HOURS = 72
_SETTLE_ERROR = "Ops so‘rovlari qo‘llanmadi"
_LEASE_EXPIRED_MESSAGE = "apply lease expired"


def request_hash(
    *, run_id: str, project_key: str, kind: str, key: str, op: str, value: str
) -> str:
    """sha256 hex of the canonical request — the one thing agent-svc, the
    applying root helper and this backend all recompute independently and
    must agree on bit-for-bit before anything is ever applied."""
    payload = {
        "v": 1,
        "run_id": run_id,
        "project_key": project_key,
        "kind": kind,
        "key": key,
        "op": op,
        "value": value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _is_denylisted(proposal: AgentOpsProposal) -> bool:
    return bool(SECRET_KEY_RE.fullmatch(proposal.key))


def has_eligible_proposal(proposals: Sequence[AgentOpsProposal]) -> bool:
    """True when at least one of up to 3 proposals would be stored as
    `proposed` rather than `invalid` by `store_proposals` — the precondition
    an `ops_pending` callback needs, checked *before* anything is inserted so
    a callback that fails it never mutates the database at all."""
    return any(
        not (_is_denylisted(proposal) or proposal.policy == "denied")
        for proposal in proposals[:3]
    )


async def lock_run_for_ops_id(session: AsyncSession, ops_id: int) -> AgentRun | None:
    """Resolve one `AgentOpsRequest.id` to its parent run and take the run's
    `FOR UPDATE` lock *before* the ops row itself — see the module docstring.
    `agent_run_id` is immutable once a row is inserted, so this initial
    unlocked lookup is safe; the caller still separately (re)loads and locks
    the ops row afterward."""
    agent_run_id = await session.scalar(
        select(AgentOpsRequest.agent_run_id).where(AgentOpsRequest.id == ops_id)
    )
    if agent_run_id is None:
        return None
    return await session.scalar(
        select(AgentRun)
        .where(AgentRun.id == agent_run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )


async def store_proposals(
    session: AsyncSession,
    run: AgentRun,
    proposals: Sequence[AgentOpsProposal],
    *,
    project_key: str,
    run_failed: bool = False,
) -> list[AgentOpsRequest]:
    """Insert up to 3 rows for one local-executor callback.

    Called only once per run (the caller never re-enters the live-lease
    transition for a run that already left it), so there is no existing-row
    check here — the unique `(agent_run_id, position)` constraint is the
    backstop against a bug ever double-inserting.

    `run_failed=True` (the callback's status is `failed`, not `pr_opened`/
    `ops_pending`) stores every otherwise-`proposed` row as `cancelled`
    instead, with `policy_reason="run_failed"`: an implement failure has
    nothing left to apply, so an owner card with a live approve button for
    it would be a dead end.
    """
    rows: list[AgentOpsRequest] = []
    for position, proposal in enumerate(proposals[:3], start=1):
        denylisted = _is_denylisted(proposal)
        invalid = denylisted or proposal.policy == "denied"
        if denylisted:
            policy_reason: str | None = "denylisted key"
        elif invalid:
            policy_reason = proposal.policy_reason or "denied"
        elif run_failed:
            policy_reason = "run_failed"
        else:
            policy_reason = proposal.policy_reason or None
        if invalid:
            status = "invalid"
        elif run_failed:
            status = "cancelled"
        else:
            status = "proposed"
        row = AgentOpsRequest(
            request_uuid=str(uuid4()),
            agent_run_id=run.id,
            position=position,
            project_key=project_key,
            kind=proposal.kind,
            key=proposal.key,
            op=proposal.op,
            value=proposal.value,
            reason=proposal.reason,
            restart_services=proposal.restart_services or None,
            request_hash=request_hash(
                run_id=run.run_id,
                project_key=project_key,
                kind=proposal.kind,
                key=proposal.key,
                op=proposal.op,
                value=proposal.value,
            ),
            status=status,
            policy_reason=policy_reason,
        )
        session.add(row)
        rows.append(row)
    if rows:
        await session.flush()
        agent_events.record(session, run, status="ops_proposed", phase="ops")
    return rows


async def list_for_run(session: AsyncSession, agent_run_id: int) -> list[AgentOpsRequest]:
    return list(
        (
            await session.scalars(
                select(AgentOpsRequest)
                .where(AgentOpsRequest.agent_run_id == agent_run_id)
                .order_by(AgentOpsRequest.position)
            )
        ).all()
    )


async def list_for_runs(
    session: AsyncSession, agent_run_ids: Sequence[int]
) -> dict[int, list[AgentOpsRequest]]:
    """Batched form of `list_for_run`, grouped by `agent_run_id` — used where
    a caller already holds a page of runs (e.g. `GET /agent-runs/notifications`)
    and would otherwise issue one query per run."""
    if not agent_run_ids:
        return {}
    rows = (
        await session.scalars(
            select(AgentOpsRequest)
            .where(AgentOpsRequest.agent_run_id.in_(agent_run_ids))
            .order_by(AgentOpsRequest.agent_run_id, AgentOpsRequest.position)
        )
    ).all()
    grouped: dict[int, list[AgentOpsRequest]] = {}
    for row in rows:
        grouped.setdefault(row.agent_run_id, []).append(row)
    return grouped


def ops_out(
    rows: Sequence[AgentOpsRequest], run_id: str, *, include_values: bool
) -> list[AgentOpsRequestOut]:
    """`rows` -> the owner-facing shape. `include_values=False` is for any
    future consumer that must see request metadata without the value itself;
    every backend endpoint today is owner-only and passes `True` (the
    task-visible `AgentRunOut` carries none of this table's columns at all).
    Never carries the ops-apply lease (`lease_id`/`lease_until`) — that is
    svc-only, see `AgentOpsWorkOut`."""
    return [
        AgentOpsRequestOut(
            id=row.id,
            request_uuid=UUID(row.request_uuid),
            run_id=run_id,
            position=row.position,
            project_key=row.project_key,
            kind=row.kind,
            key=row.key,
            op=row.op,
            value=row.value if include_values else "",
            reason=row.reason,
            restart_services=row.restart_services,
            request_hash=row.request_hash,
            status=row.status,
            policy_reason=row.policy_reason,
            decided_by_user_id=row.decided_by_user_id,
            decided_at=row.decided_at,
            attempts=row.attempts,
            applied_at=row.applied_at,
            result=row.result,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


async def settle_run(session: AsyncSession, run: AgentRun) -> None:
    """An ops-only run (`status == "ops_pending"`) whose requests have all
    left `proposed`/`approved`/`applying` finishes here: DONE if at least one
    applied, BLOCKED otherwise. A PR-based run's ops requests never change
    `run.status` — it is already `deployed` (and its task already DONE, see
    `agent_run_deployed`) by the time any of them can even be leased.

    Precondition: `run` is already locked `FOR UPDATE` by the caller (see the
    module docstring) and `run.task` is already loaded.
    """
    if run.status != "ops_pending":
        return
    open_count = await session.scalar(
        select(AgentOpsRequest.id)
        .where(
            AgentOpsRequest.agent_run_id == run.id,
            AgentOpsRequest.status.in_(("proposed", "approved", "applying")),
        )
        .limit(1)
    )
    if open_count is not None:
        return
    any_applied = await session.scalar(
        select(AgentOpsRequest.id)
        .where(AgentOpsRequest.agent_run_id == run.id, AgentOpsRequest.status == "applied")
        .limit(1)
    )
    now = datetime.now(UTC)
    run.finished_at = run.finished_at or now
    run.notified_at = None
    if any_applied is not None:
        run.status = "ops_applied"
        run.error = None
        agent_events.record(session, run, status="ops_applied", phase="ops")
        if can_transition(TaskStatus(run.task.status), TaskStatus.DONE):
            old = run.task.status
            run.task.status = TaskStatus.DONE
            run.task.done_at = now
            activity.record(
                session,
                task_id=run.task_id,
                actor=None,
                kind=ActivityKind.STATUS_CHANGED,
                payload={"from": old, "to": TaskStatus.DONE.value, "agent_run_id": run.run_id},
            )
    else:
        run.status = "failed"
        run.error = _SETTLE_ERROR
        agent_events.record(session, run, status="ops_failed", phase="ops", error=run.error)
        if can_transition(TaskStatus(run.task.status), TaskStatus.BLOCKED):
            old = run.task.status
            run.task.status = TaskStatus.BLOCKED
            activity.record(
                session,
                task_id=run.task_id,
                actor=None,
                kind=ActivityKind.STATUS_CHANGED,
                payload={
                    "from": old,
                    "to": TaskStatus.BLOCKED.value,
                    "agent_run_id": run.run_id,
                },
            )


async def reclaim_expired_applying(
    session: AsyncSession, now: datetime, *, limit: int = 20
) -> dict[int, AgentRun]:
    """`applying` rows whose lease the applying root helper never reported
    back on: below the 3-attempt cap, released back to `approved` for another
    lease; at the cap, failed outright. Locks each row's run in the same
    statement (`of=[AgentOpsRequest, AgentRun]`, `SKIP LOCKED`) so this can
    never deadlock against, or race, `POST /agent-ops/{ops_id}/result` or
    `/decision` (which lock the run first) or `cancel_agent_run` (which never
    touches an `applying` row at all — see `cancel_agent_run`).

    Called both from the watchdog sweep (`reclaim_stale`) and eagerly from
    `POST /agent-ops/lease`, inside its own advisory lock, before picking a
    fresh candidate — an agent-svc that crashed mid-apply should not have to
    wait for the next watchdog tick before its own next lease call can make
    progress again.

    Returns the touched runs (for the caller to `settle_run` afterward);
    does not commit.
    """
    stale_applying = (
        await session.scalars(
            select(AgentOpsRequest)
            .join(AgentRun, AgentRun.id == AgentOpsRequest.agent_run_id)
            .where(AgentOpsRequest.status == "applying", AgentOpsRequest.lease_until < now)
            .options(selectinload(AgentOpsRequest.agent_run).selectinload(AgentRun.task))
            .order_by(AgentRun.id, AgentOpsRequest.id)
            .limit(limit)
            .with_for_update(of=[AgentOpsRequest, AgentRun], skip_locked=True)
        )
    ).all()
    touched_runs: dict[int, AgentRun] = {}
    for row in stale_applying:
        row.lease_id = None
        row.lease_until = None
        if row.attempts >= 3:
            row.status = "failed"
            row.result = {
                "code": "timeout",
                "exit": None,
                "rolled_back": None,
                "restarted": None,
                "image_tag": None,
                "message": _LEASE_EXPIRED_MESSAGE,
            }
            row.agent_run.notified_at = None
            agent_events.record(
                session, row.agent_run, status="ops_failed", phase="ops", error="lease_expired"
            )
        else:
            row.status = "approved"
        touched_runs[row.agent_run_id] = row.agent_run
    return touched_runs


async def reclaim_stale(session: AsyncSession, now: datetime) -> None:
    """Watchdog sweep, called from the same poll as
    `agent_runs._fail_stale_local_runs`: an `applying` lease the applying
    root helper never reported back on, and a `proposed`/`approved` request
    nobody ever acted on, cannot wait forever. Also self-heals any
    `ops_pending` run left with no open row at all — belt and suspenders
    alongside the run-locking discipline that should prevent that from ever
    happening in the first place (see the module docstring).
    """
    touched_runs = await reclaim_expired_applying(session, now)

    cutoff = now - timedelta(hours=_TTL_HOURS)
    ttl_rows = (
        await session.scalars(
            select(AgentOpsRequest)
            .join(AgentRun, AgentRun.id == AgentOpsRequest.agent_run_id)
            .where(
                or_(
                    and_(
                        AgentOpsRequest.status == "proposed",
                        AgentOpsRequest.created_at < cutoff,
                    ),
                    and_(
                        AgentOpsRequest.status == "approved",
                        AgentOpsRequest.decided_at < cutoff,
                    ),
                )
            )
            .options(selectinload(AgentOpsRequest.agent_run).selectinload(AgentRun.task))
            .order_by(AgentRun.id, AgentOpsRequest.id)
            .limit(20)
            .with_for_update(of=[AgentOpsRequest, AgentRun], skip_locked=True)
        )
    ).all()
    for row in ttl_rows:
        row.status = "cancelled"
        row.agent_run.notified_at = None
        touched_runs[row.agent_run_id] = row.agent_run

    if touched_runs:
        await session.flush()

    # Self-heal: an `ops_pending` run with no proposed/approved/applying row
    # left should always have already settled as a side effect of whichever
    # transaction closed out its last open row. Sweep for any that somehow
    # didn't (a bug, or a run-locking gap this review missed) so a run can
    # never wait forever.
    stuck_runs = (
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.status == "ops_pending")
            .options(selectinload(AgentRun.task))
            .order_by(AgentRun.id)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for run in stuck_runs:
        touched_runs[run.id] = run

    for run in touched_runs.values():
        await settle_run(session, run)
