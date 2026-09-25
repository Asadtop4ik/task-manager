"""Private, resumable project conversations backed by a tokenless Codex worker."""

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Response
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.api.v1.agent_intakes import _bot_auth, _check_capability, _worker_auth
from app.core.config import settings
from app.db.models import Project, ProjectDiscussion, User
from app.schemas.project_discussion import (
    DiscussionMessage,
    DiscussionNotice,
    DiscussionOut,
    DiscussionResult,
    DiscussionStart,
    DiscussionWork,
)
from app.services import agent_events
from app.services.access import can_see_project
from app.services.agent_repos import repository_for
from app.services.telegram_media import telegram_image

router = APIRouter(prefix="/project-discussions", tags=["project-discussions"])


def _now() -> datetime:
    return datetime.now(UTC)


def _is_qa_project(project: Project | None) -> bool:
    return bool(
        project
        and (
            project.key == "agent-qa" or project.repo_full_name == settings.agent_qa_repository
        )
    )


def _is_qa_owner(user: User | None) -> bool:
    return bool(
        user
        and settings.owner_telegram_id > 0
        and user.telegram_id == settings.owner_telegram_id
    )


def _qa_project_allowed(project: Project | None, user: User | None) -> bool:
    return bool(
        settings.agent_qa_enabled
        and _is_qa_owner(user)
        and project is not None
        and repository_for(
            project.key,
            project.repo_full_name,
            project.default_branch,
            include_qa=True,
        )
    )


async def _owned(session: DbSession, discussion_id: int, user: User) -> ProjectDiscussion:
    row = await session.scalar(
        select(ProjectDiscussion)
        .where(ProjectDiscussion.id == discussion_id)
        .with_for_update()
    )
    if row is None or row.user_id != user.id or row.chat_id != user.telegram_id:
        raise HTTPException(status_code=404, detail="discussion not found")
    if not await can_see_project(session, user, row.project_id):
        raise HTTPException(status_code=404, detail="project not found")
    project = await session.get(Project, row.project_id)
    if _is_qa_project(project) and not _qa_project_allowed(project, user):
        raise HTTPException(status_code=404, detail="discussion not found")
    _check_capability(user, "pr")
    return row


@router.post("", response_model=DiscussionOut)
async def start_discussion(
    payload: DiscussionStart,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _bot_auth(x_service_token)
    _check_capability(user, "pr")
    if not settings.agent_intake_enabled:
        raise HTTPException(status_code=503, detail="Codex discussions are not enabled")
    if payload.chat_id != user.telegram_id:
        raise HTTPException(status_code=403, detail="private chat required")
    if not await can_see_project(session, user, payload.project_id):
        raise HTTPException(status_code=404, detail="project not found")
    project = await session.get(Project, payload.project_id)
    if _is_qa_project(project) and not _is_qa_owner(user):
        raise HTTPException(status_code=403, detail="agent-qa is owner-only")
    repository = (
        repository_for(
            project.key,
            project.repo_full_name,
            project.default_branch,
            include_qa=settings.agent_qa_enabled,
        )
        if project is not None
        else None
    )
    if repository is None or (not repository.private and not settings.agent_public_enabled):
        raise HTTPException(status_code=409, detail="project is not enabled for Codex")
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    row = await session.scalar(
        select(ProjectDiscussion).where(
            ProjectDiscussion.user_id == user.id,
            ProjectDiscussion.project_id == payload.project_id,
        )
    )
    if row is None:
        row = ProjectDiscussion(
            user_id=user.id,
            project_id=payload.project_id,
            chat_id=payload.chat_id,
            status="idle",
            messages=[],
            pending_images=[],
            revision=0,
            notified_revision=0,
        )
        session.add(row)
        await session.flush()
        agent_events.record(session, row)
        await session.commit()
        await session.refresh(row)
    return DiscussionOut.model_validate(row)


@router.get("/{discussion_id:int}", response_model=DiscussionOut)
async def get_discussion(
    discussion_id: int,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _bot_auth(x_service_token)
    return DiscussionOut.model_validate(await _owned(session, discussion_id, user))


@router.post("/{discussion_id:int}/messages", response_model=DiscussionOut)
async def send_message(
    discussion_id: int,
    payload: DiscussionMessage,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _bot_auth(x_service_token)
    if not settings.agent_intake_enabled:
        raise HTTPException(status_code=503, detail="Codex discussions are not enabled")
    row = await _owned(session, discussion_id, user)
    text = payload.text.strip()
    if not text and not payload.images:
        raise HTTPException(status_code=422, detail="send text or a picture")
    if row.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="previous answer is still pending")
    row.messages = [
        *row.messages[-99:],
        {
            "role": "user",
            "text": text,
            "images": [image.model_dump() for image in payload.images],
        },
    ]
    row.pending_text = text
    row.pending_images = [image.model_dump() for image in payload.images]
    row.response_text = None
    row.error = None
    row.status = "queued"
    agent_events.record(session, row)
    row.revision += 1
    await session.commit()
    await session.refresh(row)
    return DiscussionOut.model_validate(row)


@router.post("/{discussion_id:int}/reset", response_model=DiscussionOut)
async def reset_discussion(
    discussion_id: int,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _bot_auth(x_service_token)
    row = await _owned(session, discussion_id, user)
    if row.status in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="wait for the current answer")
    row.thread_id = None
    row.messages = []
    row.pending_text = None
    row.pending_images = []
    row.response_text = None
    row.error = None
    row.status = "idle"
    agent_events.record(session, row, status="reset")
    row.revision += 1
    row.notified_revision = row.revision
    await session.commit()
    await session.refresh(row)
    return DiscussionOut.model_validate(row)


@router.post("/lease", response_model=DiscussionWork)
async def lease_discussion(
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
) -> DiscussionWork | Response:
    _worker_auth(x_intake_worker_token)
    if not settings.agent_intake_enabled:
        return Response(status_code=204)
    now = _now()
    stale = await session.scalar(
        select(ProjectDiscussion)
        .where(ProjectDiscussion.status == "running", ProjectDiscussion.lease_until < now)
        .order_by(ProjectDiscussion.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if stale is not None:
        stale.status = "failed"
        stale.error = "Suhbat javobi vaqtida kelmadi. Xabarni qayta yuboring."
        agent_events.record(session, stale, error=stale.error)
        stale.lease_id = None
        stale.lease_until = None
        await session.commit()
    row = await session.scalar(
        select(ProjectDiscussion)
        .where(ProjectDiscussion.status == "queued")
        .order_by(ProjectDiscussion.updated_at, ProjectDiscussion.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return Response(status_code=204)
    project = await session.get(Project, row.project_id)
    actor = await session.get(User, row.user_id)
    repository = (
        repository_for(
            project.key,
            project.repo_full_name,
            project.default_branch,
            include_qa=settings.agent_qa_enabled,
        )
        if project is not None
        else None
    )
    allowed_actor = bool(
        actor
        and actor.is_active
        and (actor.can_use_codex or actor.telegram_id == settings.owner_telegram_id)
    )
    if actor is not None and allowed_actor:
        allowed_actor = await can_see_project(session, actor, row.project_id)
    if (
        repository is None
        or not allowed_actor
        or (_is_qa_project(project) and not _qa_project_allowed(project, actor))
        or (not repository.private and not settings.agent_public_enabled)
    ):
        row.status = "failed"
        row.error = "Loyiha yoki Codex huquqi hozir mavjud emas."
        row.lease_id = None
        row.lease_until = None
        row.revision += 1
        agent_events.record(session, row, error=row.error)
        await session.commit()
        return Response(status_code=204)
    project_key = project.key if project is not None else ""
    row.status = "running"
    agent_events.record(session, row)
    row.lease_id = str(uuid4())
    row.lease_until = now + timedelta(minutes=5)
    await session.commit()
    return DiscussionWork(
        id=row.id,
        revision=row.revision,
        lease_id=row.lease_id,
        repo_full_name=repository.full_name,
        base_branch=repository.branch,
        project_key=project_key,
        diagnostics_enabled=bool(
            settings.ketoshop_diagnostics_enabled
            and project_key == "ketoshop"
            and actor is not None
            and actor.telegram_id == settings.owner_telegram_id
            and settings.owner_telegram_id > 0
        ),
        thread_id=row.thread_id,
        text=row.pending_text or "",
        images=row.pending_images,
    )


@router.get("/{discussion_id:int}/diagnostic-context")
async def diagnostic_context(
    discussion_id: int,
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
    x_intake_lease_id: Annotated[str | None, Header()] = None,
) -> dict[str, bool | str | int]:
    """Re-check owner, project and unguessable active lease before diagnostics."""
    _worker_auth(x_intake_worker_token)
    row = await session.get(ProjectDiscussion, discussion_id)
    if (
        row is None
        or row.status != "running"
        or not row.lease_id
        or not x_intake_lease_id
        or not x_intake_lease_id.isascii()
        or not hmac.compare_digest(row.lease_id, x_intake_lease_id)
        or row.lease_until is None
        or row.lease_until <= _now()
    ):
        raise HTTPException(status_code=404, detail="diagnostics are not available")
    project = await session.get(Project, row.project_id)
    actor = await session.get(User, row.user_id)
    if actor is None or project is None:
        raise HTTPException(status_code=404, detail="diagnostics are not available")
    allowed = bool(
        settings.ketoshop_diagnostics_enabled
        and settings.owner_telegram_id > 0
        and project.key == "ketoshop"
        and actor.is_active
        and actor.telegram_id == settings.owner_telegram_id
    )
    if not allowed or not await can_see_project(session, actor, row.project_id):
        raise HTTPException(status_code=404, detail="diagnostics are not available")
    return {
        "authorized": True,
        "project_key": "ketoshop",
        "project_id": project.id,
        "actor_id": actor.id,
        "active": True,
        "revision": row.revision,
    }


async def _leased(
    session: DbSession,
    discussion_id: int,
    lease_id: str | None,
) -> ProjectDiscussion:
    row = await session.scalar(
        select(ProjectDiscussion)
        .where(ProjectDiscussion.id == discussion_id)
        .with_for_update()
    )
    if (
        row is None
        or row.status != "running"
        or not lease_id
        or not row.lease_id
        or not hmac.compare_digest(row.lease_id, lease_id)
        or row.lease_until is None
        or row.lease_until <= _now()
    ):
        raise HTTPException(status_code=409, detail="discussion lease expired")
    project = await session.get(Project, row.project_id)
    actor = await session.get(User, row.user_id)
    if _is_qa_project(project) and not _qa_project_allowed(project, actor):
        row.status = "failed"
        row.error = "agent-qa is no longer enabled for this owner."
        row.lease_id = None
        row.lease_until = None
        row.revision += 1
        agent_events.record(session, row, error=row.error)
        await session.commit()
        raise HTTPException(status_code=409, detail="QA discussion is no longer authorized")
    return row


@router.get("/{discussion_id:int}/images/{index:int}")
async def discussion_image(
    discussion_id: int,
    index: int,
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
    x_intake_lease_id: Annotated[str | None, Header()] = None,
) -> Response:
    _worker_auth(x_intake_worker_token)
    row = await _leased(session, discussion_id, x_intake_lease_id)
    if index < 0 or index >= len(row.pending_images):
        raise HTTPException(status_code=404, detail="image not found")
    image = row.pending_images[index]
    data = await telegram_image(image["file_id"], image["mime"], image.get("size"))
    return Response(data, media_type=image["mime"])


@router.post("/{discussion_id:int}/result", response_model=DiscussionOut)
async def discussion_result(
    discussion_id: int,
    payload: DiscussionResult,
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _worker_auth(x_intake_worker_token)
    row = await _leased(session, discussion_id, payload.lease_id)
    if payload.revision != row.revision:
        raise HTTPException(status_code=409, detail="stale discussion result")
    if bool(payload.response) == bool(payload.error):
        raise HTTPException(status_code=422, detail="response or error required")
    if payload.response and not payload.thread_id:
        raise HTTPException(status_code=422, detail="Codex thread is required")
    row.lease_id = None
    row.lease_until = None
    if payload.error:
        row.status = "failed"
        row.error = payload.error
        agent_events.record(session, row, error=row.error)
    else:
        row.status = "idle"
        agent_events.record(session, row, status="answered")
        row.thread_id = payload.thread_id
        row.response_text = payload.response
        row.error = None
        row.messages = [*row.messages[-99:], {"role": "assistant", "text": payload.response}]
        row.pending_text = None
        row.pending_images = []
    await session.commit()
    await session.refresh(row)
    return DiscussionOut.model_validate(row)


@router.get("/notifications", response_model=list[DiscussionNotice])
async def discussion_notifications(
    session: DbSession,
    x_agent_worker_token: Annotated[str | None, Header()] = None,
) -> list[DiscussionNotice]:
    _bot_auth(x_agent_worker_token)
    rows = (
        await session.scalars(
            select(ProjectDiscussion)
            .where(
                ProjectDiscussion.status.in_(["idle", "failed"]),
                ProjectDiscussion.revision > ProjectDiscussion.notified_revision,
            )
            .order_by(ProjectDiscussion.updated_at)
            .limit(50)
        )
    ).all()
    return [
        DiscussionNotice(
            id=row.id,
            revision=row.revision,
            chat_id=row.chat_id,
            status=row.status,
            response=row.response_text,
            error=row.error,
        )
        for row in rows
    ]


@router.post("/{discussion_id:int}/notified", response_model=DiscussionOut)
async def discussion_notified(
    discussion_id: int,
    revision: int,
    session: DbSession,
    x_agent_worker_token: Annotated[str | None, Header()] = None,
) -> DiscussionOut:
    _bot_auth(x_agent_worker_token)
    row = await session.scalar(
        select(ProjectDiscussion)
        .where(ProjectDiscussion.id == discussion_id)
        .with_for_update()
    )
    if row is None or row.revision != revision or row.status not in {"idle", "failed"}:
        raise HTTPException(status_code=409, detail="stale discussion notice")
    row.notified_revision = revision
    await session.commit()
    await session.refresh(row)
    return DiscussionOut.model_validate(row)
