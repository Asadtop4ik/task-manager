from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models import Membership, Task, User


def is_manager(user: User) -> bool:
    return user.role == UserRole.MANAGER


async def visible_project_ids(session: AsyncSession, user: User) -> set[int] | None:
    """Projects this user may see. `None` means "all of them" (managers).

    Returning None rather than every id keeps the caller from building an
    IN clause over the whole table for the common case.
    """
    if is_manager(user):
        return None
    rows = await session.scalars(
        select(Membership.project_id).where(Membership.user_id == user.id)
    )
    return set(rows.all())


async def can_see_project(session: AsyncSession, user: User, project_id: int) -> bool:
    allowed = await visible_project_ids(session, user)
    return allowed is None or project_id in allowed


async def can_see_task(session: AsyncSession, user: User, task: Task) -> bool:
    return await can_see_project(session, user, task.project_id)


async def can_edit_task(session: AsyncSession, user: User, task: Task) -> bool:
    """Who may change a task's content.

    Managers, anywhere. An executor only on tasks in their own projects — the
    plan gives them status, comments and time, and letting them retitle or
    re-scope a task the manager wrote is not that.
    """
    if is_manager(user):
        return True
    return task.assignee_id == user.id and await can_see_task(session, user, task)
