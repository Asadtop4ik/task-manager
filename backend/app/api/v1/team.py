"""Invite-only registration and the owner's Codex access grants."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession, OwnerUser
from app.core.config import settings
from app.db.enums import UserRole
from app.db.models import JoinRequest, Membership, Project, TeamInvite, User
from app.schemas.team import (
    CodexAccessUpdate,
    InviteOut,
    JoinDecision,
    JoinRequestCreate,
    JoinRequestOut,
)
from app.schemas.user import UserOut

router = APIRouter(prefix="/team", tags=["team"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _owner_chat_id() -> int:
    if not settings.owner_telegram_id:
        raise HTTPException(status_code=503, detail="owner Telegram ID is not configured")
    return settings.owner_telegram_id


def _bot_auth(token: str | None, acting_user: int | None) -> int:
    if not token or not hmac.compare_digest(token, settings.service_token) or not acting_user:
        raise HTTPException(status_code=401, detail="bot authentication required")
    return acting_user


def _as_out(row: JoinRequest, *, notify: bool = False) -> JoinRequestOut:
    name = " ".join(part for part in (row.first_name, row.last_name) if part)
    return JoinRequestOut(
        id=row.id,
        telegram_id=row.telegram_id,
        full_name=name,
        username=row.username,
        status=row.status,
        notify_owner=notify,
        owner_chat_id=_owner_chat_id() if notify else None,
    )


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def create_invite(session: DbSession, owner: OwnerUser) -> InviteOut:
    if not settings.bot_username:
        raise HTTPException(status_code=503, detail="bot username is not configured")
    token = secrets.token_urlsafe(24)
    expires_at = datetime.now(UTC) + timedelta(days=7)
    session.add(
        TeamInvite(token_hash=_hash(token), created_by_id=owner.id, expires_at=expires_at)
    )
    await session.commit()
    return InviteOut(
        url=f"https://t.me/{settings.bot_username}?start=invite_{token}",
        expires_at=expires_at,
    )


@router.post("/join-requests", response_model=JoinRequestOut)
async def request_to_join(
    payload: JoinRequestCreate,
    session: DbSession,
    x_service_token: Annotated[str | None, Header()] = None,
    x_acting_user: Annotated[int | None, Header()] = None,
) -> JoinRequestOut:
    actor_id = _bot_auth(x_service_token, x_acting_user)
    _owner_chat_id()  # Do not consume an invitation before notifications can work.
    if actor_id != payload.telegram_id:
        raise HTTPException(status_code=403, detail="Telegram user mismatch")
    existing_user = await session.scalar(
        select(User).where(User.telegram_id == payload.telegram_id)
    )
    if existing_user is not None and existing_user.is_active:
        return JoinRequestOut(
            id=None,
            telegram_id=actor_id,
            full_name=existing_user.full_name,
            username=existing_user.username,
            status="active",
        )
    invite = await session.scalar(
        select(TeamInvite)
        .where(TeamInvite.token_hash == _hash(payload.invite_token))
        .with_for_update()
    )
    if invite is None or invite.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="invite expired or invalid")
    if invite.used_at is not None:
        if invite.used_by_telegram_id == actor_id:
            prior = await session.scalar(
                select(JoinRequest).where(JoinRequest.invite_id == invite.id)
            )
            if prior is not None:
                return _as_out(prior)
        raise HTTPException(status_code=410, detail="invite already used")
    prior_pending = await session.scalar(
        select(JoinRequest).where(
            JoinRequest.telegram_id == actor_id, JoinRequest.status == "pending"
        )
    )
    if prior_pending is not None:
        return _as_out(prior_pending)
    invite.used_at = datetime.now(UTC)
    invite.used_by_telegram_id = actor_id
    row = JoinRequest(
        invite_id=invite.id,
        telegram_id=actor_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        username=payload.username,
        status="pending",
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        prior = await session.scalar(
            select(JoinRequest).where(
                JoinRequest.telegram_id == actor_id, JoinRequest.status == "pending"
            )
        )
        if prior is not None:
            return _as_out(prior)
        raise
    await session.refresh(row)
    return _as_out(row, notify=True)


@router.get("/join-requests/pending", response_model=list[JoinRequestOut])
async def pending_requests(session: DbSession, owner: OwnerUser) -> list[JoinRequestOut]:
    rows = await session.scalars(
        select(JoinRequest)
        .where(JoinRequest.status == "pending")
        .order_by(JoinRequest.created_at)
    )
    return [_as_out(row) for row in rows]


@router.post("/join-requests/{request_id}/approve", response_model=JoinRequestOut)
async def approve_request(
    request_id: int, payload: JoinDecision, session: DbSession, owner: OwnerUser
) -> JoinRequestOut:
    row = await session.scalar(
        select(JoinRequest).where(JoinRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="join request not found")
    if row.status != "pending":
        return _as_out(row)
    project_ids = set(payload.project_ids)
    projects = (
        await session.scalars(select(Project).where(Project.id.in_(project_ids)))
    ).all()
    if len(projects) != len(project_ids):
        raise HTTPException(status_code=404, detail="project not found")
    user = await session.scalar(select(User).where(User.telegram_id == row.telegram_id))
    if user is None:
        user = User(
            telegram_id=row.telegram_id,
            full_name=" ".join(part for part in (row.first_name, row.last_name) if part),
            username=row.username,
            role=UserRole.EXECUTOR,
            is_active=True,
            can_use_codex=False,
        )
        session.add(user)
        await session.flush()
    else:
        if user.telegram_id == settings.owner_telegram_id:
            raise HTTPException(status_code=409, detail="owner cannot join as a teammate")
        user.is_active = True
        user.full_name = " ".join(part for part in (row.first_name, row.last_name) if part)
        user.username = row.username
        # A new invite replaces old project and Codex grants on reactivation.
        user.role = UserRole.EXECUTOR
        user.can_use_codex = False
    memberships = (
        await session.scalars(select(Membership).where(Membership.user_id == user.id))
    ).all()
    existing = {membership.project_id for membership in memberships}
    for membership in memberships:
        if membership.project_id not in project_ids:
            await session.delete(membership)
    for project_id in project_ids - existing:
        session.add(
            Membership(
                user_id=user.id, project_id=project_id, role_in_project=UserRole.EXECUTOR
            )
        )
    row.status = "approved"
    row.decided_at = datetime.now(UTC)
    row.decided_by_id = owner.id
    await session.commit()
    return _as_out(row)


@router.post("/join-requests/{request_id}/reject", response_model=JoinRequestOut)
async def reject_request(
    request_id: int, session: DbSession, owner: OwnerUser
) -> JoinRequestOut:
    row = await session.scalar(
        select(JoinRequest).where(JoinRequest.id == request_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="join request not found")
    if row.status != "pending":
        return _as_out(row)
    row.status = "rejected"
    row.decided_at = datetime.now(UTC)
    row.decided_by_id = owner.id
    await session.commit()
    return _as_out(row)


@router.put("/members/{user_id}/codex-access", response_model=UserOut)
async def set_codex_access(
    user_id: int, payload: CodexAccessUpdate, session: DbSession, owner: OwnerUser
) -> UserOut:
    # Locking the owner serializes concurrent grants so only one other person
    # can hold the second Codex seat.
    await session.scalar(select(User).where(User.id == owner.id).with_for_update())
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.telegram_id == settings.owner_telegram_id:
        raise HTTPException(status_code=409, detail="owner Codex access cannot be changed")
    if payload.enabled and not target.is_active:
        raise HTTPException(status_code=409, detail="user is not active")
    other = await session.scalar(
        select(User).where(
            User.can_use_codex.is_(True),
            User.is_active.is_(True),
            User.telegram_id != settings.owner_telegram_id,
            User.id != user_id,
        )
    )
    if payload.enabled and other is not None:
        raise HTTPException(status_code=409, detail="second Codex seat is already assigned")
    target.can_use_codex = payload.enabled
    await session.commit()
    await session.refresh(target)
    return UserOut.model_validate(target)
