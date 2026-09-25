"""Bot-only task drafts and a leased, read-only Codex intake queue."""

import hmac
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.api.v1.tasks import _load as load_task
from app.core.config import settings
from app.db.enums import ActivityKind, TaskSource, TaskStatus
from app.db.models import AgentIntake, Attachment, Project, Task, User
from app.schemas.agent_intake import (
    IntakeConfirm,
    IntakeConfirmedOut,
    IntakeCreate,
    IntakeNotificationOut,
    IntakeNotified,
    IntakeOut,
    IntakeResult,
    IntakeText,
    IntakeWorkOut,
)
from app.schemas.task import TaskOut
from app.services import activity
from app.services.access import can_see_project, is_manager
from app.services.agent_repos import repository_for
from app.services.telegram_media import telegram_image

router = APIRouter(prefix="/agent-intakes", tags=["agent-intakes"])
_ACTIVE = ("queued", "analyzing", "needs_answers", "ready", "failed")
_FINISHED = ("confirmed", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def _bot_auth(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, settings.service_token):
        raise HTTPException(status_code=401, detail="bot authentication required")


def _worker_auth(token: str | None) -> None:
    if (
        not settings.intake_worker_token
        or not token
        or not hmac.compare_digest(token, settings.intake_worker_token)
    ):
        raise HTTPException(status_code=401, detail="intake worker authentication required")


async def _owned(session: DbSession, intake_id: int, user: User) -> AgentIntake:
    row = await session.scalar(
        select(AgentIntake).where(AgentIntake.id == intake_id).with_for_update()
    )
    if (
        row is None
        or row.user_id != user.id
        or (row.status != "confirmed" and row.expires_at <= _now())
    ):
        raise HTTPException(status_code=404, detail="intake not found")
    return row


def _check_capability(user: User, mode: str) -> None:
    if not (
        user.can_use_codex
        or (settings.owner_telegram_id and user.telegram_id == settings.owner_telegram_id)
    ):
        raise HTTPException(status_code=403, detail="Codex access is not enabled")
    if mode == "fast" and user.telegram_id != settings.owner_telegram_id:
        raise HTTPException(status_code=403, detail="fast mode is owner-only")


def _as_out(row: AgentIntake) -> IntakeOut:
    return IntakeOut.model_validate(row)


@router.post("", response_model=IntakeOut, status_code=status.HTTP_201_CREATED)
async def create_intake(
    payload: IntakeCreate,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    if not settings.agent_intake_enabled:
        raise HTTPException(status_code=503, detail="Codex intake is not enabled")
    _check_capability(user, payload.mode)
    if payload.chat_id != user.telegram_id:
        raise HTTPException(status_code=403, detail="intake works only in a private chat")
    if not await can_see_project(session, user, payload.project_id):
        raise HTTPException(status_code=404, detail="project not found")
    project = await session.get(Project, payload.project_id)
    repository = (
        repository_for(project.key, project.repo_full_name, project.default_branch)
        if project is not None
        else None
    )
    if repository is None:
        raise HTTPException(
            status_code=409, detail="project repository is not enabled for Codex"
        )
    if not repository.private and not settings.agent_public_enabled:
        raise HTTPException(status_code=503, detail="public project agents are not enabled")
    if payload.mode == "fast" and not repository.fast_enabled:
        raise HTTPException(status_code=409, detail="fast mode is limited to Task Manager")
    assert project is not None
    # The user lock makes two Telegram messages arriving together choose one draft.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = await session.scalar(
        select(AgentIntake).where(
            AgentIntake.user_id == user.id,
            AgentIntake.chat_id == payload.chat_id,
            AgentIntake.status.in_(_ACTIVE),
            AgentIntake.expires_at > _now(),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"intake #{existing.id} is still active")
    row = AgentIntake(
        user_id=user.id,
        project_id=project.id,
        chat_id=payload.chat_id,
        text=payload.text.strip(),
        mode=payload.mode,
        status="queued",
        images=[image.model_dump() for image in payload.images],
        questions=[],
        brief={},
        expires_at=_now() + timedelta(hours=24),
        attempts=0,
        retry_count=0,
        analysis_rounds=0,
        revision=1,
        notified_revision=0,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _as_out(row)


@router.get("/current", response_model=IntakeOut)
async def current_intake(
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut | Response:
    _bot_auth(x_service_token)
    row = await session.scalar(
        select(AgentIntake)
        .where(
            AgentIntake.user_id == user.id,
            AgentIntake.chat_id == user.telegram_id,
            AgentIntake.status.in_(_ACTIVE),
            AgentIntake.expires_at > _now(),
        )
        .order_by(AgentIntake.id.desc())
    )
    return _as_out(row) if row is not None else Response(status_code=204)


@router.post("/lease", response_model=IntakeWorkOut)
async def lease_intake(
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
) -> IntakeWorkOut | Response:
    _worker_auth(x_intake_worker_token)
    if not settings.agent_intake_enabled:
        return Response(status_code=204)
    now = _now()
    exhausted = await session.scalars(
        select(AgentIntake)
        .where(
            AgentIntake.status == "analyzing",
            AgentIntake.lease_until < now,
            AgentIntake.attempts >= 2,
            AgentIntake.expires_at > now,
        )
        .with_for_update(skip_locked=True)
        .limit(20)
    )
    for expired in exhausted:
        expired.status = "failed"
        expired.error = "Codex intake worker timed out"
        expired.lease_until = None
        expired.lease_id = None
        expired.revision += 1
    await session.flush()
    row = await session.scalar(
        select(AgentIntake)
        .where(
            AgentIntake.expires_at > now,
            AgentIntake.attempts < 2,
            or_(
                AgentIntake.status == "queued",
                (AgentIntake.status == "analyzing") & (AgentIntake.lease_until < now),
            ),
        )
        .order_by(AgentIntake.created_at, AgentIntake.id)
        .with_for_update(skip_locked=True)
        .limit(1)
        .options(selectinload(AgentIntake.project))
    )
    if row is None:
        return Response(status_code=204)
    row.status = "analyzing"
    row.lease_until = now + timedelta(minutes=2)
    row.lease_id = str(uuid4())
    row.attempts += 1
    await session.commit()
    assert row.project.repo_full_name and row.project.default_branch and row.lease_id
    return IntakeWorkOut(
        id=row.id,
        revision=row.revision,
        lease_id=row.lease_id,
        text=row.text,
        answer_text=row.answer_text,
        mode=row.mode,
        images=row.images,
        repo_full_name=row.project.repo_full_name,
        base_branch=row.project.default_branch,
        analysis_rounds=row.analysis_rounds,
    )


@router.post("/{intake_id}/result", response_model=IntakeOut)
async def report_intake(
    intake_id: int,
    payload: IntakeResult,
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _worker_auth(x_intake_worker_token)
    row = await session.scalar(
        select(AgentIntake).where(AgentIntake.id == intake_id).with_for_update()
    )
    if (
        row is None
        or row.status != "analyzing"
        or row.expires_at <= _now()
        or row.lease_until is None
        or row.lease_until <= _now()
        or row.lease_id != payload.lease_id
        or row.revision != payload.revision
    ):
        raise HTTPException(status_code=409, detail="intake lease is no longer current")
    if payload.status == "needs_answers" and row.answer_text is not None:
        raise HTTPException(status_code=409, detail="the clarification round is complete")
    row.status = payload.status
    row.questions = payload.questions
    row.brief = payload.brief.model_dump() if payload.brief else {}
    row.error = payload.error
    row.lease_until = None
    row.lease_id = None
    row.analysis_rounds += 1
    row.revision += 1
    await session.commit()
    return _as_out(row)


@router.get("/notifications", response_model=list[IntakeNotificationOut])
async def pending_notifications(
    session: DbSession,
    x_agent_worker_token: Annotated[str | None, Header()] = None,
) -> list[IntakeNotificationOut]:
    _bot_auth(x_agent_worker_token)
    rows = await session.scalars(
        select(AgentIntake)
        .where(
            AgentIntake.status.in_(["needs_answers", "ready", "failed"]),
            AgentIntake.notified_revision < AgentIntake.revision,
            AgentIntake.expires_at > _now(),
        )
        .order_by(AgentIntake.id)
        .limit(20)
    )
    return [
        IntakeNotificationOut(
            id=row.id,
            revision=row.revision,
            status=row.status,
            chat_id=row.chat_id,
            title=str(row.brief.get("title") or row.text[:120]),
            mode=row.mode,
            questions=row.questions,
            brief=row.brief,
            error=row.error,
        )
        for row in rows
    ]


@router.post("/{intake_id}/notified", status_code=204)
async def mark_notified(
    intake_id: int,
    payload: IntakeNotified,
    session: DbSession,
    x_agent_worker_token: Annotated[str | None, Header()] = None,
) -> None:
    _bot_auth(x_agent_worker_token)
    row = await session.scalar(
        select(AgentIntake).where(AgentIntake.id == intake_id).with_for_update()
    )
    if row is None or row.revision != payload.revision:
        raise HTTPException(status_code=409, detail="intake notice is stale")
    row.notified_revision = payload.revision
    row.bot_message_id = payload.message_id
    await session.commit()


@router.get("/{intake_id}/images/{index}")
async def intake_image(
    intake_id: int,
    index: int,
    session: DbSession,
    x_intake_worker_token: Annotated[str | None, Header()] = None,
    x_intake_lease_id: Annotated[str | None, Header()] = None,
) -> Response:
    _worker_auth(x_intake_worker_token)
    row = await session.get(AgentIntake, intake_id)
    if (
        row is None
        or row.status != "analyzing"
        or row.lease_id != x_intake_lease_id
        or row.lease_until is None
        or row.lease_until <= _now()
        or index < 0
        or index >= len(row.images)
    ):
        raise HTTPException(status_code=404, detail="image not found")
    image = row.images[index]
    data = await telegram_image(image["file_id"], image["mime"], image.get("size"))
    return Response(content=data, media_type=image["mime"])


@router.get("/{intake_id}", response_model=IntakeOut)
async def get_intake(
    intake_id: int,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    return _as_out(await _owned(session, intake_id, user))


@router.post("/{intake_id}/answer", response_model=IntakeOut)
async def answer_intake(
    intake_id: int,
    payload: IntakeText,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    row = await _owned(session, intake_id, user)
    if row.status != "needs_answers":
        raise HTTPException(status_code=409, detail="intake is not awaiting an answer")
    row.answer_text = payload.text.strip()
    row.status = "queued"
    row.error = None
    row.attempts = 0
    row.revision += 1
    await session.commit()
    return _as_out(row)


@router.post("/{intake_id}/revise", response_model=IntakeOut)
async def revise_intake(
    intake_id: int,
    payload: IntakeText,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    row = await _owned(session, intake_id, user)
    if row.status not in {"needs_answers", "ready", "failed"}:
        raise HTTPException(status_code=409, detail="intake cannot be revised now")
    row.text = payload.text.strip()
    row.answer_text = None
    row.questions = []
    row.brief = {}
    row.error = None
    row.status = "queued"
    row.attempts = 0
    row.analysis_rounds = 0
    row.revision += 1
    await session.commit()
    return _as_out(row)


@router.post("/{intake_id}/retry", response_model=IntakeOut)
async def retry_intake(
    intake_id: int,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    row = await _owned(session, intake_id, user)
    if row.status != "failed":
        raise HTTPException(status_code=409, detail="intake is not failed")
    if row.retry_count >= 1:
        raise HTTPException(status_code=409, detail="intake retry limit reached")
    row.status = "queued"
    row.error = None
    row.attempts = 0
    row.retry_count += 1
    row.revision += 1
    await session.commit()
    return _as_out(row)


@router.post("/{intake_id}/cancel", response_model=IntakeOut)
async def cancel_intake(
    intake_id: int,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeOut:
    _bot_auth(x_service_token)
    row = await _owned(session, intake_id, user)
    if row.status not in _FINISHED:
        row.status = "cancelled"
        row.revision += 1
        await session.commit()
    return _as_out(row)


def _description(row: AgentIntake, fallback: bool) -> tuple[str, str]:
    if fallback:
        return row.text[:255], row.text[:12000]
    brief = row.brief
    title = str(brief["title"])[:255]
    pieces = [str(brief["goal"]), "", "Qabul mezonlari:"]
    pieces.extend(f"- {item}" for item in brief["acceptance"])
    if brief.get("assumptions"):
        pieces.extend(["", "Taxminlar:"])
        pieces.extend(f"- {item}" for item in brief["assumptions"])
    # A discussion-derived request contains a transcript, including earlier
    # assistant replies and pasted error cards. The user approved the concise
    # brief above; duplicating that transcript made Telegram cards too long and
    # accidentally promoted unverified old answers into new requirements.
    if not row.text.startswith("@codex Quyidagi loyiha suhbatidagi"):
        pieces.extend(["", f"Asl so‘rov: {row.text[:3000]}"])
    if row.answer_text:
        pieces.append(f"Javoblar: {row.answer_text}")
    return title, "\n".join(pieces)[:12000]


@router.post("/{intake_id}/confirm", response_model=IntakeConfirmedOut)
async def confirm_intake(
    intake_id: int,
    payload: IntakeConfirm,
    session: DbSession,
    user: CurrentUser,
    x_service_token: Annotated[str | None, Header()] = None,
) -> IntakeConfirmedOut:
    _bot_auth(x_service_token)
    row = await _owned(session, intake_id, user)
    if row.status == "confirmed" and row.task_id is not None:
        task = await load_task(session, row.task_id)
        return IntakeConfirmedOut(
            task=TaskOut.model_validate(task),
            mode=row.confirmed_mode or row.mode,
            created=False,
        )
    if row.status != ("failed" if payload.fallback_pr else "ready"):
        raise HTTPException(status_code=409, detail="intake is not ready for confirmation")
    mode = "pr" if payload.fallback_pr else row.mode
    title, description = _description(row, payload.fallback_pr)
    task = Task(
        project_id=row.project_id,
        title=title,
        description=description,
        status=TaskStatus.TODO,
        source=TaskSource.BOT,
        source_chat_id=row.chat_id,
        created_by_id=user.id,
        assignee_id=None if is_manager(user) else user.id,
        position=time.time(),
    )
    session.add(task)
    await session.flush()
    for image in row.images:
        session.add(
            Attachment(
                task_id=task.id,
                tg_file_id=image["file_id"],
                mime=image["mime"],
                size=image.get("size"),
            )
        )
    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.CREATED,
        payload={"title": task.title, "agent_intake_id": row.id, "images": len(row.images)},
    )
    row.task_id = task.id
    row.confirmed_mode = mode
    row.status = "confirmed"
    row.revision += 1
    await session.commit()
    loaded = await load_task(session, task.id)
    return IntakeConfirmedOut(task=TaskOut.model_validate(loaded), mode=mode, created=True)
