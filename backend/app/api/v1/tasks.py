import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.core.logging import get_logger
from app.db.enums import OPEN_STATUSES, ActivityKind, TaskPriority, TaskStatus, can_transition
from app.db.models import Activity, Comment, Task, User
from app.schemas.activity import ActivityOut
from app.schemas.comment import CommentCreate, CommentOut
from app.schemas.task import (
    TaskAssign,
    TaskCard,
    TaskCreate,
    TaskListResponse,
    TaskOut,
    TaskReorder,
    TaskTransition,
    TaskUpdate,
    TimeLog,
)
from app.services import activity
from app.services.access import can_edit_task, can_see_project, is_manager, visible_project_ids

log = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Every read returns the same shape, so load the relationships the schema needs
# in one go rather than letting each row lazy-load three more queries.
_RELATIONS = (
    selectinload(Task.project),
    selectinload(Task.assignee),
    selectinload(Task.created_by),
)


async def _load(session: DbSession, task_id: int) -> Task:
    task = await session.scalar(select(Task).where(Task.id == task_id).options(*_RELATIONS))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task


async def _visible_or_404(session: DbSession, user: User, task: Task) -> Task:
    """404, not 403, for a task in a project you cannot see.

    Telling someone "this exists but is not yours" leaks the id space and how
    busy other projects are.
    """
    if not await can_see_project(session, user, task.project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    session: DbSession,
    user: CurrentUser,
    project_id: int | None = None,
    status_in: Annotated[list[TaskStatus] | None, Query(alias="status")] = None,
    assignee_id: int | None = None,
    priority: TaskPriority | None = None,
    open_only: bool = False,
    overdue: bool = False,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskListResponse:
    query = select(Task)
    count_query = select(func.count()).select_from(Task)

    allowed = await visible_project_ids(session, user)
    conditions: list[ColumnElement[bool]] = []
    if allowed is not None:
        conditions.append(Task.project_id.in_(allowed))
    if project_id is not None:
        conditions.append(Task.project_id == project_id)
    if status_in:
        conditions.append(Task.status.in_([s.value for s in status_in]))
    if open_only:
        conditions.append(Task.status.in_([s.value for s in OPEN_STATUSES]))
    if assignee_id is not None:
        conditions.append(Task.assignee_id == assignee_id)
    if priority is not None:
        conditions.append(Task.priority == priority.value)
    if overdue:
        conditions.append(Task.due_at < datetime.now(UTC))
        conditions.append(Task.status.in_([s.value for s in OPEN_STATUSES]))
    if q:
        pattern = f"%{q}%"
        conditions.append(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))

    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = await session.scalar(count_query) or 0
    rows = await session.scalars(
        query.options(*_RELATIONS)
        # Nulls last so undated work does not push the deadline out of view.
        .order_by(Task.due_at.asc().nulls_last(), Task.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return TaskListResponse(
        items=[TaskOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: DbSession, user: CurrentUser) -> TaskOut:
    if not await can_see_project(session, user, payload.project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    if (
        payload.assignee_id is not None
        and payload.assignee_id != user.id
        and not is_manager(user)
    ):
        # An executor can capture their own work; handing it to someone else is
        # the manager's call.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="only a manager can assign to others"
        )

    # New work lands at the bottom of its column. time() is monotonic enough for
    # an ordering key and needs no extra query to find the current maximum.
    task = Task(**payload.model_dump(), created_by_id=user.id, position=time.time())
    session.add(task)
    await session.flush()

    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.CREATED,
        payload={"title": task.title, "assignee_id": task.assignee_id},
    )
    await session.commit()
    log.info("task_created", task_id=task.id, project_id=task.project_id, by=user.id)
    return TaskOut.model_validate(await _load(session, task.id))


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, session: DbSession, user: CurrentUser) -> TaskOut:
    task = await _visible_or_404(session, user, await _load(session, task_id))
    return TaskOut.model_validate(task)


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int, payload: TaskUpdate, session: DbSession, user: CurrentUser
) -> TaskOut:
    task = await _visible_or_404(session, user, await _load(session, task_id))
    if not await can_edit_task(session, user, task):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not allowed to edit"
        )

    changes = payload.model_dump(exclude_unset=True)
    if "project_id" in changes and not is_manager(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="only a manager can move a task"
        )
    if "project_id" in changes and not await can_see_project(
        session, user, changes["project_id"]
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    if "priority" in changes and changes["priority"] != task.priority:
        activity.record(
            session,
            task_id=task.id,
            actor=user,
            kind=ActivityKind.PRIORITY_CHANGED,
            payload={"from": task.priority, "to": changes["priority"]},
        )
    if "due_at" in changes and changes["due_at"] != task.due_at:
        activity.record(
            session,
            task_id=task.id,
            actor=user,
            kind=ActivityKind.DUE_CHANGED,
            payload={
                "from": task.due_at.isoformat() if task.due_at else None,
                "to": changes["due_at"].isoformat() if changes["due_at"] else None,
            },
        )

    for field, value in changes.items():
        setattr(task, field, value)
    await session.commit()
    return TaskOut.model_validate(await _load(session, task.id))


@router.post("/{task_id}/transition", response_model=TaskOut)
async def transition_task(
    task_id: int, payload: TaskTransition, session: DbSession, user: CurrentUser
) -> TaskOut:
    """Move a task through the status table in app/db/enums.py.

    The rules live there because the board, the bot's inline buttons and this
    endpoint all have to agree; a stale Telegram card must not be able to reopen
    a task that was closed last week.
    """
    task = await _visible_or_404(session, user, await _load(session, task_id))

    current = TaskStatus(task.status)
    target = payload.status
    if current == target:
        return TaskOut.model_validate(task)
    if not can_transition(current, target):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot move a task from {current} to {target}",
        )
    if not is_manager(user) and task.assignee_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the assignee or a manager can move this",
        )

    now = datetime.now(UTC)
    task.status = target
    if target == TaskStatus.IN_PROGRESS and task.started_at is None:
        task.started_at = now
    if target == TaskStatus.DONE:
        task.done_at = now
    elif current == TaskStatus.DONE:
        # Reopened: the old completion time is no longer true.
        task.done_at = None

    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.STATUS_CHANGED,
        payload={"from": current.value, "to": target.value},
    )
    await session.commit()
    log.info("task_transitioned", task_id=task.id, to=target.value, by=user.id)
    return TaskOut.model_validate(await _load(session, task.id))


@router.post("/{task_id}/assign", response_model=TaskOut)
async def assign_task(
    task_id: int, payload: TaskAssign, session: DbSession, user: CurrentUser
) -> TaskOut:
    task = await _visible_or_404(session, user, await _load(session, task_id))
    if not is_manager(user) and payload.assignee_id != user.id:
        # An executor may pick up unassigned work; handing it elsewhere is not theirs.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="only a manager can assign to others"
        )

    if payload.assignee_id is not None:
        assignee = await session.get(User, payload.assignee_id)
        if assignee is None or not assignee.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    previous = task.assignee_id
    task.assignee_id = payload.assignee_id
    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.ASSIGNED,
        payload={"from": previous, "to": payload.assignee_id},
    )
    await session.commit()
    return TaskOut.model_validate(await _load(session, task.id))


@router.post("/{task_id}/time", response_model=TaskOut)
async def log_time(
    task_id: int, payload: TimeLog, session: DbSession, user: CurrentUser
) -> TaskOut:
    task = await _visible_or_404(session, user, await _load(session, task_id))
    if not is_manager(user) and task.assignee_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="only the assignee can log time"
        )

    task.spent_minutes += payload.minutes
    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.TIME_LOGGED,
        payload={"minutes": payload.minutes, "total": task.spent_minutes},
    )
    await session.commit()
    return TaskOut.model_validate(await _load(session, task.id))


# The gap left between neighbours when a card is dropped at one end of a column.
# Large enough that the halving below takes a very long time to run out of room.
_ORDER_GAP = 1024.0


@router.post("/{task_id}/reorder", response_model=TaskOut)
async def reorder_task(
    task_id: int, payload: TaskReorder, session: DbSession, user: CurrentUser
) -> TaskOut:
    """Place a task between two others.

    Only the dragged row is written. Neighbours are re-read here rather than
    trusting a position the client computed, because the client's copy of the
    column may be seconds out of date.
    """
    task = await _visible_or_404(session, user, await _load(session, task_id))

    async def neighbour(other_id: int | None) -> Task | None:
        if other_id is None:
            return None
        row = await session.get(Task, other_id)
        if row is None or not await can_see_project(session, user, row.project_id):
            return None
        return row

    previous = await neighbour(payload.previous_id)
    following = await neighbour(payload.next_id)

    if previous is not None and following is not None:
        task.position = (previous.position + following.position) / 2
    elif previous is not None:
        task.position = previous.position + _ORDER_GAP
    elif following is not None:
        task.position = following.position - _ORDER_GAP
    else:
        # Dropped into an empty column: nothing to be relative to, and the
        # existing position is as good as any.
        return TaskOut.model_validate(task)

    await session.commit()
    return TaskOut.model_validate(await _load(session, task.id))


@router.post("/{task_id}/card", response_model=TaskOut)
async def set_card(
    task_id: int, payload: TaskCard, session: DbSession, user: CurrentUser
) -> TaskOut:
    """Remember which Telegram message is this task's card.

    Separate from PATCH because it is bookkeeping the bot does about its own
    messages, not a change to the task that anyone should see in the activity
    log.
    """
    task = await _visible_or_404(session, user, await _load(session, task_id))
    task.source_chat_id = payload.chat_id
    task.source_message_id = payload.message_id
    await session.commit()
    return TaskOut.model_validate(await _load(session, task.id))


@router.get("/{task_id}/comments", response_model=list[CommentOut])
async def list_comments(
    task_id: int, session: DbSession, user: CurrentUser
) -> list[CommentOut]:
    await _visible_or_404(session, user, await _load(session, task_id))
    rows = await session.scalars(
        select(Comment)
        .where(Comment.task_id == task_id)
        .options(selectinload(Comment.author))
        .order_by(Comment.created_at)
    )
    return [CommentOut.model_validate(row) for row in rows]


@router.post(
    "/{task_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED
)
async def add_comment(
    task_id: int, payload: CommentCreate, session: DbSession, user: CurrentUser
) -> CommentOut:
    task = await _visible_or_404(session, user, await _load(session, task_id))
    comment = Comment(task_id=task.id, author_id=user.id, body=payload.body)
    session.add(comment)
    await session.flush()

    activity.record(
        session,
        task_id=task.id,
        actor=user,
        kind=ActivityKind.COMMENTED,
        payload={"comment_id": comment.id},
    )
    await session.commit()
    await session.refresh(comment, ["author"])
    return CommentOut.model_validate(comment)


@router.get("/{task_id}/activity", response_model=list[ActivityOut])
async def list_activity(
    task_id: int, session: DbSession, user: CurrentUser
) -> list[ActivityOut]:
    await _visible_or_404(session, user, await _load(session, task_id))
    rows = await session.scalars(
        select(Activity)
        .where(Activity.task_id == task_id)
        .options(selectinload(Activity.actor))
        .order_by(Activity.created_at.desc())
    )
    return [ActivityOut.model_validate(row) for row in rows]
