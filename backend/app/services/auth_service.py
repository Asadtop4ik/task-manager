from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import UserRole
from app.db.models import Membership, Project, User

log = get_logger(__name__)


def _full_name(data: dict[str, Any]) -> str:
    parts = [
        str(data.get("first_name") or "").strip(),
        str(data.get("last_name") or "").strip(),
    ]
    name = " ".join(part for part in parts if part)
    return name or str(data.get("username") or f"tg:{data['id']}")


async def resolve_user(session: AsyncSession, telegram_data: dict[str, Any]) -> User:
    """Find or create the user behind a verified Telegram payload.

    A first-time telegram_id is created INACTIVE and waits for a manager to
    approve it. An open Telegram login on a public domain is otherwise an open
    door: anyone who finds the page gets an account.

    The exception is ADMIN_TELEGRAM_IDS, which bootstraps the first manager —
    without one, nobody could ever approve anybody.
    """
    telegram_id = int(telegram_data["id"])
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))

    is_bootstrap_admin = telegram_id in settings.admin_ids

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=telegram_data.get("username"),
            full_name=_full_name(telegram_data),
            role=UserRole.MANAGER if is_bootstrap_admin else UserRole.EXECUTOR,
            is_active=is_bootstrap_admin,
            lang=str(telegram_data.get("language_code") or settings.default_language)[:8],
            tz=settings.timezone,
        )
        session.add(user)
        await session.flush()
        log.info(
            "user_created",
            user_id=user.id,
            telegram_id=telegram_id,
            active=user.is_active,
        )
    else:
        # Names and usernames change in Telegram; keep ours current.
        user.username = telegram_data.get("username")
        user.full_name = _full_name(telegram_data)

    if is_bootstrap_admin:
        # Listing an id in ADMIN_TELEGRAM_IDS is also the way back in if someone
        # deactivates themselves, so re-assert it on every login.
        user.role = UserRole.MANAGER
        user.is_active = True
        await _join_every_project(session, user)

    return user


async def _join_every_project(session: AsyncSession, user: User) -> None:
    project_ids = set((await session.scalars(select(Project.id))).all())
    existing = set(
        (
            await session.scalars(
                select(Membership.project_id).where(Membership.user_id == user.id)
            )
        ).all()
    )
    for project_id in project_ids - existing:
        session.add(
            Membership(
                user_id=user.id, project_id=project_id, role_in_project=UserRole.MANAGER
            )
        )
