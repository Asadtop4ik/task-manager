"""Start one coding run for a task and record verified GitHub results."""

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from math import ceil
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, OwnerUser
from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import ActivityKind, TaskStatus, can_transition
from app.db.models import (
    AgentEvent,
    AgentIntake,
    AgentRun,
    Attachment,
    ProjectDiscussion,
    Task,
)
from app.schemas.agent_run import (
    AgentCiPending,
    AgentCiResult,
    AgentDeployment,
    AgentEventOut,
    AgentImageOut,
    AgentMerge,
    AgentMetricsOut,
    AgentNoticeAck,
    AgentNotificationOut,
    AgentRunCallback,
    AgentRunOut,
    AgentRunStart,
    ExternalAgentPending,
    MetricDuration,
)
from app.services import activity, agent_events
from app.services.access import can_edit_task, can_see_task
from app.services.agent_repos import (
    DISPATCH_REPOSITORY,
    PUBLIC_REPOSITORIES,
    REPOSITORIES,
    repository_for,
)
from app.services.telegram_media import IMAGE_MIMES, telegram_image

log = get_logger(__name__)
router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])
_GITHUB = "https://api.github.com"
_METRICS_PILOT_START = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


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


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_agent_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _ensure_repo_visibility(
    client: httpx.AsyncClient, repo: str, *, private: bool
) -> None:
    response = await client.get(f"{_GITHUB}/repos/{repo}", headers=_headers())
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub repository access failed")
    if response.json().get("private") is not private:
        raise HTTPException(
            status_code=409,
            detail="repository visibility differs from the approved agent policy",
        )


async def _dispatch(run: AgentRun, task: Task) -> int:
    repository = repository_for(task.project.key, run.repo_full_name, run.base_branch)
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
                "event_type": ("agent_task" if repository.private else "agent_public_task"),
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
            headers=_headers(),
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


async def _verify_pr_ci(run: AgentRun, sha: str, conclusion: str, url: str) -> None:
    """Prove a completed CI run belongs to this PR head and passed required jobs."""
    prefix = f"https://github.com/{run.repo_full_name}/actions/runs/"
    run_number = url.removeprefix(prefix)
    if not url.startswith(prefix) or not run_number.isdecimal():
        raise HTTPException(status_code=400, detail="invalid PR CI run URL")
    target = next(
        (
            item
            for item in REPOSITORIES
            if item.full_name == run.repo_full_name and item.branch == run.base_branch
        ),
        None,
    )
    if target is None or not target.pr_ci_jobs:
        raise HTTPException(status_code=409, detail="PR CI policy is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_GITHUB}/repos/{run.repo_full_name}/actions/runs/{run_number}",
            headers=_headers(),
        )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="PR CI verification failed")
        workflow = response.json()
        if (
            workflow.get("head_sha") != sha
            or workflow.get("head_branch") != _run_branch(run)
            or workflow.get("event") != "pull_request"
            or workflow.get("path") != ".github/workflows/ci.yml"
            or workflow.get("status") != "completed"
        ):
            raise HTTPException(status_code=409, detail="PR CI does not match this commit")
        passed = workflow.get("conclusion") == "success"
        if passed:
            jobs_response = await client.get(
                f"{_GITHUB}/repos/{run.repo_full_name}/actions/runs/{run_number}/jobs?per_page=100",
                headers=_headers(),
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
            headers=_headers(),
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
            f"{_GITHUB}/repos/{run.repo_full_name}/commits/{sha}", headers=_headers()
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


@router.get("/notifications", response_model=list[AgentNotificationOut])
async def pending_notifications(
    session: DbSession, x_agent_worker_token: str | None = Header(default=None)
) -> list[AgentNotificationOut]:
    _worker_auth(x_agent_worker_token)
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
                    AgentRun.status.in_(["merged", "failed", "deployed"]),
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
        if run.status == "pr_ready":
            # A reviewer can push a new commit between the CI monitor tick and
            # the bot tick. Never announce the previously verified head.
            if not run.pr_url or not run.ci_verified_sha:
                continue
            pr_number = run.pr_url.rsplit("/", 1)[-1]
            if not pr_number.isdecimal():
                continue
            try:
                await _verify_pr(run, pr_number, run.ci_verified_sha)
            except HTTPException:
                continue  # Fail closed; the CI monitor will reconcile next tick.
        verified_runs.append(run)
    return [
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
        )
        for run in verified_runs
    ]


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
                input_tokens=run.input_tokens if run and run.status == event.status else None,
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
    if run.status not in {"pr_opened", "pr_ready", "merged", "failed", "deployed"}:
        raise HTTPException(status_code=409, detail="run is not finished")
    if run.status == "pr_opened" and run.telegram_message_id is None:
        raise HTTPException(status_code=409, detail="no earlier card to update")
    if run.status == "pr_ready" and (
        run.ci_status != "success" or run.ci_verified_sha != run.head_sha
    ):
        raise HTTPException(status_code=409, detail="PR CI is not verified")
    if run.notified_at is None:
        if payload is not None and payload.message_id is not None:
            run.telegram_message_id = payload.message_id
        run.notified_at = datetime.now(UTC)
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

    unchanged = (
        run.status == ("pr_ready" if payload.conclusion == "success" else "pr_opened")
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
    run.notified_at = None if run.telegram_message_id is not None else run.notified_at
    if payload.conclusion == "success":
        run.status = "pr_ready"
        run.pr_ready_at = datetime.now(UTC)
        run.notified_at = None
        agent_events.record(session, run, phase="ci", github_run_url=run.ci_url)
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


@router.get("/{run_id}/status", response_model=AgentRunOut)
async def agent_run_status(
    run_id: str, session: DbSession, x_agent_callback_token: str | None = Header(default=None)
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
    task_id: int, session: DbSession, user: CurrentUser, payload: AgentRunStart | None = None
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
    repository = repository_for(task.project.key, repo, branch)
    if repository is not None and not repository.private and not settings.agent_public_enabled:
        raise HTTPException(status_code=503, detail="public project agents are not enabled")
    if mode == "fast" and (repository is None or not repository.fast_enabled):
        raise HTTPException(status_code=409, detail="fast mode is limited to Task Manager")
    if mode == "fast" and branch != "main":
        raise HTTPException(status_code=409, detail="fast mode requires the main branch")
    allowlist = {
        name.strip().lower() for name in settings.github_agent_allowed_repos.split(",")
    }
    if repository is None or repo is None or repo.lower() not in allowlist:
        raise HTTPException(
            status_code=409, detail="project repository is not enabled for agents"
        )
    if not settings.github_agent_token or not settings.agent_callback_token:
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
        select(AgentRun).where(AgentRun.run_id == run_id).options(selectinload(AgentRun.task))
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
    }:
        raise HTTPException(status_code=409, detail="agent run is already finished")
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
                payload={"from": old, "to": TaskStatus.TODO.value, "agent_run_id": run_id},
            )
    else:
        await _cancel_github(run)
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


@router.post("/{run_id}/callback", response_model=AgentRunOut)
async def agent_run_callback(
    run_id: str,
    payload: AgentRunCallback,
    session: DbSession,
    x_agent_callback_token: str | None = Header(default=None),
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
        select(AgentRun).where(AgentRun.run_id == run_id).options(selectinload(AgentRun.task))
    )
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in {"pr_opened", "pr_ready", "merged", "deployed", "cancelled", "failed"}:
        return AgentRunOut.model_validate(run)

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
        run.pr_url = payload.pr_url
        run.head_sha = payload.head_sha
        run.ci_status = "pending"
        run.ci_verified_sha = None
        run.ci_url = None

    changed = run.status != payload.status or (
        payload.status == "failed" and run.error != payload.error
    )
    run.status = payload.status
    if payload.status == "running" and run.runner_started_at is None:
        run.runner_started_at = datetime.now(UTC)
    if payload.status == "pr_opened" and run.pr_opened_at is None:
        run.pr_opened_at = datetime.now(UTC)
    if payload.github_run_url:
        expected_url = f"https://github.com/{DISPATCH_REPOSITORY}/actions/runs/"
        suffix = payload.github_run_url.removeprefix(expected_url)
        if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
            raise HTTPException(status_code=400, detail="invalid GitHub run URL")
        run.github_run_url = payload.github_run_url
    run.error = payload.error
    if changed:
        agent_events.record(
            session,
            run,
            error=(
                "Kod yozish yoki tekshiruv bosqichi xato bilan tugadi"
                if payload.status == "failed" and payload.failure_phase == "implement"
                else (
                    "PR nashr bosqichi xato bilan tugadi"
                    if payload.status == "failed" and payload.failure_phase == "publish"
                    else "Agent ishi xato bilan tugadi" if payload.status == "failed" else None
                )
            ),
            phase=payload.failure_phase if payload.status == "failed" else None,
            github_run_url=payload.github_run_url,
        )
    if payload.input_tokens is not None:
        run.input_tokens = payload.input_tokens
    if payload.cached_input_tokens is not None:
        run.cached_input_tokens = payload.cached_input_tokens
    if payload.output_tokens is not None:
        run.output_tokens = payload.output_tokens
    if payload.status in {"pr_opened", "failed"}:
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
            payload={"from": old, "to": TaskStatus.BLOCKED.value, "agent_run_id": run_id},
        )
    if payload.status == "running" and can_transition(
        TaskStatus(run.task.status), TaskStatus.IN_PROGRESS
    ):
        old = run.task.status
        run.task.status = TaskStatus.IN_PROGRESS
        run.task.started_at = run.task.started_at or datetime.now(UTC)
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={"from": old, "to": TaskStatus.IN_PROGRESS.value, "agent_run_id": run_id},
        )
    await session.commit()
    return AgentRunOut.model_validate(run)
