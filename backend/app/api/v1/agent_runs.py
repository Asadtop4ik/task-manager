"""Start one coding run for a task and record verified GitHub results."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import ActivityKind, TaskStatus, can_transition
from app.db.models import AgentRun, Task
from app.schemas.agent_run import (
    AgentDeployment,
    AgentNotificationOut,
    AgentRunCallback,
    AgentRunOut,
)
from app.services import activity
from app.services.access import can_edit_task, can_see_task

log = get_logger(__name__)
router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])
_GITHUB = "https://api.github.com"


def _worker_auth(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, settings.service_token):
        raise HTTPException(status_code=401, detail="invalid worker token")


async def _task(session: DbSession, task_id: int) -> Task:
    task = await session.scalar(
        select(Task).where(Task.id == task_id).options(selectinload(Task.project))
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _revision(task: Task) -> str:
    payload = json.dumps(
        {
            "title": task.title,
            "description": task.description,
            "project_id": task.project_id,
            "repo": task.project.repo_full_name,
            "branch": task.project.default_branch,
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


async def _ensure_private_repo(client: httpx.AsyncClient, repo: str) -> None:
    response = await client.get(f"{_GITHUB}/repos/{repo}", headers=_headers())
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub repository access failed")
    if not response.json().get("private"):
        raise HTTPException(
            status_code=409,
            detail="subscription runner is limited to private repositories",
        )


async def _dispatch(run: AgentRun, task: Task) -> int:
    async with httpx.AsyncClient(timeout=15.0) as client:
        await _ensure_private_repo(client, run.repo_full_name)
        response = await client.post(
            f"{_GITHUB}/repos/{run.repo_full_name}/dispatches",
            headers=_headers(),
            json={
                "event_type": "agent_task",
                "client_payload": {
                    "run_id": run.run_id,
                    "task_id": task.id,
                    "title": task.title,
                    "description": task.description or "",
                    "base_branch": run.base_branch,
                    "task_revision": run.task_revision,
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
    expected_branch = f"codex/task-{run.task_id}-{run.run_id}"
    if (
        response.status_code != 200
        or response.json()["head"]["ref"] != expected_branch
        or response.json()["head"]["sha"] != sha
        or (response.json()["head"].get("repo") or {}).get("full_name", "").lower()
        != run.repo_full_name.lower()
    ):
        raise HTTPException(status_code=409, detail="PR does not match this agent run")


async def _verify_deployment(run: AgentRun, sha: str) -> None:
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
    if (
        not pr.get("merged")
        or pr.get("merge_commit_sha") != sha
        or pr["head"]["sha"] != run.head_sha
    ):
        raise HTTPException(status_code=409, detail="deployed SHA does not match merged PR")


@router.get("/notifications", response_model=list[AgentNotificationOut])
async def pending_notifications(
    session: DbSession, x_agent_worker_token: str | None = Header(default=None)
) -> list[AgentNotificationOut]:
    _worker_auth(x_agent_worker_token)
    runs = await session.scalars(
        select(AgentRun)
        .where(
            AgentRun.status.in_(["pr_ready", "failed", "deployed"]),
            AgentRun.notified_at.is_(None),
        )
        .options(selectinload(AgentRun.task).selectinload(Task.created_by))
        .order_by(AgentRun.id)
        .limit(20)
    )
    return [
        AgentNotificationOut(
            run_id=run.run_id,
            task_id=run.task_id,
            title=run.task.title,
            chat_id=run.task.source_chat_id
            or (run.task.created_by.telegram_id if run.task.created_by else None),
            status=run.status,
            pr_url=run.pr_url,
            github_run_url=run.github_run_url,
        )
        for run in runs
    ]


@router.post("/{run_id}/notified", status_code=204)
async def mark_notified(
    run_id: str,
    session: DbSession,
    x_agent_worker_token: str | None = Header(default=None),
) -> None:
    _worker_auth(x_agent_worker_token)
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status not in {"pr_ready", "failed", "deployed"}:
        raise HTTPException(status_code=409, detail="run is not finished")
    if run.notified_at is None:
        run.notified_at = datetime.now(UTC)
        await session.commit()


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


@router.post(
    "/tasks/{task_id}", response_model=AgentRunOut, status_code=status.HTTP_201_CREATED
)
async def start_agent_run(task_id: int, session: DbSession, user: CurrentUser) -> AgentRunOut:
    task = await _task(session, task_id)
    if not await can_edit_task(session, user, task):
        raise HTTPException(status_code=403, detail="not allowed to delegate this task")
    if task.status in {"done", "cancelled"}:
        raise HTTPException(status_code=409, detail="task is closed")
    repo = task.project.repo_full_name
    branch = task.project.default_branch
    allowlist = {
        name.strip().lower() for name in settings.github_agent_allowed_repos.split(",")
    }
    if not repo or not branch or repo.lower() not in allowlist:
        raise HTTPException(
            status_code=409, detail="project repository is not enabled for agents"
        )
    if not settings.github_agent_token or not settings.agent_callback_token:
        raise HTTPException(status_code=503, detail="agent integration is not configured")
    if len(task.description or "") > 12000:
        raise HTTPException(
            status_code=422, detail="task description is too long for an agent run"
        )

    revision = _revision(task)
    run = await session.scalar(
        select(AgentRun).where(AgentRun.task_id == task_id, AgentRun.task_revision == revision)
    )
    if run is not None and run.status != "pending":
        # A repeated Telegram callback must never make a second PR.
        return AgentRunOut.model_validate(run)
    if run is None:
        run = AgentRun(
            run_id=str(uuid4()),
            task_id=task_id,
            task_revision=revision,
            repo_full_name=repo,
            base_branch=branch,
            status="pending",
            attempts=0,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(AgentRun).where(
                    AgentRun.task_id == task_id, AgentRun.task_revision == revision
                )
            )
            if existing is None:
                raise
            return AgentRunOut.model_validate(existing)
        await session.refresh(run)
    if run.attempts >= 2:
        raise HTTPException(status_code=409, detail="agent dispatch retry limit reached")

    run.status = "dispatching"
    await session.commit()
    try:
        github_status = await _dispatch(run, task)
    except HTTPException as exc:
        run.status = "failed"
        run.error = str(exc.detail)
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise
    except httpx.RequestError as exc:
        run.status = "pending"
        run.attempts += 1
        await session.commit()
        log.error("agent_dispatch_network_failed", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=502, detail="GitHub dispatch failed") from exc
    run.attempts += 1
    if github_status != 204:
        run.status = "pending"
        await session.commit()
        log.error("agent_dispatch_rejected", task_id=task_id, github_status=github_status)
        raise HTTPException(status_code=502, detail="GitHub dispatch was rejected")
    await session.refresh(run)
    if run.status in {"dispatching", "pending"}:
        run.status = "dispatched"
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
    if run.status != "pr_ready":
        raise HTTPException(status_code=409, detail="agent PR is not ready")
    expected_url = f"https://github.com/{run.repo_full_name}/actions/runs/"
    suffix = payload.github_run_url.removeprefix(expected_url)
    if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
        raise HTTPException(status_code=400, detail="invalid deploy run URL")
    await _verify_deployment(run, payload.sha)
    run.status = "deployed"
    run.deployed_sha = payload.sha
    run.github_run_url = payload.github_run_url
    run.finished_at = datetime.now(UTC)
    run.notified_at = None
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
    if run.status in {"pr_ready", "deployed", "cancelled"}:
        return AgentRunOut.model_validate(run)

    if payload.status == "pr_ready":
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

    run.status = payload.status
    if payload.github_run_url:
        expected_url = f"https://github.com/{run.repo_full_name}/actions/runs/"
        suffix = payload.github_run_url.removeprefix(expected_url)
        if not payload.github_run_url.startswith(expected_url) or not suffix.isdecimal():
            raise HTTPException(status_code=400, detail="invalid GitHub run URL")
        run.github_run_url = payload.github_run_url
    run.error = payload.error
    if payload.status in {"pr_ready", "failed"}:
        run.finished_at = datetime.now(UTC)
    if payload.status == "pr_ready" and can_transition(
        TaskStatus(run.task.status), TaskStatus.REVIEW
    ):
        old = run.task.status
        run.task.status = TaskStatus.REVIEW
        activity.record(
            session,
            task_id=run.task_id,
            actor=None,
            kind=ActivityKind.STATUS_CHANGED,
            payload={
                "from": old,
                "to": TaskStatus.REVIEW.value,
                "agent_run_id": run_id,
            },
        )
    await session.commit()
    return AgentRunOut.model_validate(run)
