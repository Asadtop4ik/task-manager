from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, ManagerUser
from app.core.logging import get_logger
from app.db.models import User
from app.schemas.user import UserOut, UserUpdate

log = get_logger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(session: DbSession, user: CurrentUser) -> list[UserOut]:
    """Everyone active, so an assignee picker has names to show."""
    rows = await session.scalars(select(User).where(User.is_active).order_by(User.full_name))
    return [UserOut.model_validate(row) for row in rows]


@router.get("/pending", response_model=list[UserOut])
async def list_pending(session: DbSession, manager: ManagerUser) -> list[UserOut]:
    """The approval queue: anyone who logged in via Telegram and is still waiting."""
    rows = await session.scalars(
        select(User).where(User.is_active.is_(False)).order_by(User.created_at)
    )
    return [UserOut.model_validate(row) for row in rows]


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, payload: UserUpdate, session: DbSession, manager: ManagerUser
) -> UserOut:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    if target.id == manager.id and payload.is_active is False:
        # Deactivating yourself as the only manager locks everyone out of the
        # approval queue, so refuse the obvious version of it.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="you cannot deactivate yourself"
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(target, field, value)
    await session.commit()
    await session.refresh(target)
    log.info("user_updated", user_id=target.id, by=manager.id)
    return UserOut.model_validate(target)
