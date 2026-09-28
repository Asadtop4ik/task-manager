"""Start one coding run for a task and record verified GitHub results."""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Response, status
from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, OwnerUser
from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import ActivityKind, TaskStatus, can_transition
from app.db.models import (
    AgentEvent,
    AgentIntake,
    AgentOpsRequest,
    AgentRun,
    AgentRunAction,
    Attachment,
    Project,
    ProjectDiscussion,
    Task,
)
from app.schemas.agent_intake import is_safe_relevant_file
from app.schemas.agent_run import (
    AgentActionAvailability,
    AgentActionDetailOut,
    AgentActionOut,
    AgentActionResult,
    AgentCiEvidenceOut,
    AgentCiPending,
    AgentCiResult,
    AgentCorrectionRequest,
    AgentDeployment,
    AgentEventOut,
    AgentImageOut,
    AgentLeaseHeartbeat,
    AgentLeaseHeartbeatOut,
    AgentLeaseRequest,
    AgentMerge,
    AgentMetricsOut,
    AgentNoticeAck,
    AgentNotificationOut,
    AgentQaDeployDispatchResult,
    AgentQaDeployment,
    AgentQaDeploymentAuthorization,
    AgentQaDeploymentAuthorizationOut,
    AgentQaDeploymentFailure,
    AgentReleaseRequest,
    AgentReviewFinding,
    AgentReviewOut,
    AgentReviewResult,
    AgentRunCallback,
    AgentRunDetailOut,
    AgentRunOut,
    AgentRunStart,
    AgentStageReport,
    AgentWorkOut,
    ExternalAgentPending,
    MetricDuration,
)
from app.services import activity, agent_events, agent_ops
from app.services.access import can_edit_task, can_see_task
from app.services.agent_repos import (
    DISPATCH_REPOSITORY,
    PUBLIC_REPOSITORIES,
    QA_REPOSITORY,
    REPOSITORIES,
    repository_for,
)
from app.services.telegram_media import IMAGE_MIMES, telegram_image

_LEASE_MINUTES = 5
_WATCHDOG_DISPATCHED_MINUTES = 30
_WATCHDOG_LEASE_MINUTES = 15
# A local run is treated as "queued behind healthy work" rather than
# abandoned when any local run has heartbeat_at (set on both lease issuance
# and every heartbeat) within this window.
_WATCHDOG_LIVENESS_MINUTES = 5
# A hard ceiling on one lease's total lifetime, independent of how many
# times it has been heartbeat-renewed: an agent-svc that keeps heartbeating
# without ever finishing must not hold a lease forever.
_LEASE_MAX_DURATION_MINUTES = 60
_AGENT_SVC_TIMEOUT_ERROR = "agent-svc javob bermadi"
_TASK_REVISION_STALE_ERROR = "Task tasdiqlangandan keyin o'zgartirildi; qayta yuboring."

log = get_logger(__name__)
router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])
_GITHUB = "https://api.github.com"
_METRICS_PILOT_START = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
_QA_DEPLOY_FAILURE_MESSAGES = {
    "image_pull_failed": (
        "QA deployment failed [image_pull_failed]: could not pull the approved image; "
        "check package read access."
    ),
    "deploy_failed": (
        "QA deployment failed [deploy_failed]: inspect the trusted QA deploy workflow."
    ),
    "readiness_failed": (
        "QA deployment failed [readiness_failed]: the QA service did not pass readiness."
    ),
}


def _review_out(run: AgentRun) -> AgentReviewOut:
    findings = [
        AgentReviewFinding.model_validate(item) for item in (run.review_findings or [])
    ]
    state = cast(
        Literal["pending", "clean", "advisory", "findings", "stale", "error"],
        run.review_status or "pending",
    )
    return AgentReviewOut(
        state=state,
        reviewed_head_sha=run.review_sha,
        summary=run.review_summary,
        findings=findings,
    )


def _owner_release_supported(run: AgentRun) -> bool:
    return run.mode == "pr" and any(
        item.full_name == run.repo_full_name and item.branch == run.base_branch
        for item in REPOSITORIES + ((QA_REPOSITORY,) if settings.agent_qa_enabled else ())
    )


def _run_detail(run: AgentRun) -> AgentRunDetailOut:
    open_pr = bool(
        _owner_release_supported(run)
        and run.pr_url
        and run.head_sha
        and run.status in {"pr_opened", "pr_ready", "correction_running"}
    )
    merge_ready = bool(
        open_pr
        and run.status == "pr_ready"
        and run.ci_status == "success"
        and run.ci_verified_sha == run.head_sha
        and run.review_status in {"clean", "advisory"}
        and run.review_sha == run.head_sha
    )
    return AgentRunDetailOut(
        run_id=run.run_id,
        repo_full_name=run.repo_full_name,
        status=run.status,
        summary=run.task.title,
        impact=run.review_summary or run.task.description or "Review pending",
        head_sha=run.head_sha,
        merged_sha=run.merged_sha,
        deployed_sha=run.deployed_sha,
        github_run_url=run.github_run_url,
        error=run.error,
        qa_ready_url=run.qa_ready_url,
        qa_ready_sha=run.qa_ready_sha,
        ci_evidence=AgentCiEvidenceOut(
            state=run.ci_status,
            verified_head_sha=run.ci_verified_sha,
            url=run.ci_url,
        ),
        review=_review_out(run),
        actions={
            "merge": AgentActionAvailability(available=merge_ready),
            "correction": AgentActionAvailability(
                available=open_pr and run.status in {"pr_opened", "pr_ready"}
            ),
        },
    )


async def _run_detail_with_ops(session: DbSession, run: AgentRun) -> AgentRunDetailOut:
    """`_run_detail` plus its owner-only `ops_requests` (with values) — a
    separate async step because loading them is a query of its own, not
    something already sitting on `run`. Shared by `GET /agent-runs/{run_id}`
    and `POST /agent-ops/{ops_id}/decision`'s `run` field."""
    detail = _run_detail(run)
    if run.executor == "local":
        rows = await agent_ops.list_for_run(session, run.id)
        detail.ops_requests = agent_ops.ops_out(rows, run.run_id, include_values=True)
    return detail


def _can_merge(run: AgentRun) -> bool:
    return _run_detail(run).actions["merge"].available


def _refresh_pr_ready(run: AgentRun) -> bool:
    ready = bool(
        run.pr_url
        and run.status in {"pr_opened", "pr_ready"}
        and run.head_sha
        and run.ci_status == "success"
        and run.ci_verified_sha == run.head_sha
        and run.review_status in {"clean", "advisory"}
        and run.review_sha == run.head_sha
    )
    if ready:
        run.status = "pr_ready"
        run.pr_ready_at = run.pr_ready_at or datetime.now(UTC)
    else:
        run.status = "pr_opened"
        run.pr_ready_at = None
    return ready


def _action_error(
    code: str, message: str, *, status_code: int, current_head_sha: str | None = None
) -> Response:
    body: dict[str, object] = {"code": code, "message": message}
    if current_head_sha is not None:
        body["current_head_sha"] = current_head_sha
    return Response(
        content=json.dumps({"error": body}),
        status_code=status_code,
        media_type="application/json",
    )


def _action_response(action: AgentRunAction, run: AgentRun) -> AgentActionOut:
    status_map: dict[
        str, Literal["accepted", "in_progress", "completed", "rejected", "retryable"]
    ] = {
        "accepted": "accepted",
        "in_progress": "in_progress",
        "completed": "completed",
        "rejected": "rejected",
        "retryable": "retryable",
    }
    message = (action.result or {}).get("message")
    if not isinstance(message, str):
        message = None
    return AgentActionOut(
        action_id=UUID(action.action_id),
        status=status_map.get(action.status, "in_progress"),
        run_id=run.run_id,
        head_sha=run.head_sha,
        message=message,
    )


async def _dispatch_release_action(
    run: AgentRun, action: AgentRunAction, payload: dict[str, object]
) -> None:
    if not _owner_release_supported(run):
        raise HTTPException(
            status_code=409,
            detail="release actions are not enabled for this repository",
        )
    if not settings.github_agent_token:
        raise HTTPException(status_code=503, detail="release workflow token is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_GITHUB}/repos/{DISPATCH_REPOSITORY}/dispatches",
            headers=_headers(),
            json={
                "event_type": f"agent_run_{action.kind}",
                "client_payload": {
                    "run_id": run.run_id,
                    "action_id": action.action_id,
                    "branch": _run_branch(run),
                    "repo_full_name": run.repo_full_name,
                    **payload,
                },
            },
        )
    if response.status_code != 204:
        raise HTTPException(
            status_code=502 if response.status_code >= 500 else 409,
            detail="GitHub did not accept release action",
        )


async def _dispatch_external_review(run: AgentRun, pr_number: str) -> None:
    if run.repo_full_name == DISPATCH_REPOSITORY:
        return  # This repository's successful CI workflow_run starts its reviewer.
    if not settings.github_agent_token:
        raise HTTPException(status_code=503, detail="review workflow token is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_GITHUB}/repos/{DISPATCH_REPOSITORY}/dispatches",
            headers=_headers(),
            json={
                "event_type": "agent_pr_review",
                "client_payload": {
                    "repo_full_name": run.repo_full_name,
                    "run_id": run.run_id,
                    "pull_number": int(pr_number),
                    "head_sha": run.head_sha,
                    "branch": _run_branch(run),
                    "base_branch": run.base_branch,
                },
            },
        )
    if response.status_code != 204:
        raise HTTPException(status_code=502, detail="GitHub did not accept independent review")


def _reject_retryable_action(action: AgentRunAction, message: str) -> None:
    if action.status == "retryable":
        action.status = "rejected"
        action.result = {"message": message}


def _duration_summary(values: list[float]) -> MetricDuration:
    seconds = sorted(round(value) for value in values if value >= 0)
    if not seconds:
        return MetricDuration(samples=0, p50_seconds=None, p90_seconds=None)
    return MetricDuration(
        samples=len(seconds),
        p50_seconds=seconds[ceil(len(seconds) * 0.5) - 1],
        p90_seconds=seconds[ceil(len(seconds) * 0.9) - 1],
    )


def _worker_auth(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, settings.service_token):
        raise HTTPException(status_code=401, detail="invalid worker token")


def _agent_svc_auth(token: str | None) -> None:
    if not settings.agent_svc_token:
        raise HTTPException(status_code=404, detail="not found")
    valid = False
    if token:
        try:
            # hmac.compare_digest rejects non-ASCII `str` operands with a
            # TypeError rather than just comparing unequal; a stray header
            # value outside ASCII must not turn into a 500.
            valid = hmac.compare_digest(token.encode(), settings.agent_svc_token.encode())
        except (TypeError, UnicodeEncodeError):
            valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="invalid agent-svc token")


def _require_live_lease(
    run: AgentRun, lease_id: str | None, *, kind: str | None = None
) -> None:
    """A lease id that still matches AND has not expired AND (if given) is
    the expected kind. Used by every local-executor callback so a stale or
    mismatched header can never be accepted as authorization to mutate."""
    now = datetime.now(UTC)
    if (
        run.lease_id is None
        or lease_id != run.lease_id
        or run.lease_until is None
        or run.lease_until < now
        or (kind is not None and run.lease_kind != kind)
    ):
        raise HTTPException(status_code=409, detail="lease_mismatch")


def _clear_lease(run: AgentRun) -> None:
    run.lease_id = None
    run.lease_until = None
    run.lease_kind = None
    run.heartbeat_at = None
    run.lease_issued_at = None


def _use_local_executor(project_key: str, mode: str) -> bool:
    return mode == "pr" and project_key in settings.agent_local_executor_projects


def _stale_lease_condition(lease_until_cutoff: datetime, now: datetime) -> ColumnElement[bool]:
    """A lease is stale when it missed its own renewal deadline, OR when it
    has simply run past the hard total-duration cap regardless of how
    recently it was heartbeat (see `_LEASE_MAX_DURATION_MINUTES`)."""
    max_duration_cutoff = now - timedelta(minutes=_LEASE_MAX_DURATION_MINUTES)
    return or_(
        AgentRun.lease_until < lease_until_cutoff,
        AgentRun.lease_issued_at < max_duration_cutoff,
    )


def _reject_stale_correction(
    session: DbSession, run: AgentRun, action: AgentRunAction | None
) -> None:
    """A local correction agent-svc never finished: reject it exactly like a
    GitHub-path rejection and release the run back to `pr_opened`, never
    touching the PR itself. Shared by the lease-expiry cap (`attempts >= 2`)
    and the watchdog's "never leased at all" timeout."""
    if action is not None:
        action.status = "rejected"
        action.result = {"message": _AGENT_SVC_TIMEOUT_ERROR}
    if run.status == "correction_running":
        run.status = "pr_opened"
        _refresh_pr_ready(run)
    run.notified_at = None
    agent_events.record(session, run, phase="correction", error=_AGENT_SVC_TIMEOUT_ERROR)


def _reject_correction_for_stale_task_revision(
    session: DbSession, run: AgentRun, action: AgentRunAction
) -> None:
    """The task text changed after a correction was requested. The run
    already has an open PR, so only the correction is refused (like a
    GitHub-path rejection) and the run returns to `pr_opened`; the owner can
    request the correction again against the edited task."""
    action.status = "rejected"
    action.result = {"message": _TASK_REVISION_STALE_ERROR}
    if run.status == "correction_running":
        run.status = "pr_opened"
        _refresh_pr_ready(run)
    _clear_lease(run)
    run.notified_at = None
    agent_events.record(session, run, phase="correction", error=_TASK_REVISION_STALE_ERROR)


def _fail_run_for_stale_task_revision(
    session: DbSession,
    run: AgentRun,
    now: datetime,
    *,
    action: AgentRunAction | None = None,
) -> None:
    """The task's approved title/description changed after this run was
    dispatched: the run's `task_revision` was frozen at dispatch time (see
    `start_agent_run`), but a task stays editable while a local run is
    queued, so anyone with edit rights could otherwise change what Codex
    implements after the owner approved it. Caught right before a lease
    hands out the current, possibly-edited, task text — treated exactly
    like a failed callback: BLOCKED + event + notified_at cleared."""
    if action is not None and action.status == "in_progress":
        action.status = "rejected"
        action.result = {"message": _TASK_REVISION_STALE_ERROR}
    run.status = "failed"
    run.error = _TASK_REVISION_STALE_ERROR
    run.finished_at = now
    run.notified_at = None
    _clear_lease(run)
    agent_events.record(session, run, phase="lease", error=run.error)
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


def _review_needed(run: AgentRun) -> bool:
    """True exactly when the GitHub flow would dispatch `agent_pr_review`.

    `POST /agent-runs/lease` encodes this same condition directly in SQL
    (a Python-side filter after a bounded query would stop leasing new
    reviews once enough already-reviewed runs exist ahead of them). This
    helper is the one place the condition is spelled out in plain terms, kept
    in lockstep with that SQL and used by tests to pin the definition down.
    """
    return bool(
        run.pr_url
        and run.status in {"pr_opened", "pr_ready"}
        and run.ci_status == "success"
        and run.ci_verified_sha is not None
        and run.ci_verified_sha == run.head_sha
        and run.review_sha != run.head_sha
    )


def _mark_running(
    session: DbSession,
    run: AgentRun,
    changed: bool,
    *,
    github_run_url: str | None = None,
) -> None:
    """Same effects as a callback reporting status=running: start the runner
    clock, record the transition, and move the task into progress. Reused by
    the local-executor lease endpoint's implement step."""
    now = datetime.now(UTC)
    if run.runner_started_at is None:
        run.runner_started_at = now
    if changed:
        agent_events.record(session, run, github_run_url=github_run_url)
    if can_transition(TaskStatus(run.task.status), TaskStatus.IN_PROGRESS):
        old = run.task.status
        run.task.status = TaskStatus.IN_PROGRESS
        run.task.started_at = run.task.started_at or now
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={
                "from": old,
                "to": TaskStatus.IN_PROGRESS.value,
                "agent_run_id": run.run_id,
            },
        )


def _pr_number(pr_url: str | None) -> int | None:
    if not pr_url:
        return None
    tail = pr_url.rsplit("/", 1)[-1]
    return int(tail) if tail.isdecimal() else None


async def _intake_complexity(
    session: DbSession, task_id: int
) -> tuple[Literal["simple", "complex"] | None, list[str]]:
    intake = await session.scalar(select(AgentIntake).where(AgentIntake.task_id == task_id))
    if intake is None:
        return None, []
    brief = intake.brief or {}
    complexity = brief.get("complexity")
    if complexity not in {"simple", "complex"}:
        complexity = None
    relevant_files_raw = brief.get("relevant_files")
    relevant_files = (
        [item for item in relevant_files_raw if is_safe_relevant_file(item)][:12]
        if isinstance(relevant_files_raw, list)
        else []
    )
    return cast(Literal["simple", "complex"] | None, complexity), relevant_files


async def _image_count(session: DbSession, task_id: int) -> int:
    rows = await session.scalars(
        select(Attachment.mime).where(Attachment.task_id == task_id).limit(3)
    )
    return sum(1 for mime in rows if mime in IMAGE_MIMES)


async def _issue_lease(
    session: DbSession,
    run: AgentRun,
    kind: Literal["implement", "review", "correction"],
    now: datetime,
    *,
    action: AgentRunAction | None = None,
) -> AgentWorkOut:
    run.lease_id = str(uuid4())
    run.lease_until = now + timedelta(minutes=_LEASE_MINUTES)
    run.lease_kind = kind
    run.heartbeat_at = now
    run.lease_issued_at = now
    # No event recorded here: `_mark_running`/the correction dispatch mirror
    # already record the GitHub-parity event for this pickup, and agent-svc
    # reports its own "leased" moment through POST /stage.
    complexity, relevant_files = await _intake_complexity(session, run.task_id)
    image_count = await _image_count(session, run.task_id)
    await session.commit()
    return AgentWorkOut(
        run_id=run.run_id,
        kind=kind,
        lease_id=run.lease_id,
        lease_until=run.lease_until,
        attempts=run.attempts,
        attempt_index=run.attempt_index,
        task_id=run.task_id,
        task_revision=run.task_revision,
        repo_full_name=run.repo_full_name,
        base_branch=run.base_branch,
        mode=run.mode,
        title=run.task.title,
        description=run.task.description or "",
        image_count=image_count,
        complexity=complexity,
        relevant_files=relevant_files,
        branch=_run_branch(run),
        pr_url=run.pr_url,
        pr_number=_pr_number(run.pr_url),
        head_sha=run.head_sha,
        action_id=action.action_id if action is not None else None,
        instruction=(
            cast(str | None, action.request_data.get("instruction"))
            if action is not None
            else None
        ),
        expected_head_sha=(
            cast(str | None, action.request_data.get("expected_head_sha"))
            if action is not None
            else None
        ),
    )


async def _reclaim_expired_lease(session: DbSession, run: AgentRun, now: datetime) -> None:
    """One local-executor lease whose `lease_until` has passed.

    Shared by `POST /agent-runs/lease` (reclaiming eagerly, so the run can be
    handed out again in the same call) and the notifications watchdog
    (reclaiming a lease agent-svc will never renew because it is dead).

    An implement lease has no PR yet, so giving up after too many expiries
    fails the whole run — there is nothing to protect. A review or
    correction lease has an open PR behind it, so giving up only finalizes
    that one piece of work (review -> `review_status="error"`, correction ->
    the action rejected and the run released back to `pr_opened`) and never
    touches the PR itself. Below the cap, only the lease is released.
    """
    if run.lease_kind == "implement":
        if run.attempts >= 2:
            run.status = "failed"
            run.error = _AGENT_SVC_TIMEOUT_ERROR
            run.finished_at = now
            run.notified_at = None
            _clear_lease(run)
            agent_events.record(session, run, phase="lease", error=run.error)
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
            return
        if run.status == "running":
            run.status = "dispatched"
            agent_events.record(session, run, phase="lease")
        _clear_lease(run)
        return
    if run.lease_kind == "review":
        # The run can have moved on since this lease was issued: an owner
        # correction request flips it to correction_running immediately
        # (independent of any live review lease, since a review never holds
        # run.status), or a completed correction can have landed a new,
        # never-reviewed head. Finalizing in either case would either strand
        # an in-progress correction (clobbering its status back to
        # pr_opened) or wrongly mark a fresh head as a failed review. Only
        # finalize when the run is still exactly the same open, unreviewed
        # PR this lease was tracking; otherwise just release the lease.
        still_tracking_this_review = (
            run.status in {"pr_opened", "pr_ready"}
            and run.review_attempts_sha == run.head_sha
            and run.review_sha != run.head_sha
        )
        if run.review_attempts >= 2 and still_tracking_this_review:
            run.review_status = "error"
            run.review_sha = run.head_sha
            run.review_summary = _AGENT_SVC_TIMEOUT_ERROR
            run.review_findings = []
            run.status = "pr_opened"
            run.pr_ready_at = None
            run.notified_at = None
            _clear_lease(run)
            agent_events.record(
                session,
                run,
                status="review_error",
                phase="review",
                error=_AGENT_SVC_TIMEOUT_ERROR,
            )
            return
        _clear_lease(run)
        return
    if run.lease_kind == "correction":
        action = await session.scalar(
            select(AgentRunAction)
            .where(
                AgentRunAction.agent_run_id == run.id,
                AgentRunAction.kind == "correction",
                AgentRunAction.status == "in_progress",
            )
            .with_for_update()
        )
        if action is not None and action.attempts >= 2:
            _reject_stale_correction(session, run, action)
            _clear_lease(run)
            return
        _clear_lease(run)
        return
    _clear_lease(run)


def _qa_deploy_auth(token: str | None) -> None:
    if (
        not settings.agent_qa_enabled
        or not settings.agent_qa_callback_token
        or not token
        or not hmac.compare_digest(token, settings.agent_qa_callback_token)
    ):
        raise HTTPException(status_code=401, detail="invalid QA deployment token")


async def _task(session: DbSession, task_id: int, *, lock: bool = False) -> Task:
    query = select(Task).where(Task.id == task_id).options(selectinload(Task.project))
    task = await session.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.deleted_at is not None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _revision(task: Task, mode: str = "pr") -> str:
    payload = json.dumps(
        {
            "title": task.title,
            "description": task.description,
            "project_id": task.project_id,
            "repo": task.project.repo_full_name,
            "branch": task.project.default_branch,
            "mode": mode,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _headers(repo: str | None = None) -> dict[str, str]:
    token = (
        settings.github_public_agent_token
        if repo in PUBLIC_REPOSITORIES
        else (
            settings.github_agent_qa_token
            if settings.agent_qa_enabled and repo == settings.agent_qa_repository
            else settings.github_agent_token
        )
    )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _ensure_repo_visibility(
    client: httpx.AsyncClient, repo: str, *, private: bool
) -> None:
    response = await client.get(f"{_GITHUB}/repos/{repo}", headers=_headers(repo))
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub repository access failed")
    if response.json().get("private") is not private:
        raise HTTPException(
            status_code=409,
            detail="repository visibility differs from the approved agent policy",
        )


async def _dispatch(run: AgentRun, task: Task) -> int:
    repository = repository_for(
        task.project.key,
        run.repo_full_name,
        run.base_branch,
        include_qa=settings.agent_qa_enabled,
    )
    if repository is None:
        raise HTTPException(
            status_code=409, detail="project repository is not enabled for agents"
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        await _ensure_repo_visibility(client, run.repo_full_name, private=repository.private)
        response = await client.post(
            f"{_GITHUB}/repos/{DISPATCH_REPOSITORY}/dispatches",
            headers=_headers(),
            json={
                "event_type": (
                    "agent_task"
                    if repository.private and not repository.qa_only
                    else "agent_public_task"
                ),
                "client_payload": {
                    "run_id": run.run_id,
                    "task_id": task.id,
                    "repo_full_name": run.repo_full_name,
                    "title": task.title,
                    "description": task.description or "",
                    "base_branch": run.base_branch,
                    "task_revision": run.task_revision,
                    "mode": run.mode,
                },
            },
        )
        return response.status_code


async def _verify_pr(run: AgentRun, pr_number: str, sha: str) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/pulls/{pr_number}",
            headers=_headers(run.repo_full_name),
        )
    expected_branch = _run_branch(run)
    if (
        response.status_code != 200
        or response.json()["head"]["ref"] != expected_branch
        or response.json()["head"]["sha"] != sha
        or (response.json()["head"].get("repo") or {}).get("full_name", "").lower()
        != run.repo_full_name.lower()
    ):
        raise HTTPException(status_code=409, detail="PR does not match this agent run")


async def _current_pr_head(run: AgentRun) -> tuple[str, bool]:
    if not run.pr_url:
        raise HTTPException(status_code=409, detail="agent PR has no URL")
    prefix = f"https://github.com/{run.repo_full_name}/pull/"
    pr_number = run.pr_url.removeprefix(prefix)
    if not run.pr_url.startswith(prefix) or not pr_number.isdecimal():
        raise HTTPException(status_code=409, detail="invalid agent PR reference")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/pulls/{pr_number}",
            headers=_headers(run.repo_full_name),
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub PR status is unavailable")
    pr = response.json()
    head = pr.get("head") or {}
    sha = head.get("sha", "")
    if (
        head.get("ref") != _run_branch(run)
        or (head.get("repo") or {}).get("full_name", "").lower() != run.repo_full_name.lower()
        or (pr.get("base") or {}).get("ref") != run.base_branch
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
    ):
        raise HTTPException(status_code=409, detail="PR does not match this agent run")
    return sha, pr.get("state") == "open" and not pr.get("merged")


def _invalidate_review_and_ci(run: AgentRun, head_sha: str) -> None:
    run.head_sha = head_sha
    run.status = "pr_opened"
    run.ci_status = "pending"
    run.ci_verified_sha = None
    run.ci_url = None
    run.pr_ready_at = None
    run.review_status = "pending"
    run.review_sha = None
    run.review_summary = None
    run.review_findings = None
    run.notified_at = None


async def _merge_rejection_is_retryable(run: AgentRun, action: AgentRunAction) -> bool:
    expected_head = action.request_data.get("expected_head_sha")
    if (
        action.kind != "merge"
        or run.status != "pr_ready"
        or expected_head != run.head_sha
        or not _can_merge(run)
    ):
        return False
    try:
        current_head, is_open = await _current_pr_head(run)
    except httpx.HTTPError:
        return True
    except HTTPException as error:
        return error.status_code >= 500
    if current_head != run.head_sha:
        _invalidate_review_and_ci(run, current_head)
        return False
    return is_open


async def _verify_recovered_commit(run: AgentRun, sha: str) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/commits/{sha}",
            headers=_headers(run.repo_full_name),
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502, detail="recovered agent commit verification failed"
        )
    message = (response.json().get("commit") or {}).get("message", "")
    if f"Agent-Run-ID: {run.run_id}" not in message.splitlines():
        raise HTTPException(status_code=409, detail="recovered commit is not this agent run")


async def _verify_pr_ci(run: AgentRun, sha: str, conclusion: str, url: str) -> None:
    """Prove a completed CI run belongs to this PR head and passed required jobs."""
    prefix = f"https://github.com/{run.repo_full_name}/actions/runs/"
    run_number = url.removeprefix(prefix)
    if not url.startswith(prefix) or not run_number.isdecimal():
        raise HTTPException(status_code=400, detail="invalid PR CI run URL")
    target = next(
        (
            item
            for item in REPOSITORIES + ((QA_REPOSITORY,) if settings.agent_qa_enabled else ())
            if item.full_name == run.repo_full_name and item.branch == run.base_branch
        ),
        None,
    )
    if target is None or not target.pr_ci_jobs:
        raise HTTPException(status_code=409, detail="PR CI policy is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/actions/runs/{run_number}",
            headers=_headers(run.repo_full_name),
        )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="PR CI verification failed")
        workflow = response.json()
        if (
            workflow.get("head_sha") != sha
            or workflow.get("head_branch") != _run_branch(run)
            or workflow.get("event") != "pull_request"
            or workflow.get("path") != target.pr_ci_workflow
            or workflow.get("status") != "completed"
        ):
            raise HTTPException(status_code=409, detail="PR CI does not match this commit")
        passed = workflow.get("conclusion") == "success"
        if passed:
            jobs_response = await client.get(
                f"{_GITHUB}/repos/{run.repo_full_name}/actions/runs/{run_number}/jobs?per_page=100",
                headers=_headers(run.repo_full_name),
            )
            if jobs_response.status_code != 200:
                raise HTTPException(status_code=502, detail="PR CI jobs verification failed")
            successful = {
                job.get("name")
                for job in jobs_response.json().get("jobs", [])
                if job.get("conclusion") == "success"
            }
            passed = set(target.pr_ci_jobs).issubset(successful)
        if passed != (conclusion == "success"):
            raise HTTPException(status_code=409, detail="PR CI conclusion does not match")


async def _verify_deployment(run: AgentRun, sha: str) -> str:
    if not run.pr_url or not run.head_sha:
        raise HTTPException(status_code=409, detail="agent PR is not verified")
    pr_number = run.pr_url.rsplit("/", 1)[-1]
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/pulls/{pr_number}",
            headers=_headers(run.repo_full_name),
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub PR verification failed")
    pr = response.json()
    head = pr.get("head") or {}
    head_sha = head.get("sha", "")
    if (
        not pr.get("merged")
        or pr.get("merge_commit_sha") != sha
        or (pr.get("base") or {}).get("ref") != run.base_branch
        or head.get("ref") != _run_branch(run)
        or (head.get("repo") or {}).get("full_name", "").lower() != run.repo_full_name.lower()
        or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
    ):
        raise HTTPException(status_code=409, detail="deployed SHA does not match merged PR")
    # A reviewer may push fixes after the agent opens the PR. The merge commit,
    # PR branch and source repo are verified above; record the final reviewed head.
    return head_sha


def _run_branch(run: AgentRun) -> str:
    prefix = "codex/fast" if run.mode == "fast" else "codex"
    return f"{prefix}/task-{run.task_id}-{run.run_id}"


async def _verify_fast_branch(run: AgentRun, sha: str) -> None:
    if run.mode != "fast":
        raise HTTPException(status_code=409, detail="run is not in fast mode")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/git/ref/heads/{_run_branch(run)}",
            headers=_headers(),
        )
    if response.status_code != 200 or (response.json().get("object") or {}).get("sha") != sha:
        raise HTTPException(status_code=409, detail="fast branch does not match this run")


async def _verify_fast_commit(run: AgentRun, sha: str) -> None:
    if run.mode != "fast" or run.head_sha != sha:
        raise HTTPException(status_code=409, detail="deployed SHA does not match fast run")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/commits/{sha}",
            headers=_headers(run.repo_full_name),
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="fast commit verification failed")
    message = (response.json().get("commit") or {}).get("message", "")
    if f"Agent-Run-ID: {run.run_id}" not in message.splitlines():
        raise HTTPException(status_code=409, detail="commit is not owned by this fast run")


async def _cancel_github(run: AgentRun) -> None:
    if not run.github_run_url:
        return
    expected = f"https://github.com/{DISPATCH_REPOSITORY}/actions/runs/"
    run_number = run.github_run_url.removeprefix(expected)
    if not run.github_run_url.startswith(expected) or not run_number.isdecimal():
        raise HTTPException(status_code=409, detail="invalid GitHub run reference")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_GITHUB}/repos/{DISPATCH_REPOSITORY}/actions/runs/{run_number}/cancel",
            headers=_headers(),
        )
    if response.status_code not in {202, 204}:
        raise HTTPException(status_code=409, detail="GitHub could not cancel this run")


async def _close_pr(run: AgentRun) -> None:
    if not run.pr_url:
        raise HTTPException(status_code=409, detail="agent PR has no URL")
    expected = f"https://github.com/{run.repo_full_name}/pull/"
    pr_number = run.pr_url.removeprefix(expected)
    if not run.pr_url.startswith(expected) or not pr_number.isdecimal():
        raise HTTPException(status_code=409, detail="invalid agent PR reference")
    token = (
        settings.github_public_agent_token
        if run.repo_full_name in PUBLIC_REPOSITORIES
        else settings.github_agent_token
    )
    if not token:
        raise HTTPException(status_code=503, detail="PR close token is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.patch(
            f"{_GITHUB}/repos/{run.repo_full_name}/pulls/{pr_number}",
            headers={**_headers(), "Authorization": f"Bearer {token}"},
            json={"state": "closed"},
        )
    if response.status_code != 200:
        raise HTTPException(status_code=409, detail="GitHub could not close this PR")


@router.post("/lease", response_model=AgentWorkOut)
async def lease_agent_work(
    payload: AgentLeaseRequest,
    session: DbSession,
    x_agent_svc_token: str | None = Header(default=None),
) -> AgentWorkOut | Response:
    """Hand agent-svc one unit of local-executor work, in a fixed priority order.

    Order: reclaim expired leases first (failing exhausted implement attempts),
    then an independent review, then an owner correction, then a fresh
    implement job. Each candidate set is row-locked with SKIP LOCKED so two
    concurrent lease calls never hand out the same run.
    """
    _agent_svc_auth(x_agent_svc_token)
    now = datetime.now(UTC)

    expired = (
        await session.scalars(
            select(AgentRun)
            .where(
                AgentRun.executor == "local",
                AgentRun.lease_id.is_not(None),
                _stale_lease_condition(now, now),
            )
            .options(selectinload(AgentRun.task))
            .order_by(AgentRun.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for stale in expired:
        await _reclaim_expired_lease(session, stale, now)
    if expired:
        await session.flush()

    # The full `_review_needed` condition, in SQL, with LIMIT 1: a Python-side
    # filter after a bounded LIMIT would silently stop leasing new reviews
    # once that many already-reviewed runs exist ahead of them in id order.
    review_run = await session.scalar(
        select(AgentRun)
        .where(
            AgentRun.executor == "local",
            AgentRun.lease_id.is_(None),
            AgentRun.status.in_(["pr_opened", "pr_ready"]),
            AgentRun.ci_status == "success",
            AgentRun.ci_verified_sha.is_not(None),
            AgentRun.ci_verified_sha == AgentRun.head_sha,
            AgentRun.review_sha.is_distinct_from(AgentRun.head_sha),
            AgentRun.pr_url.is_not(None),
        )
        .options(selectinload(AgentRun.task))
        .order_by(AgentRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if review_run is not None:
        if review_run.review_attempts_sha != review_run.head_sha:
            review_run.review_attempts = 0
            review_run.review_attempts_sha = review_run.head_sha
        review_run.review_attempts += 1
        return await _issue_lease(session, review_run, "review", now)

    # A local correction already transitioned to correction_running/in_progress
    # at request time (mirroring a successful GitHub dispatch exactly, see
    # `_request_owner_action`), so leasing it here is just picking the work
    # back up — no state to flip, only the attempt counter to advance.
    #
    # A task stays editable while its run is queued. `_issue_lease` reports
    # the task's *current* title/description, so before handing either an
    # implement or a correction lease out, re-check that text against the
    # revision frozen at dispatch time (the same `_revision` start_agent_run
    # compares on retry). A run whose task was edited since it was approved
    # is failed instead of leased, and the scan moves on to the next
    # candidate rather than returning stale work.
    correction_candidates = (
        await session.execute(
            select(AgentRun, AgentRunAction)
            .join(AgentRunAction, AgentRunAction.agent_run_id == AgentRun.id)
            .where(
                AgentRun.executor == "local",
                AgentRun.lease_id.is_(None),
                AgentRun.status == "correction_running",
                AgentRunAction.kind == "correction",
                AgentRunAction.status == "in_progress",
            )
            .options(selectinload(AgentRun.task).selectinload(Task.project))
            .order_by(AgentRunAction.agent_run_id)
            .limit(20)
            .with_for_update(of=[AgentRun, AgentRunAction], skip_locked=True)
        )
    ).all()
    correction_pick: tuple[AgentRun, AgentRunAction] | None = None
    for candidate_run, candidate_action in correction_candidates:
        if _revision(candidate_run.task, candidate_run.mode) != candidate_run.task_revision:
            _reject_correction_for_stale_task_revision(
                session, candidate_run, candidate_action
            )
            continue
        correction_pick = (candidate_run, candidate_action)
        break
    if correction_candidates:
        await session.flush()
    if correction_pick is not None:
        correction_run, correction_action = correction_pick
        correction_action.attempts += 1
        return await _issue_lease(
            session, correction_run, "correction", now, action=correction_action
        )

    implement_candidates = (
        await session.scalars(
            select(AgentRun)
            .where(
                AgentRun.executor == "local",
                AgentRun.status == "dispatched",
                AgentRun.lease_id.is_(None),
            )
            .options(selectinload(AgentRun.task).selectinload(Task.project))
            .order_by(AgentRun.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    ).all()
    implement_run = None
    for candidate in implement_candidates:
        if _revision(candidate.task, candidate.mode) != candidate.task_revision:
            _fail_run_for_stale_task_revision(session, candidate, now)
            continue
        implement_run = candidate
        break
    if implement_candidates:
        await session.flush()
    if implement_run is not None:
        implement_run.attempts += 1
        changed = implement_run.status != "running"
        implement_run.status = "running"
        _mark_running(session, implement_run, changed)
        return await _issue_lease(session, implement_run, "implement", now)

    await session.commit()
    return Response(status_code=204)


@router.post("/{run_id}/heartbeat", response_model=AgentLeaseHeartbeatOut)
async def agent_work_heartbeat(
    run_id: str,
    payload: AgentLeaseHeartbeat,
    session: DbSession,
    x_agent_svc_token: str | None = Header(default=None),
) -> AgentLeaseHeartbeatOut:
    _agent_svc_auth(x_agent_svc_token)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).with_for_update()
    )
    if run is None or run.executor != "local":
        raise HTTPException(status_code=404, detail="run not found")
    if run.status == "cancelled":
        raise HTTPException(status_code=409, detail="cancelled")
    now = datetime.now(UTC)
    if run.lease_id != payload.lease_id:
        raise HTTPException(status_code=409, detail="lease_mismatch")
    if run.lease_until is None or run.lease_until < now:
        raise HTTPException(status_code=409, detail="lease_expired")
    if run.lease_issued_at is not None and now - run.lease_issued_at > timedelta(
        minutes=_LEASE_MAX_DURATION_MINUTES
    ):
        # Renewable via heartbeat, but not forever: a worker that keeps
        # heartbeating without ever finishing must still give the lease up.
        raise HTTPException(status_code=409, detail="lease_expired")
    run.lease_until = now + timedelta(minutes=_LEASE_MINUTES)
    run.heartbeat_at = now
    await session.commit()
    return AgentLeaseHeartbeatOut(lease_until=run.lease_until)


@router.post("/{run_id}/stage", status_code=204)
async def agent_work_stage(
    run_id: str,
    payload: AgentStageReport,
    session: DbSession,
    x_agent_svc_token: str | None = Header(default=None),
) -> None:
    _agent_svc_auth(x_agent_svc_token)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).with_for_update()
    )
    if run is None or run.executor != "local":
        raise HTTPException(status_code=404, detail="run not found")
    _require_live_lease(run, payload.lease_id)
    agent_events.record(session, run, status=payload.stage, phase="stage", error=payload.error)
    await session.commit()


async def _fail_stale_local_runs(session: DbSession) -> None:
    """Watchdog for a dead or unreachable agent-svc.

    Runs inside the notifications poll (every 10s) rather than only inside
    `/lease`, because a fully stalled agent-svc never calls `/lease` again to
    trigger that endpoint's own expired-lease cleanup.

    Three independent checks, deliberately not symmetric:

    - A stale *lease* (any kind) is reclaimed through the exact same
      `_reclaim_expired_lease` helper `/lease` uses. Only an exhausted
      implement lease fails the whole run; a review or correction lease
      finalizes just that piece of work and leaves the PR alone (see that
      helper's docstring).
    - A `dispatched` run that was never leased at all has no per-attempt
      state to reclaim, so it can only ever be failed outright.
    - A `correction_running` run whose action never got leased either (the
      owner asked for a correction, but agent-svc was already dead) has an
      open PR to protect, so it is released back to `pr_opened` — never
      failed outright — through the same `_reject_stale_correction` an
      exhausted correction lease uses.

    Both "never leased" buckets share one liveness gate: a large queue is
    not the same as a dead agent-svc, so neither fails anything once any
    local run has been leased or heartbeat within the liveness window —
    work genuinely queued behind other healthy jobs is never punished for
    its place in line.
    """
    now = datetime.now(UTC)
    lease_cutoff = now - timedelta(minutes=_WATCHDOG_LEASE_MINUTES)
    stale_leases = (
        await session.scalars(
            select(AgentRun)
            .where(
                AgentRun.executor == "local",
                AgentRun.lease_id.is_not(None),
                _stale_lease_condition(lease_cutoff, now),
            )
            .options(selectinload(AgentRun.task))
            .order_by(AgentRun.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for stale in stale_leases:
        await _reclaim_expired_lease(session, stale, now)
    if stale_leases:
        await session.commit()

    # An expired `applying` ops lease, and a `proposed`/`approved` ops
    # request nobody ever acted on past its 72h TTL — independent of the
    # local-executor implement/review/correction leases above.
    await agent_ops.reclaim_stale(session, now)
    await session.commit()

    dispatched_cutoff = now - timedelta(minutes=_WATCHDOG_DISPATCHED_MINUTES)
    unclaimed_dispatched = (
        await session.scalars(
            select(AgentRun)
            .where(
                AgentRun.executor == "local",
                AgentRun.status == "dispatched",
                AgentRun.lease_id.is_(None),
                AgentRun.created_at < dispatched_cutoff,
            )
            .options(selectinload(AgentRun.task))
            .order_by(AgentRun.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    ).all()
    unclaimed_corrections = (
        await session.scalars(
            select(AgentRun)
            .where(
                AgentRun.executor == "local",
                AgentRun.status == "correction_running",
                AgentRun.lease_id.is_(None),
                AgentRun.updated_at < dispatched_cutoff,
            )
            .options(selectinload(AgentRun.task))
            .order_by(AgentRun.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not unclaimed_dispatched and not unclaimed_corrections:
        return
    liveness_cutoff = now - timedelta(minutes=_WATCHDOG_LIVENESS_MINUTES)
    agent_svc_alive = await session.scalar(
        select(AgentRun.id)
        .where(AgentRun.executor == "local", AgentRun.heartbeat_at > liveness_cutoff)
        .limit(1)
    )
    if agent_svc_alive is not None:
        return
    for run in unclaimed_corrections:
        action = await session.scalar(
            select(AgentRunAction)
            .where(
                AgentRunAction.agent_run_id == run.id,
                AgentRunAction.kind == "correction",
                AgentRunAction.status == "in_progress",
            )
            .with_for_update()
        )
        _reject_stale_correction(session, run, action)
    for run in unclaimed_dispatched:
        run.status = "failed"
        run.error = _AGENT_SVC_TIMEOUT_ERROR
        run.finished_at = now
        run.notified_at = None
        _clear_lease(run)
        agent_events.record(session, run, phase="lease", error=run.error)
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
    await session.commit()


@router.get("/notifications", response_model=list[AgentNotificationOut])
async def pending_notifications(
    session: DbSession, x_agent_worker_token: str | None = Header(default=None)
) -> list[AgentNotificationOut]:
    _worker_auth(x_agent_worker_token)
    await _fail_stale_local_runs(session)
    runs = (
        await session.scalars(
            select(AgentRun)
            .where(
                or_(
                    (
                        (AgentRun.status == "pr_ready")
                        & (AgentRun.ci_status == "success")
                        & (AgentRun.ci_verified_sha == AgentRun.head_sha)
                    ),
                    AgentRun.status.in_(
                        ["merged", "failed", "deployed", "ops_pending", "ops_applied"]
                    ),
                    (
                        (AgentRun.status == "pr_opened")
                        & (AgentRun.review_status == "findings")
                    ),
                    ((AgentRun.status == "pr_opened") & (AgentRun.review_status == "error")),
                    ((AgentRun.status == "pr_opened") & (AgentRun.ci_status == "failure")),
                    (AgentRun.status == "pr_opened")
                    & AgentRun.telegram_message_id.is_not(None),
                ),
                AgentRun.notified_at.is_(None),
            )
            .options(selectinload(AgentRun.task).selectinload(Task.created_by))
            .order_by(AgentRun.id)
            .limit(20)
        )
    ).all()
    verified_runs: list[AgentRun] = []
    for run in runs:
        if run.status in {"pr_opened", "pr_ready"} and _owner_release_supported(run):
            # A reviewer can push a new commit between the CI monitor tick and
            # the bot tick. Never announce controls for the previous head.
            if not run.pr_url or not run.head_sha:
                continue
            try:
                current_head, is_open = await _current_pr_head(run)
            except HTTPException:
                continue  # Fail closed; the CI monitor will reconcile next tick.
            if current_head != run.head_sha:
                _invalidate_review_and_ci(run, current_head)
                await session.commit()
                continue
            if not is_open:
                continue
            if run.status == "pr_ready" and (
                run.review_status not in {"clean", "advisory"}
                or run.review_sha != run.head_sha
                or run.ci_verified_sha != run.head_sha
            ):
                _refresh_pr_ready(run)
                await session.commit()
                continue
        verified_runs.append(run)
    ops_by_run = await agent_ops.list_for_runs(
        session, [run.id for run in verified_runs if run.executor == "local"]
    )
    notifications = []
    for run in verified_runs:
        ops_rows = ops_by_run.get(run.id, [])
        notifications.append(
            AgentNotificationOut(
                run_id=run.run_id,
                task_id=run.task_id,
                title=run.task.title,
                repo_full_name=run.repo_full_name,
                chat_id=run.task.source_chat_id
                or (run.task.created_by.telegram_id if run.task.created_by else None),
                status=run.status,
                ci_status=run.ci_status,
                ci_url=run.ci_url,
                mode=run.mode,
                pr_url=run.pr_url,
                github_run_url=run.github_run_url,
                head_sha=run.head_sha,
                merged_sha=run.merged_sha,
                deployed_sha=run.deployed_sha,
                telegram_message_id=run.telegram_message_id,
                error=run.error,
                owner_chat_id=settings.owner_telegram_id or None,
                owner_notice_chat_id=run.owner_notice_chat_id,
                owner_notice_message_id=run.owner_notice_message_id,
                owner_controls_available=bool(
                    settings.owner_telegram_id
                    and _owner_release_supported(run)
                    and run.pr_url
                    and run.head_sha
                    and run.status in {"pr_opened", "pr_ready"}
                ),
                summary=run.task.title,
                impact=run.review_summary or run.task.description or "Review pending",
                review=_review_out(run),
                ci_evidence=AgentCiEvidenceOut(
                    state=run.ci_status,
                    verified_head_sha=run.ci_verified_sha,
                    url=run.ci_url,
                ),
                actions=_run_detail(run).actions,
                executor=run.executor,
                qa_ready_url=run.qa_ready_url,
                qa_ready_sha=run.qa_ready_sha,
                qa_deploy_dispatch_status=run.qa_deploy_dispatch_status,
                qa_deploy_dispatch_error=run.qa_deploy_dispatch_error,
                ops_requests=agent_ops.ops_out(ops_rows, run.run_id, include_values=True),
                # Never advertise decision buttons once the run itself can no
                # longer accept a decision (see `decide_ops_request`'s own
                # `run.status in {"cancelled", "failed"}` check) — a
                # `proposed` row can still exist there for a moment until the
                # cancel cascade / watchdog TTL sweep catches up to it.
                ops_controls_available=(
                    run.status not in {"cancelled", "failed"}
                    and any(row.status == "proposed" for row in ops_rows)
                ),
                ops_pending_count=sum(1 for row in ops_rows if row.status == "proposed"),
            )
        )
    return notifications


@router.get("/metrics", response_model=AgentMetricsOut)
async def agent_metrics(
    session: DbSession, owner: OwnerUser, since: datetime | None = None
) -> AgentMetricsOut:
    """Measure the first 20 distinct tasks after an owner-selected start time."""
    effective_since = since or _METRICS_PILOT_START
    if effective_since.tzinfo is None:
        raise HTTPException(status_code=422, detail="since must include a timezone")
    task_ids = (
        await session.scalars(
            select(AgentRun.task_id)
            .where(AgentRun.created_at >= effective_since)
            .group_by(AgentRun.task_id)
            .order_by(func.min(AgentRun.created_at), AgentRun.task_id)
            .limit(20)
        )
    ).all()
    attempts = (
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.created_at >= effective_since, AgentRun.task_id.in_(task_ids))
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    ).all()
    selected: dict[int, AgentRun] = {}
    first_created: dict[int, datetime] = {}
    for run in attempts:
        first_created.setdefault(run.task_id, run.created_at)
        selected[run.task_id] = run
    included = attempts
    current = list(selected.values())
    queue = [
        (run.runner_started_at - run.created_at).total_seconds()
        for run in current
        if run.runner_started_at
    ]
    implementation = [
        (run.pr_opened_at - run.runner_started_at).total_seconds()
        for run in current
        if run.pr_opened_at and run.runner_started_at
    ]
    human_review = [
        (run.merged_at - run.pr_ready_at).total_seconds()
        for run in current
        if run.merged_at and run.pr_ready_at
    ]
    end_to_end = [
        (run.deployed_at - first_created[run.task_id]).total_seconds()
        for run in current
        if run.deployed_at
    ]
    return AgentMetricsOut(
        since=effective_since,
        target_tasks=20,
        sampled_runs=len(current),
        enough_data=len(current) == 20
        and all(run.status in {"deployed", "failed", "cancelled"} for run in current),
        deployed=sum(run.status == "deployed" for run in current),
        failed_attempts=sum(run.status == "failed" for run in included),
        cancelled_attempts=sum(run.status == "cancelled" for run in included),
        retried=sum(run.attempt_index > 1 for run in current),
        queue=_duration_summary(queue),
        implementation=_duration_summary(implementation),
        human_review=_duration_summary(human_review),
        end_to_end=_duration_summary(end_to_end),
        input_tokens=sum(run.input_tokens or 0 for run in included),
        cached_input_tokens=sum(run.cached_input_tokens or 0 for run in included),
        output_tokens=sum(run.output_tokens or 0 for run in included),
    )


@router.get("/events/recent", response_model=list[AgentEventOut])
async def recent_agent_events(
    session: DbSession, owner: OwnerUser, before_id: int | None = None
) -> list[AgentEventOut]:
    """Owner-only history for coding, task preparation and project discussions."""
    if before_id is not None and before_id < 1:
        raise HTTPException(status_code=422, detail="invalid event cursor")
    query = (
        select(AgentEvent, AgentRun, AgentIntake, ProjectDiscussion)
        .outerjoin(AgentRun, AgentEvent.agent_run_id == AgentRun.id)
        .outerjoin(AgentIntake, AgentEvent.agent_intake_id == AgentIntake.id)
        .outerjoin(
            ProjectDiscussion,
            AgentEvent.project_discussion_id == ProjectDiscussion.id,
        )
        .order_by(AgentEvent.id.desc())
        .limit(100)
    )
    if before_id is not None:
        query = query.where(AgentEvent.id < before_id)
    rows = (await session.execute(query)).all()
    result: list[AgentEventOut] = []
    for event, run, intake, discussion in rows:
        flow = "coding" if run else "intake" if intake else "discussion"
        subject = run or intake or discussion
        if subject is None:
            continue
        result.append(
            AgentEventOut(
                id=event.id,
                flow=flow,
                source_id=subject.id,
                task_id=run.task_id if run else intake.task_id if intake else None,
                project_id=(
                    intake.project_id
                    if intake
                    else discussion.project_id if discussion else None
                ),
                status=event.status,
                phase=event.phase,
                error=event.error,
                github_run_url=event.github_run_url,
                input_tokens=(
                    run.input_tokens if run and run.status == event.status else None
                ),
                cached_input_tokens=(
                    run.cached_input_tokens if run and run.status == event.status else None
                ),
                output_tokens=(
                    run.output_tokens if run and run.status == event.status else None
                ),
                created_at=event.created_at,
            )
        )
    return result


@router.post("/{run_id}/notified", status_code=204)
async def mark_notified(
    run_id: str,
    session: DbSession,
    payload: AgentNoticeAck | None = None,
    x_agent_worker_token: str | None = Header(default=None),
) -> None:
    _worker_auth(x_agent_worker_token)
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status not in {
        "pr_opened",
        "pr_ready",
        "merged",
        "failed",
        "deployed",
        "ops_pending",
        "ops_applied",
    }:
        raise HTTPException(status_code=409, detail="run is not finished")
    if (
        run.status == "pr_opened"
        and run.telegram_message_id is None
        and run.owner_notice_message_id is None
    ):
        raise HTTPException(status_code=409, detail="no earlier card to update")
    if run.status == "pr_ready" and (
        run.ci_status != "success"
        or run.ci_verified_sha != run.head_sha
        or run.review_status not in {"clean", "advisory"}
        or run.review_sha != run.head_sha
    ):
        raise HTTPException(
            status_code=409, detail="PR CI and independent review are not verified"
        )
    if run.notified_at is None:
        if payload is not None and payload.message_id is not None:
            run.telegram_message_id = payload.message_id
        run.notified_at = datetime.now(UTC)
        await session.commit()


@router.post("/{run_id}/owner-notified", status_code=204)
async def mark_owner_notified(
    run_id: str,
    payload: AgentNoticeAck,
    session: DbSession,
    x_agent_worker_token: str | None = Header(default=None),
) -> None:
    _worker_auth(x_agent_worker_token)
    if not settings.owner_telegram_id:
        raise HTTPException(status_code=503, detail="owner Telegram ID is not configured")
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status not in {
        "pr_opened",
        "pr_ready",
        "merged",
        "failed",
        "deployed",
        "ops_pending",
        "ops_applied",
    }:
        raise HTTPException(status_code=409, detail="run is not notifiable")
    if payload.message_id is None:
        raise HTTPException(status_code=422, detail="owner message ID is required")
    run.owner_notice_chat_id = settings.owner_telegram_id
    run.owner_notice_message_id = payload.message_id
    await session.commit()


@router.get("/external-pending", response_model=list[ExternalAgentPending])
async def external_pending(
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
    after_id: int = 0,
) -> list[ExternalAgentPending]:
    _image_callback_auth(x_agent_callback_token)
    if after_id < 0:
        raise HTTPException(status_code=400, detail="invalid cursor")
    rows = await session.scalars(
        select(AgentRun)
        .where(
            AgentRun.repo_full_name.in_(PUBLIC_REPOSITORIES),
            AgentRun.status.in_(["pr_ready", "merged"]),
            AgentRun.pr_url.is_not(None),
            AgentRun.id > after_id,
        )
        .order_by(AgentRun.id)
        .limit(50)
    )
    return [
        ExternalAgentPending(
            id=run.id,
            run_id=run.run_id,
            repo_full_name=run.repo_full_name,
            base_branch=run.base_branch,
            pr_url=run.pr_url,
            status=run.status,
            merged_sha=run.merged_sha,
            notified=run.notified_at is not None,
        )
        for run in rows
        if run.pr_url is not None
    ]


@router.get("/ci-pending", response_model=list[AgentCiPending])
async def pending_pr_ci(
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
    after_id: int = 0,
) -> list[AgentCiPending]:
    _image_callback_auth(x_agent_callback_token)
    if after_id < 0:
        raise HTTPException(status_code=400, detail="invalid cursor")
    rows = await session.scalars(
        select(AgentRun)
        .where(
            AgentRun.status.in_(["pr_opened", "pr_ready"]),
            AgentRun.pr_url.is_not(None),
            AgentRun.head_sha.is_not(None),
            AgentRun.id > after_id,
        )
        .order_by(AgentRun.id)
        .limit(50)
    )
    return [
        AgentCiPending(
            id=run.id,
            run_id=run.run_id,
            repo_full_name=run.repo_full_name,
            base_branch=run.base_branch,
            pr_url=run.pr_url,
            head_sha=run.head_sha,
            status=run.status,
            ci_status=run.ci_status,
            ci_verified_sha=run.ci_verified_sha,
            ci_url=run.ci_url,
        )
        for run in rows
        if run.pr_url is not None and run.head_sha is not None
    ]


@router.post("/{run_id}/ci-result", response_model=AgentRunOut)
async def agent_pr_ci_result(
    run_id: str,
    payload: AgentCiResult,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    _image_callback_auth(x_agent_callback_token)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None or run.status not in {"pr_opened", "pr_ready"} or not run.pr_url:
        raise HTTPException(status_code=409, detail="agent PR is not awaiting CI")
    pr_number = run.pr_url.rsplit("/", 1)[-1]
    if not pr_number.isdecimal():
        raise HTTPException(status_code=409, detail="invalid agent PR reference")
    await _verify_pr(run, pr_number, payload.sha)
    if payload.conclusion != "pending" and not payload.github_run_url:
        raise HTTPException(status_code=400, detail="completed PR CI needs its run URL")
    if payload.github_run_url:
        await _verify_pr_ci(run, payload.sha, payload.conclusion, payload.github_run_url)

    if run.head_sha != payload.sha:
        # A new PR commit invalidates every previous review result immediately.
        run.review_status = "pending"
        run.review_sha = None
        run.review_summary = None
        run.review_findings = None
    unchanged = (
        run.status
        == ("pr_ready" if payload.conclusion == "success" and _can_merge(run) else "pr_opened")
        and run.head_sha == payload.sha
        and run.ci_status == payload.conclusion
        and run.ci_url == payload.github_run_url
        and (payload.conclusion != "success" or run.ci_verified_sha == payload.sha)
    )
    if unchanged:
        return AgentRunOut.model_validate(run)

    run.head_sha = payload.sha
    run.ci_status = payload.conclusion
    run.ci_url = payload.github_run_url
    run.ci_verified_sha = payload.sha if payload.conclusion == "success" else None
    run.status = "pr_opened"
    run.notified_at = None if run.telegram_message_id is not None else run.notified_at
    if payload.conclusion == "success":
        _refresh_pr_ready(run)
        run.notified_at = None
        agent_events.record(session, run, phase="ci", github_run_url=run.ci_url)
        if run.executor != "local":
            # A local-executor review is picked up by the next `/lease` call
            # (see `_review_needed`) instead of a repository_dispatch.
            await _dispatch_external_review(run, pr_number)
        target_status = TaskStatus.REVIEW
    else:
        run.status = "pr_opened"
        run.pr_ready_at = None
        agent_events.record(
            session,
            run,
            status="ci_failed" if payload.conclusion == "failure" else "ci_pending",
            phase="ci",
            error=("PR CI xato bilan tugadi" if payload.conclusion == "failure" else None),
            github_run_url=run.ci_url,
        )
        target_status = (
            TaskStatus.BLOCKED if payload.conclusion == "failure" else TaskStatus.IN_PROGRESS
        )
    if can_transition(TaskStatus(run.task.status), target_status):
        old_task_status = run.task.status
        run.task.status = target_status
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={
                "from": old_task_status,
                "to": target_status.value,
                "agent_run_id": run_id,
            },
        )
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/review-result", response_model=AgentRunOut)
async def agent_pr_review_result(
    run_id: str,
    payload: AgentReviewResult,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
    x_agent_lease_id: str | None = Header(default=None),
) -> AgentRunOut:
    _image_callback_auth(x_agent_callback_token)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Computed once, before any mutation below: whether the header names the
    # review lease this run currently holds. A stale status or head below
    # must still release that lease rather than let it sit until it times
    # out on its own and burns one of the two review attempts for nothing.
    has_matching_review_lease = (
        run.executor == "local"
        and run.lease_kind == "review"
        and run.lease_id is not None
        and run.lease_id == x_agent_lease_id
    )
    if run.status not in {"pr_opened", "pr_ready"} or not run.pr_url:
        if has_matching_review_lease:
            _clear_lease(run)
            await session.commit()
        raise HTTPException(status_code=409, detail="agent PR is not awaiting review")
    if run.head_sha != payload.sha:
        if has_matching_review_lease:
            _clear_lease(run)
            await session.commit()
        raise HTTPException(status_code=409, detail="review does not match current PR head")
    if run.executor == "local":
        _require_live_lease(run, x_agent_lease_id, kind="review")
    pr_number = run.pr_url.rsplit("/", 1)[-1]
    if not pr_number.isdecimal():
        raise HTTPException(status_code=409, detail="invalid agent PR reference")
    await _verify_pr(run, pr_number, payload.sha)
    findings = [finding.model_dump() for finding in payload.findings]
    blocking_findings = [
        finding for finding in payload.findings if finding.severity in {"P1", "P2"}
    ]
    advisory_findings = [finding for finding in payload.findings if finding.severity == "P3"]
    review_state = payload.state or (
        "findings" if blocking_findings else "advisory" if advisory_findings else "clean"
    )
    invalid_state = (
        (review_state == "clean" and findings)
        or (review_state == "advisory" and (not advisory_findings or blocking_findings))
        or (review_state == "findings" and not blocking_findings)
        or (review_state == "error" and findings)
    )
    if invalid_state:
        raise HTTPException(status_code=422, detail="review state does not match findings")
    run.review_status = review_state
    run.review_sha = payload.sha
    run.review_summary = payload.summary
    run.review_findings = findings
    run.status = "pr_opened"
    if _refresh_pr_ready(run):
        run.notified_at = None
        agent_events.record(session, run, phase="review", github_run_url=run.ci_url)
    else:
        run.notified_at = None
        agent_events.record(
            session,
            run,
            status=(
                "review_findings"
                if blocking_findings
                else (
                    "review_advisory"
                    if advisory_findings
                    else (
                        "review_error"
                        if review_state == "error"
                        else "review_passed_ci_pending"
                    )
                )
            ),
            phase="review",
        )
    if run.executor == "local":
        _clear_lease(run)
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.get("/tasks/{task_id}", response_model=list[AgentRunOut])
async def list_task_runs(
    task_id: int, session: DbSession, user: CurrentUser
) -> list[AgentRunOut]:
    task = await _task(session, task_id)
    if not await can_see_task(session, user, task):
        raise HTTPException(status_code=404, detail="task not found")
    runs = await session.scalars(
        select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.id.desc())
    )
    return [AgentRunOut.model_validate(row) for row in runs]


async def _load_run_detail(session: DbSession, run: AgentRun) -> AgentRunDetailOut:
    """The full `GET /agent-runs/{run_id}` body for an already-loaded,
    already-locked run: refresh PR-live state if one is open, then the
    detail plus its owner-only `ops_requests`. Shared with
    `GET /agent-ops/{ops_id}`, which resolves to the same run a different way."""
    is_open = False
    if run.pr_url and run.status in {"pr_opened", "pr_ready", "correction_running"}:
        current_head, is_open = await _current_pr_head(run)
        if current_head != run.head_sha:
            _invalidate_review_and_ci(run, current_head)
            await session.commit()
    detail = await _run_detail_with_ops(session, run)
    if not is_open:
        detail.actions = {
            "merge": AgentActionAvailability(available=False),
            "correction": AgentActionAvailability(available=False),
        }
    return detail


@router.get("/{run_id}", response_model=AgentRunDetailOut)
async def agent_run_detail(
    run_id: str, session: DbSession, owner: OwnerUser
) -> AgentRunDetailOut:
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return await _load_run_detail(session, run)


async def _request_owner_action(
    run_id: str,
    kind: str,
    expected_head_sha: str,
    action_id: str,
    session: DbSession,
    *,
    instruction: str | None = None,
) -> AgentActionOut | Response:
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    action_payload: dict[str, object] = {"expected_head_sha": expected_head_sha}
    if instruction is not None:
        action_payload["instruction"] = instruction
    request_hash = hashlib.sha256(
        json.dumps(action_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    action = await session.get(AgentRunAction, action_id)
    retry_existing = False
    if action is not None:
        if (
            action.agent_run_id != run.id
            or action.kind != kind
            or action.request_hash != request_hash
        ):
            return _action_error(
                "conflict",
                "action_id was already used for a different request",
                status_code=409,
            )
        if action.status != "retryable":
            return _action_response(action, run)
        retry_existing = True
    if not _owner_release_supported(run) or not run.pr_url:
        if retry_existing:
            assert action is not None
            _reject_retryable_action(action, "this run no longer has an owner-controlled PR")
            await session.commit()
            return _action_response(action, run)
        return _action_error(
            "not_ready", "this run has no owner-controlled private PR", status_code=409
        )
    current_head, is_open = await _current_pr_head(run)
    if current_head != run.head_sha:
        _invalidate_review_and_ci(run, current_head)
        if retry_existing:
            assert action is not None
            _reject_retryable_action(
                action, "PR head changed; fetch the current run and retry"
            )
        await session.commit()
    if not is_open:
        if retry_existing:
            assert action is not None
            _reject_retryable_action(action, "PR is no longer open")
            await session.commit()
            return _action_response(action, run)
        return _action_error("not_ready", "PR is no longer open", status_code=409)
    if expected_head_sha != current_head:
        if retry_existing:
            assert action is not None
            _reject_retryable_action(
                action, "PR head changed; fetch the current run and retry"
            )
            await session.commit()
            return _action_response(action, run)
        return _action_error(
            "stale_head",
            "PR head changed; fetch the current run and retry",
            status_code=409,
            current_head_sha=current_head,
        )
    if kind == "merge" and not _can_merge(run):
        if retry_existing:
            assert action is not None
            _reject_retryable_action(
                action, "CI and a clean review are not valid on the current head"
            )
            await session.commit()
            return _action_response(action, run)
        return _action_error(
            "not_ready",
            "CI and a clean review must pass on the current head",
            status_code=409,
        )
    if kind == "correction" and run.status not in {"pr_opened", "pr_ready"}:
        return _action_error("not_ready", "PR is not open for correction", status_code=409)
    target_token = (
        settings.github_public_agent_token
        if run.repo_full_name in PUBLIC_REPOSITORIES
        else (
            settings.github_agent_qa_token
            if settings.agent_qa_enabled and run.repo_full_name == settings.agent_qa_repository
            else settings.github_agent_token
        )
    )
    if not settings.github_agent_token or not target_token:
        raise HTTPException(status_code=503, detail="release workflow token is not configured")

    if not retry_existing:
        action = AgentRunAction(
            action_id=action_id,
            agent_run_id=run.id,
            kind=kind,
            request_hash=request_hash,
            request_data=action_payload,
            status="accepted",
        )
        session.add(action)
    else:
        assert action is not None
        action.status = "accepted"
        action.result = None
    assert action is not None
    if kind == "correction" and run.executor == "local":
        # agent-svc leases this via POST /agent-runs/lease (lease step 3)
        # instead of a repository_dispatch, but the run and action must
        # transition exactly like a successful GitHub dispatch, right now —
        # otherwise the PR would sit "open" with a queued-but-invisible
        # correction, letting a concurrent merge, cancel, or second
        # correction request interfere before agent-svc ever picks it up.
        if run.lease_kind == "review" and run.lease_id is not None:
            # A live review lease never blocks run.status (it stays
            # pr_opened/pr_ready throughout), so a correction request can
            # legitimately arrive while one is outstanding. Release it now
            # instead of leaving it to expire on its own: the review is
            # about to be invalidated by this correction anyway, and
            # agent-svc's own heartbeat/review-result call for it will just
            # 409 harmlessly once it is gone.
            _clear_lease(run)
        action.status = "in_progress"
        run.status = "correction_running"
        run.notified_at = None
        agent_events.record(session, run, phase="correction")
        await session.commit()
        return _action_response(action, run)
    await session.commit()
    payload: dict[str, object] = {
        "expected_head_sha": expected_head_sha,
        "task_id": run.task_id,
    }
    if instruction is not None:
        payload["instruction"] = instruction
    try:
        await _dispatch_release_action(run, action, payload)
    except (HTTPException, httpx.HTTPError) as exc:
        is_permanent = isinstance(exc, HTTPException) and exc.status_code < 500
        action.status = "rejected" if is_permanent else "retryable"
        action.result = {
            "message": (
                "GitHub rejected this release action"
                if is_permanent
                else "GitHub dispatch failed; retry this action with the same action ID"
            )
        }
        await session.commit()
        if isinstance(exc, HTTPException):
            log.warning("agent_release_dispatch_rejected", run_id=run_id, action=kind)
        else:
            log.warning("agent_release_dispatch_failed", run_id=run_id, action=kind)
        return _action_response(action, run)
    action.status = "in_progress"
    if kind == "correction":
        run.status = "correction_running"
        run.notified_at = None
        agent_events.record(session, run, phase="correction")
    await session.commit()
    return _action_response(action, run)


@router.post("/{run_id}/merge", response_model=AgentActionOut)
async def request_agent_merge(
    run_id: str,
    payload: AgentReleaseRequest,
    session: DbSession,
    owner: OwnerUser,
) -> AgentActionOut | Response:
    return await _request_owner_action(
        run_id,
        "merge",
        payload.expected_head_sha,
        str(payload.action_id),
        session,
    )


@router.post("/{run_id}/corrections", response_model=AgentActionOut)
async def request_agent_correction(
    run_id: str,
    payload: AgentCorrectionRequest,
    session: DbSession,
    owner: OwnerUser,
) -> AgentActionOut | Response:
    return await _request_owner_action(
        run_id,
        "correction",
        payload.expected_head_sha,
        str(payload.action_id),
        session,
        instruction=payload.instruction,
    )


@router.get("/{run_id}/actions/{action_id}", response_model=AgentActionDetailOut)
async def agent_action_status(
    run_id: str,
    action_id: str,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentActionDetailOut:
    _image_callback_auth(x_agent_callback_token)
    action = await session.scalar(
        select(AgentRunAction)
        .join(AgentRun, AgentRun.id == AgentRunAction.agent_run_id)
        .where(AgentRun.run_id == run_id, AgentRunAction.action_id == action_id)
    )
    if action is None:
        raise HTTPException(status_code=404, detail="release action not found")
    return AgentActionDetailOut(
        action_id=UUID(action.action_id),
        kind=cast(Literal["merge", "correction"], action.kind),
        status=cast(
            Literal["accepted", "in_progress", "completed", "rejected", "retryable"],
            action.status,
        ),
        request=action.request_data,
        result=action.result,
    )


@router.post("/{run_id}/action-result", response_model=AgentRunOut)
async def agent_action_result(
    run_id: str,
    payload: AgentActionResult,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
    x_agent_lease_id: str | None = Header(default=None),
) -> AgentRunOut:
    _image_callback_auth(x_agent_callback_token)
    await _lock_qa_project_for_run(session, run_id)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    action = await session.scalar(
        select(AgentRunAction)
        .where(
            AgentRunAction.action_id == str(payload.action_id),
            AgentRunAction.agent_run_id == run.id,
        )
        .with_for_update()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="release action not found")
    if action.status == "completed":
        if action.result and action.result.get("head_sha") == payload.head_sha:
            return AgentRunOut.model_validate(run)
        raise HTTPException(status_code=409, detail="release action result changed")
    if action.status == "rejected":
        return AgentRunOut.model_validate(run)
    # Merge actions keep the GitHub workflow for both executors; only a local
    # correction is reported by agent-svc and needs its lease checked. Checked
    # after the idempotent replays above so a retry of an already-finished
    # result never 409s just because the lease was already cleared.
    if run.executor == "local" and action.kind == "correction":
        _require_live_lease(run, x_agent_lease_id, kind="correction")
    if payload.status == "rejected":
        action.status = (
            "retryable" if await _merge_rejection_is_retryable(run, action) else "rejected"
        )
        action.result = {"message": payload.message or "GitHub rejected the action"}
        if action.kind == "correction" and run.status == "correction_running":
            run.status = "pr_opened"
            _refresh_pr_ready(run)
        if run.executor == "local" and action.kind == "correction":
            _clear_lease(run)
        await session.commit()
        return AgentRunOut.model_validate(run)

    if action.kind == "merge":
        if (
            payload.head_sha != run.head_sha
            or action.request_data.get("expected_head_sha") != run.head_sha
            or not _owner_release_supported(run)
            or not payload.merge_sha
        ):
            raise HTTPException(
                status_code=409, detail="merge result does not match current PR head"
            )
        merged_head = await _verify_deployment(run, payload.merge_sha)
        if merged_head != payload.head_sha:
            raise HTTPException(status_code=409, detail="merged PR head changed")
        run.status = "merged"
        run.merged_sha = payload.merge_sha
        run.merged_at = run.merged_at or datetime.now(UTC)
        run.notified_at = None
        if settings.agent_qa_enabled and run.repo_full_name == settings.agent_qa_repository:
            run.qa_deploy_dispatch_status = "pending"
            run.qa_deploy_dispatch_error = None
            run.qa_deploy_dispatched_at = None
        agent_events.record(session, run, phase="merge")
        action.result = {
            "head_sha": payload.head_sha,
            "merge_sha": payload.merge_sha,
            "message": payload.message,
        }
    elif action.kind == "correction":
        if (
            run.status != "correction_running"
            or action.request_data.get("expected_head_sha") != run.head_sha
        ):
            raise HTTPException(
                status_code=409, detail="correction action is no longer current"
            )
        if not payload.head_sha:
            raise HTTPException(
                status_code=409, detail="correction result needs the current PR head"
            )
        pr_number = run.pr_url.rsplit("/", 1)[-1] if run.pr_url else ""
        if not pr_number.isdecimal():
            raise HTTPException(status_code=409, detail="invalid agent PR reference")
        current_head, is_open = await _current_pr_head(run)
        if not is_open or current_head != payload.head_sha:
            raise HTTPException(
                status_code=409, detail="correction PR head changed before recording"
            )
        if payload.head_sha != run.head_sha:
            _invalidate_review_and_ci(run, payload.head_sha)
        else:
            # An owner may be asking for a false-positive finding to be
            # reconsidered. Preserve CI for this exact commit and require a new
            # independent review result before making Merge available.
            run.status = "pr_opened"
            run.pr_ready_at = None
            run.review_status = "pending"
            run.review_sha = None
            run.review_summary = None
            run.review_findings = None
            run.notified_at = None
        action.result = {"head_sha": payload.head_sha, "message": payload.message}
        agent_events.record(
            session, run, phase="correction", github_run_url=run.github_run_url
        )
    else:
        raise HTTPException(status_code=409, detail="unknown release action")
    action.status = "completed"
    if run.executor == "local" and action.kind == "correction":
        _clear_lease(run)
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.get("/{run_id}/status", response_model=AgentRunOut)
async def agent_run_status(
    run_id: str,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    if (
        not settings.agent_callback_token
        or not x_agent_callback_token
        or not hmac.compare_digest(x_agent_callback_token, settings.agent_callback_token)
    ):
        raise HTTPException(status_code=401, detail="invalid callback token")
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return AgentRunOut.model_validate(run)


def _image_callback_auth(token: str | None) -> None:
    if (
        not settings.agent_callback_token
        or not token
        or not hmac.compare_digest(token, settings.agent_callback_token)
    ):
        raise HTTPException(status_code=401, detail="invalid callback token")


@router.get("/{run_id}/images", response_model=list[AgentImageOut])
async def list_run_images(
    run_id: str,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> list[AgentImageOut]:
    _image_callback_auth(x_agent_callback_token)
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    rows = await session.scalars(
        select(Attachment)
        .where(Attachment.task_id == run.task_id)
        .order_by(Attachment.id)
        .limit(3)
    )
    return [
        AgentImageOut(id=row.id, mime=row.mime, size=row.size)
        for row in rows
        if row.mime in IMAGE_MIMES
    ]


@router.get("/{run_id}/images/{attachment_id}")
async def download_run_image(
    run_id: str,
    attachment_id: int,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> Response:
    _image_callback_auth(x_agent_callback_token)
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    image = await session.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.task_id == run.task_id,
        )
    )
    if image is None or image.mime not in IMAGE_MIMES:
        raise HTTPException(status_code=404, detail="image not found")
    data = await telegram_image(image.tg_file_id, image.mime, image.size)
    return Response(content=data, media_type=image.mime)


@router.post(
    "/tasks/{task_id}", response_model=AgentRunOut, status_code=status.HTTP_201_CREATED
)
async def start_agent_run(
    task_id: int,
    session: DbSession,
    user: CurrentUser,
    payload: AgentRunStart | None = None,
) -> AgentRunOut:
    task = await _task(session, task_id, lock=True)
    mode = payload.mode if payload else "pr"
    if mode == "fast" and not settings.agent_fast_enabled:
        raise HTTPException(status_code=503, detail="fast mode is not enabled")
    if mode == "fast" and user.telegram_id != settings.owner_telegram_id:
        raise HTTPException(status_code=403, detail="fast mode is owner-only")
    if not (
        user.can_use_codex
        or (settings.owner_telegram_id and user.telegram_id == settings.owner_telegram_id)
    ):
        raise HTTPException(status_code=403, detail="Codex access is not enabled")
    if not await can_edit_task(session, user, task):
        raise HTTPException(status_code=403, detail="not allowed to delegate this task")
    if task.status in {"done", "cancelled"}:
        raise HTTPException(status_code=409, detail="task is closed")
    repo = task.project.repo_full_name
    branch = task.project.default_branch
    repository = repository_for(
        task.project.key, repo, branch, include_qa=settings.agent_qa_enabled
    )
    if repository is not None and not repository.private and not settings.agent_public_enabled:
        raise HTTPException(status_code=503, detail="public project agents are not enabled")
    if mode == "fast" and (repository is None or not repository.fast_enabled):
        raise HTTPException(status_code=409, detail="fast mode is limited to Task Manager")
    if mode == "fast" and branch != "main":
        raise HTTPException(status_code=409, detail="fast mode requires the main branch")
    allowlist = {
        name.strip().lower() for name in settings.github_agent_allowed_repos.split(",")
    }
    if settings.agent_qa_enabled:
        allowlist.add(settings.agent_qa_repository.lower())
    if repository is None or repo is None or repo.lower() not in allowlist:
        raise HTTPException(
            status_code=409, detail="project repository is not enabled for agents"
        )
    target_token = (
        settings.github_public_agent_token
        if repository is not None and not repository.private
        else (
            settings.github_agent_qa_token
            if repository is not None and repository.qa_only
            else settings.github_agent_token
        )
    )
    if (
        not settings.github_agent_token
        or not target_token
        or not settings.agent_callback_token
    ):
        raise HTTPException(status_code=503, detail="agent integration is not configured")
    if len(task.description or "") > 12000:
        raise HTTPException(
            status_code=422, detail="task description is too long for an agent run"
        )

    revision = _revision(task, mode)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.task_id == task_id, AgentRun.task_revision == revision)
        .order_by(AgentRun.attempt_index.desc())
    )
    if run is not None and run.status in {"failed", "cancelled"}:
        if run.attempt_index >= 2:
            raise HTTPException(status_code=409, detail="agent run retry limit reached")
        next_attempt = run.attempt_index + 1
        run = None
    else:
        next_attempt = 1
    if run is not None and run.status != "pending":
        # A repeated Telegram callback must never make a second PR.
        return AgentRunOut.model_validate(run)
    if run is None:
        run = AgentRun(
            run_id=str(uuid4()),
            task_id=task_id,
            task_revision=revision,
            attempt_index=next_attempt,
            repo_full_name=repo,
            base_branch=branch,
            mode=mode,
            status="pending",
            attempts=0,
        )
        session.add(run)
        try:
            await session.flush()
            agent_events.record(session, run)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(AgentRun).where(
                    AgentRun.task_id == task_id,
                    AgentRun.task_revision == revision,
                    AgentRun.attempt_index == next_attempt,
                )
            )
            if existing is None:
                raise
            return AgentRunOut.model_validate(existing)
        await session.refresh(run)
    if _use_local_executor(task.project.key, mode):
        # agent-svc leases this run itself (see POST /agent-runs/lease); it
        # never goes through repository_dispatch. `attempts` means something
        # different once a run is local (implement-lease retries, not
        # GitHub dispatch retries): if this row is a leftover "pending" run
        # from a GitHub attempt made before the project was added to
        # AGENT_LOCAL_EXECUTOR_PROJECTS, reset it so a stale dispatch-retry
        # count can't prematurely trip the local retry cap.
        run.executor = "local"
        run.status = "dispatched"
        run.attempts = 0
        agent_events.record(session, run)
        await session.commit()
        return AgentRunOut.model_validate(run)
    if run.attempts >= 2:
        run.status = "failed"
        run.error = "GitHub dispatch retry limit reached"
        run.finished_at = datetime.now(UTC)
        agent_events.record(session, run, phase="dispatch", error=run.error)
        await session.commit()
        raise HTTPException(status_code=409, detail="agent dispatch retry limit reached")

    run.status = "dispatching"
    agent_events.record(session, run)
    await session.commit()
    try:
        github_status = await _dispatch(run, task)
    except HTTPException as exc:
        run.status = "failed"
        run.error = str(exc.detail)
        run.finished_at = datetime.now(UTC)
        agent_events.record(session, run, phase="dispatch", error=run.error)
        await session.commit()
        raise
    except httpx.RequestError as exc:
        run.status = "pending"
        run.attempts += 1
        agent_events.record(
            session, run, phase="dispatch", error="GitHub dispatch network failed"
        )
        await session.commit()
        log.error("agent_dispatch_network_failed", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=502, detail="GitHub dispatch failed") from exc
    run.attempts += 1
    if github_status != 204:
        run.status = "pending"
        agent_events.record(session, run, phase="dispatch", error="GitHub dispatch rejected")
        await session.commit()
        log.error("agent_dispatch_rejected", task_id=task_id, github_status=github_status)
        raise HTTPException(status_code=502, detail="GitHub dispatch was rejected")
    await session.refresh(run)
    if run.status in {"dispatching", "pending"}:
        run.status = "dispatched"
        agent_events.record(session, run)
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/cancel", response_model=AgentRunOut)
async def cancel_agent_run(run_id: str, session: DbSession, user: CurrentUser) -> AgentRunOut:
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None or not await can_see_task(session, user, run.task):
        raise HTTPException(status_code=404, detail="run not found")
    if not await can_edit_task(session, user, run.task):
        raise HTTPException(status_code=403, detail="not allowed to cancel this run")
    if run.status == "cancelled":
        return AgentRunOut.model_validate(run)
    if run.status not in {
        "pending",
        "dispatching",
        "dispatched",
        "running",
        "validating",
        "pr_opened",
        "pr_ready",
        "ops_pending",
    }:
        raise HTTPException(status_code=409, detail="agent run is already finished")
    if run.status in {"pr_opened", "pr_ready", "ops_pending"}:
        if run.status in {"pr_opened", "pr_ready"}:
            await _close_pr(run)
        if can_transition(TaskStatus(run.task.status), TaskStatus.TODO):
            old = run.task.status
            run.task.status = TaskStatus.TODO
            activity.record(
                session,
                task_id=run.task_id,
                actor=user,
                kind=ActivityKind.STATUS_CHANGED,
                payload={
                    "from": old,
                    "to": TaskStatus.TODO.value,
                    "agent_run_id": run_id,
                },
            )
    elif run.executor != "local":
        await _cancel_github(run)
    # A bulk UPDATE guarded by `status IN (...)` in its own WHERE clause,
    # never a SELECT-then-Python-loop-then-write: the run is already locked
    # above, but the ops row is not, so `POST /agent-ops/lease` (which locks
    # both atomically, see `app.services.agent_ops`) could concurrently flip
    # one to `applying` in between. Re-checking status atomically inside the
    # UPDATE itself means that row is simply left untouched rather than
    # clobbered back to `cancelled` under an in-flight apply.
    await session.execute(
        update(AgentOpsRequest)
        .where(
            AgentOpsRequest.agent_run_id == run.id,
            AgentOpsRequest.status.in_(("proposed", "approved")),
        )
        .values(status="cancelled")
    )
    _clear_lease(run)
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    agent_events.record(session, run)
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/merged", response_model=AgentRunOut)
async def agent_run_merged(
    run_id: str,
    payload: AgentMerge,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    _image_callback_auth(x_agent_callback_token)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).with_for_update()
    )
    if run is None or run.repo_full_name not in PUBLIC_REPOSITORIES or not run.pr_url:
        raise HTTPException(status_code=404, detail="external agent PR not found")
    if run.status == "merged" and run.merged_sha == payload.sha:
        return AgentRunOut.model_validate(run)
    if run.status not in {"pr_opened", "pr_ready"}:
        raise HTTPException(status_code=409, detail="agent PR is not ready for merge tracking")
    run.head_sha = await _verify_deployment(run, payload.sha)
    run.merged_sha = payload.sha
    run.merged_at = run.merged_at or datetime.now(UTC)
    run.status = "merged"
    run.notified_at = None
    agent_events.record(session, run)
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/deployed", response_model=AgentRunOut)
async def agent_run_deployed(
    run_id: str,
    payload: AgentDeployment,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    if (
        not settings.agent_callback_token
        or not x_agent_callback_token
        or not hmac.compare_digest(x_agent_callback_token, settings.agent_callback_token)
    ):
        raise HTTPException(status_code=401, detail="invalid callback token")
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).options(selectinload(AgentRun.task))
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status == "deployed" and run.deployed_sha == payload.sha:
        return AgentRunOut.model_validate(run)
    if run.pr_url:
        eligible_statuses = {"pr_ready", "merged"}
        if run.repo_full_name == DISPATCH_REPOSITORY:
            # The narrow docs/CSS auto-merge independently proves exact PR CI.
            eligible_statuses.add("pr_opened")
        if run.status not in eligible_statuses:
            raise HTTPException(status_code=409, detail="agent PR is not ready")
        if run.status == "merged" and run.merged_sha != payload.sha:
            raise HTTPException(status_code=409, detail="deployed SHA differs from merge")
        run.head_sha = await _verify_deployment(run, payload.sha)
    else:
        if run.status not in {"validating", "publishing", "deploying", "failed"}:
            raise HTTPException(status_code=409, detail="fast run is not deploying")
        await _verify_fast_commit(run, payload.sha)
    expected_url = f"https://github.com/{run.repo_full_name}/actions/runs/"
    suffix = payload.github_run_url.removeprefix(expected_url)
    if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
        raise HTTPException(status_code=400, detail="invalid deploy run URL")
    run.status = "deployed"
    run.deployed_sha = payload.sha
    run.deployed_at = run.deployed_at or datetime.now(UTC)
    run.github_run_url = payload.github_run_url
    run.finished_at = datetime.now(UTC)
    run.notified_at = None
    agent_events.record(session, run, github_run_url=run.github_run_url)
    if can_transition(TaskStatus(run.task.status), TaskStatus.DONE) or (
        run.mode == "fast" and run.task.status == TaskStatus.BLOCKED
    ):
        # A publisher callback can fail after the validated main push and mark
        # the task blocked. The verified production deploy is the final source
        # of truth for this exact commit.
        old = run.task.status
        run.task.status = TaskStatus.DONE
        run.task.done_at = datetime.now(UTC)
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={"from": old, "to": TaskStatus.DONE.value, "agent_run_id": run_id},
        )
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/qa-deployed", response_model=AgentRunOut)
async def agent_qa_run_deployed(
    run_id: str,
    payload: AgentQaDeployment,
    session: DbSession,
    x_agent_qa_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    """Accept completion for the exact image authorized by the QA workflow.

    The QA workflow probes its private service, then reports through a token
    scoped to this one repository instead of receiving the production callback
    credential used by every agent workflow. The whole-workflow concurrency
    group serializes owner merge and deployment mutations. A newer merge may
    arrive while an already authorized image is deploying, so completion stays
    bound to that exact merge/readiness evidence; freshness checks apply when a
    new deployment is authorized or retried.
    """
    _qa_deploy_auth(x_agent_qa_callback_token)
    await _lock_qa_project_for_run(session, run_id)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).options(selectinload(AgentRun.task))
    )
    if run is None or run.repo_full_name != settings.agent_qa_repository:
        raise HTTPException(status_code=404, detail="QA agent run not found")
    if payload.ready_url != settings.agent_qa_ready_url or payload.ready_sha != payload.sha:
        raise HTTPException(
            status_code=409,
            detail="QA readiness evidence does not match the merged SHA",
        )
    if run.status == "deployed" and run.deployed_sha == payload.sha:
        return AgentRunOut.model_validate(run)
    if run.status != "merged" or run.merged_sha != payload.sha:
        raise HTTPException(status_code=409, detail="QA run is not merged at this SHA")
    head_sha = await _verify_deployment(run, payload.sha)
    expected_url = f"https://github.com/{settings.agent_qa_repository}/actions/runs/"
    suffix = payload.github_run_url.removeprefix(expected_url)
    if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
        raise HTTPException(status_code=400, detail="invalid QA deployment run URL")
    run.head_sha = head_sha
    run.status = "deployed"
    run.deployed_sha = payload.sha
    run.deployed_at = run.deployed_at or datetime.now(UTC)
    run.qa_ready_url = payload.ready_url
    run.qa_ready_sha = payload.ready_sha
    run.qa_ready_at = run.deployed_at
    run.error = None
    run.github_run_url = payload.github_run_url
    run.finished_at = datetime.now(UTC)
    run.notified_at = None
    agent_events.record(session, run, github_run_url=payload.github_run_url)
    if can_transition(TaskStatus(run.task.status), TaskStatus.DONE):
        old = run.task.status
        run.task.status = TaskStatus.DONE
        run.task.done_at = datetime.now(UTC)
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={"from": old, "to": TaskStatus.DONE.value, "agent_run_id": run_id},
        )
    await session.commit()
    return AgentRunOut.model_validate(run)


async def _has_newer_qa_owner_merge(session: DbSession, run: AgentRun) -> bool:
    if (
        not run.merged_sha
        or not re.fullmatch(r"[0-9a-f]{40}", run.merged_sha)
        or not run.base_branch
    ):
        return True
    newer_merge_shas = (
        await session.scalars(
            select(AgentRun.merged_sha)
            .join(AgentRunAction, AgentRunAction.agent_run_id == AgentRun.id)
            .where(
                AgentRun.repo_full_name == settings.agent_qa_repository,
                AgentRun.id != run.id,
                AgentRun.status.in_(["merged", "deployed"]),
                AgentRun.merged_sha.is_not(None),
                AgentRunAction.kind == "merge",
                AgentRunAction.status == "completed",
                AgentRunAction.result["merge_sha"].as_string() == AgentRun.merged_sha,
                AgentRunAction.request_data["expected_head_sha"].as_string()
                == AgentRunAction.result["head_sha"].as_string(),
            )
            .distinct()
        )
    ).all()
    if not newer_merge_shas:
        return False
    if any(
        not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_sha)
        for merge_sha in newer_merge_shas
    ):
        return True
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{settings.agent_qa_repository}/compare/"
            f"{run.merged_sha}...{quote(run.base_branch, safe='')}",
            headers=_headers(settings.agent_qa_repository),
        )
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="QA merge ordering verification failed")
    comparison = response.json()
    compare_status = comparison.get("status")
    if compare_status == "identical":
        return False
    if compare_status != "ahead":
        return True
    commits = comparison.get("commits")
    total_commits = comparison.get("total_commits")
    if (
        not isinstance(commits, list)
        or not isinstance(total_commits, int)
        or total_commits < len(commits)
        or total_commits > len(commits)
    ):
        return True
    owner_merge_set = set(newer_merge_shas)
    for commit in commits:
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return True
        if sha in owner_merge_set:
            return True
    return False


async def _lock_qa_project_for_run(session: DbSession, run_id: str) -> bool:
    repository = await session.scalar(
        select(AgentRun.repo_full_name).where(AgentRun.run_id == run_id)
    )
    if repository != settings.agent_qa_repository:
        return False
    project = await session.scalar(
        select(Project).where(Project.key == "agent-qa").with_for_update()
    )
    if project is None:
        raise HTTPException(status_code=409, detail="QA project is unavailable")
    return True


@router.post("/{run_id}/qa-deployment-failed", response_model=AgentRunOut)
async def agent_qa_deployment_failed(
    run_id: str,
    payload: AgentQaDeploymentFailure,
    session: DbSession,
    x_agent_qa_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    """Record a bounded QA deploy failure without marking its task complete."""
    _qa_deploy_auth(x_agent_qa_callback_token)
    await _lock_qa_project_for_run(session, run_id)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None or run.repo_full_name != settings.agent_qa_repository:
        raise HTTPException(status_code=404, detail="QA agent run not found")
    if run.status == "deployed" and run.deployed_sha == payload.merge_sha:
        return AgentRunOut.model_validate(run)
    if (
        run.status != "merged"
        or run.merged_sha != payload.merge_sha
        or run.head_sha != payload.expected_head_sha
        or run.qa_deploy_dispatch_status != "dispatched"
        or run.merged_at is None
    ):
        raise HTTPException(status_code=409, detail="QA failure does not match the merged run")
    expected_url = f"https://github.com/{settings.agent_qa_repository}/actions/runs/"
    suffix = payload.github_run_url.removeprefix(expected_url)
    if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
        raise HTTPException(status_code=400, detail="invalid QA deployment run URL")
    action = await session.scalar(
        select(AgentRunAction)
        .where(
            AgentRunAction.action_id == str(payload.action_id),
            AgentRunAction.agent_run_id == run.id,
        )
        .with_for_update()
    )
    if (
        action is None
        or action.kind != "merge"
        or action.status != "completed"
        or action.request_data.get("expected_head_sha") != payload.expected_head_sha
        or (action.result or {}).get("head_sha") != payload.expected_head_sha
        or (action.result or {}).get("merge_sha") != payload.merge_sha
    ):
        raise HTTPException(
            status_code=409,
            detail="QA failure is not tied to the completed owner merge",
        )
    error = _QA_DEPLOY_FAILURE_MESSAGES[payload.failure_code]
    if run.error == error and run.github_run_url == payload.github_run_url:
        return AgentRunOut.model_validate(run)
    run.error = error
    run.github_run_url = payload.github_run_url
    run.notified_at = None
    agent_events.record(
        session,
        run,
        status="qa_deploy_failed",
        phase="qa_deploy",
        error=error,
        github_run_url=payload.github_run_url,
    )
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post("/{run_id}/qa-deploy-dispatch-result", response_model=AgentRunOut)
async def agent_qa_deploy_dispatch_result(
    run_id: str,
    payload: AgentQaDeployDispatchResult,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
) -> AgentRunOut:
    """Record whether the trusted central workflow queued QA deployment."""
    _image_callback_auth(x_agent_callback_token)
    await _lock_qa_project_for_run(session, run_id)
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task))
        .with_for_update()
    )
    if run is None or run.repo_full_name != settings.agent_qa_repository:
        raise HTTPException(status_code=404, detail="QA agent run not found")
    if run.status not in {"merged", "deployed"} or run.merged_sha != payload.sha:
        raise HTTPException(status_code=409, detail="QA merge does not match dispatch result")
    action = await session.scalar(
        select(AgentRunAction).where(
            AgentRunAction.action_id == str(payload.action_id),
            AgentRunAction.agent_run_id == run.id,
        )
    )
    if (
        action is None
        or action.kind != "merge"
        or action.status != "completed"
        or (action.result or {}).get("merge_sha") != payload.sha
    ):
        raise HTTPException(
            status_code=409,
            detail="QA result is not tied to the completed merge action",
        )
    if run.status == "deployed" and run.qa_ready_sha == payload.sha:
        return AgentRunOut.model_validate(run)
    if payload.status == "failed" and run.qa_deploy_dispatch_status == "dispatched":
        return AgentRunOut.model_validate(run)
    if payload.status == "dispatched":
        run.qa_deploy_dispatch_status = "dispatched"
        run.qa_deploy_dispatch_error = None
        run.qa_deploy_dispatched_at = run.qa_deploy_dispatched_at or datetime.now(UTC)
        agent_events.record(session, run, phase="qa_deploy")
    else:
        run.qa_deploy_dispatch_status = "failed"
        run.qa_deploy_dispatch_error = (
            payload.message or "QA deployment workflow dispatch failed"
        )
        agent_events.record(
            session,
            run,
            status="qa_dispatch_failed",
            phase="qa_deploy",
            error=run.qa_deploy_dispatch_error,
        )
    await session.commit()
    return AgentRunOut.model_validate(run)


@router.post(
    "/{run_id}/qa-deployment-authorization",
    response_model=AgentQaDeploymentAuthorizationOut,
)
async def authorize_qa_deployment(
    run_id: str,
    payload: AgentQaDeploymentAuthorization,
    session: DbSession,
    x_agent_qa_callback_token: str | None = Header(default=None),
) -> AgentQaDeploymentAuthorizationOut:
    """Authorize only the exact QA merge dispatched by an owner release action."""
    _qa_deploy_auth(x_agent_qa_callback_token)
    await _lock_qa_project_for_run(session, run_id)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == run_id).with_for_update()
    )
    if run is None or run.repo_full_name != settings.agent_qa_repository:
        raise HTTPException(status_code=404, detail="QA agent run not found")
    if (
        run.status != "merged"
        or run.merged_sha != payload.merge_sha
        or run.qa_deploy_dispatch_status != "dispatched"
    ):
        raise HTTPException(
            status_code=409,
            detail="QA deployment was not authorized by the owner merge",
        )
    action = await session.scalar(
        select(AgentRunAction).where(
            AgentRunAction.action_id == str(payload.action_id),
            AgentRunAction.agent_run_id == run.id,
        )
    )
    if (
        action is None
        or action.kind != "merge"
        or action.status != "completed"
        or action.request_data.get("expected_head_sha") != payload.expected_head_sha
        or (action.result or {}).get("head_sha") != payload.expected_head_sha
        or (action.result or {}).get("merge_sha") != payload.merge_sha
    ):
        raise HTTPException(
            status_code=409,
            detail="QA deployment does not match the owner merge action",
        )
    current_head = await _verify_deployment(run, payload.merge_sha)
    if current_head != payload.expected_head_sha:
        raise HTTPException(
            status_code=409,
            detail="QA PR head differs from the owner-approved head",
        )
    if await _has_newer_qa_owner_merge(session, run):
        raise HTTPException(
            status_code=409,
            detail="QA deployment was superseded by a newer owner merge",
        )
    return AgentQaDeploymentAuthorizationOut(
        authorized=True,
        run_id=run.run_id,
        repo_full_name=run.repo_full_name,
        expected_head_sha=payload.expected_head_sha,
        merge_sha=payload.merge_sha,
    )


@router.post("/{run_id}/callback", response_model=AgentRunOut)
async def agent_run_callback(
    run_id: str,
    payload: AgentRunCallback,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
    x_agent_lease_id: str | None = Header(default=None),
) -> AgentRunOut:
    if (
        not settings.agent_callback_token
        or not x_agent_callback_token
        or not hmac.compare_digest(x_agent_callback_token, settings.agent_callback_token)
    ):
        raise HTTPException(status_code=401, detail="invalid callback token")
    if run_id != payload.run_id:
        raise HTTPException(status_code=400, detail="run ID mismatch")
    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.run_id == run_id)
        .options(selectinload(AgentRun.task).selectinload(Task.project))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    recovering_preflight = run.status == "failed" and payload.status == "pr_opened"
    if recovering_preflight:
        if (
            run.error != "Trusted PR preflight failed; inspect the publisher job."
            or run.pr_url is not None
            or run.task.deleted_at is not None
            or run.task.status in {TaskStatus.DONE, TaskStatus.CANCELLED}
            or run.task_revision != _revision(run.task, run.mode)
        ):
            raise HTTPException(status_code=409, detail="failed run cannot publish a PR")
        newer = await session.scalar(
            select(AgentRun.id).where(
                AgentRun.task_id == run.task_id,
                AgentRun.id > run.id,
            )
        )
        if newer is not None:
            raise HTTPException(status_code=409, detail="a newer agent attempt exists")
    elif run.status in {
        "pr_opened",
        "pr_ready",
        "merged",
        "deployed",
        "cancelled",
        "failed",
        "ops_pending",
        "ops_applied",
    }:
        # An idempotent replay of an already-finished status; never require a
        # lease that a prior terminal callback already cleared, and never
        # re-store proposals a first callback already inserted.
        return AgentRunOut.model_validate(run)

    if run.executor == "local":
        _require_live_lease(run, x_agent_lease_id, kind="implement")

    # Ops proposals are trusted from the local executor only, inside its own
    # live implement lease — a GitHub-executor run (whose callback token is
    # also held by GitHub Actions) can never attach or claim them.
    if payload.ops_requests and run.executor != "local":
        raise HTTPException(status_code=409, detail="ops_requests require the local executor")
    if payload.status == "ops_pending":
        if run.executor != "local":
            raise HTTPException(
                status_code=409, detail="ops_pending requires the local executor"
            )
        if not agent_ops.has_eligible_proposal(payload.ops_requests):
            raise HTTPException(
                status_code=400,
                detail="ops_pending requires at least one eligible ops request",
            )

    if payload.status in {"validating", "publishing", "deploying"}:
        if not payload.head_sha:
            raise HTTPException(status_code=400, detail="fast branch SHA is required")
        if payload.status == "validating" and run.status not in {
            "running",
            "validating",
            "publishing",
        }:
            raise HTTPException(status_code=409, detail="run cannot start fast validation")
        if payload.status == "publishing" and run.status != "validating":
            raise HTTPException(status_code=409, detail="fast run has not passed validation")
        if payload.status == "deploying" and run.status != "publishing":
            raise HTTPException(status_code=409, detail="fast run has not been validated")
        await _verify_fast_branch(run, payload.head_sha)
        run.head_sha = payload.head_sha

    if payload.status == "pr_opened":
        if not payload.pr_url or not payload.head_sha or not settings.github_agent_token:
            raise HTTPException(status_code=400, detail="PR URL and SHA are required")
        prefix = f"https://github.com/{run.repo_full_name}/pull/"
        if not payload.pr_url.startswith(prefix):
            raise HTTPException(status_code=400, detail="PR belongs to a different repository")
        pr_number = payload.pr_url.removeprefix(prefix)
        if not pr_number.isdecimal():
            raise HTTPException(status_code=400, detail="invalid PR URL")
        await _verify_pr(run, pr_number, payload.head_sha)
        if recovering_preflight:
            await _verify_recovered_commit(run, payload.head_sha)
        run.pr_url = payload.pr_url
        run.head_sha = payload.head_sha
        run.ci_status = "pending"
        run.ci_verified_sha = None
        run.ci_url = None
        if recovering_preflight:
            run.notified_at = None  # Edit the earlier failed card, never send a second one.

    if (
        run.executor == "local"
        and payload.ops_requests
        and payload.status in {"pr_opened", "ops_pending", "failed"}
    ):
        # Only inside this live-lease implement transition, and only once —
        # the idempotent-replay early return above means a run never reaches
        # here a second time in this same status. A `failed` implement has
        # nothing left to apply, so any otherwise-eligible proposal is
        # stored `cancelled` rather than `proposed` — no dead approve button
        # on a run that already ended.
        project_key = run.task.project.key if run.task.project else ""
        await agent_ops.store_proposals(
            session,
            run,
            payload.ops_requests,
            project_key=project_key,
            run_failed=payload.status == "failed",
        )
    if run.executor == "local" and payload.ops_note is not None:
        run.ops_note = payload.ops_note

    changed = run.status != payload.status or (
        payload.status == "failed" and run.error != payload.error
    )
    run.status = payload.status
    if payload.status == "pr_opened" and run.pr_opened_at is None:
        run.pr_opened_at = datetime.now(UTC)
    if payload.github_run_url:
        expected_url = f"https://github.com/{DISPATCH_REPOSITORY}/actions/runs/"
        suffix = payload.github_run_url.removeprefix(expected_url)
        if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
            raise HTTPException(status_code=400, detail="invalid GitHub run URL")
        run.github_run_url = payload.github_run_url
    run.error = payload.error
    if payload.status == "running":
        _mark_running(session, run, changed, github_run_url=payload.github_run_url)
    elif changed:
        agent_events.record(
            session,
            run,
            error=(
                "Kod yozish yoki tekshiruv bosqichi xato bilan tugadi"
                if payload.status == "failed" and payload.failure_phase == "implement"
                else (
                    "PR nashr bosqichi xato bilan tugadi"
                    if payload.status == "failed" and payload.failure_phase == "publish"
                    else (
                        "Agent ishi xato bilan tugadi" if payload.status == "failed" else None
                    )
                )
            ),
            phase=payload.failure_phase if payload.status == "failed" else None,
            github_run_url=payload.github_run_url,
        )
    if run.executor == "local" and payload.status in {"pr_opened", "failed", "ops_pending"}:
        # Implement lease done: a review is leased separately once CI is
        # green, or the run is terminal (or waiting on an owner ops decision)
        # and needs no further implement lease at all.
        _clear_lease(run)
    if payload.input_tokens is not None:
        run.input_tokens = payload.input_tokens
    if payload.cached_input_tokens is not None:
        run.cached_input_tokens = payload.cached_input_tokens
    if payload.output_tokens is not None:
        run.output_tokens = payload.output_tokens
    if payload.status in {"pr_opened", "failed", "ops_pending"}:
        run.finished_at = datetime.now(UTC)
    if payload.status == "failed" and can_transition(
        TaskStatus(run.task.status), TaskStatus.BLOCKED
    ):
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
                "agent_run_id": run_id,
            },
        )
    await session.commit()
    return AgentRunOut.model_validate(run)
