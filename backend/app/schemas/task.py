from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.enums import TaskPriority, TaskSource, TaskStatus
from app.schemas.project import ProjectOut
from app.schemas.user import UserOut


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: ProjectOut
    title: str
    description: str | None
    status: TaskStatus
    priority: TaskPriority
    assignee: UserOut | None
    created_by: UserOut | None
    due_at: datetime | None
    started_at: datetime | None
    done_at: datetime | None
    estimate_minutes: int | None
    spent_minutes: int
    source: TaskSource
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.TODO
    assignee_id: int | None = None
    due_at: datetime | None = None
    estimate_minutes: int | None = Field(default=None, ge=0)
    # Set by the bot so a later status change can edit the original card instead
    # of posting a second message into the chat.
    source: TaskSource = TaskSource.WEB
    source_chat_id: int | None = None
    source_message_id: int | None = None

    @model_validator(mode="after")
    def _no_creating_terminal_tasks(self) -> "TaskCreate":
        if self.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            raise ValueError("a new task cannot start out done or cancelled")
        return self


class TaskUpdate(BaseModel):
    """Everything except status, which goes through /transition and its rules."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    priority: TaskPriority | None = None
    project_id: int | None = None
    due_at: datetime | None = None
    estimate_minutes: int | None = Field(default=None, ge=0)


class TaskTransition(BaseModel):
    status: TaskStatus


class TaskAssign(BaseModel):
    # None unassigns; the manager captured it before deciding who does it.
    assignee_id: int | None = None


class TimeLog(BaseModel):
    minutes: int = Field(gt=0, le=24 * 60)


class TaskListResponse(BaseModel):
    items: list[TaskOut]
    total: int
    limit: int
    offset: int


class TaskCard(BaseModel):
    """Where this task's card lives in Telegram.

    Recorded after the bot sends it, so a later status change edits that message
    instead of posting a second card into the chat.
    """

    chat_id: int
    message_id: int
