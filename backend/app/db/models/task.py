from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import TaskPriority, TaskSource, TaskStatus

if TYPE_CHECKING:
    from app.db.models.misc import Activity, Attachment, Comment
    from app.db.models.project import Project
    from app.db.models.user import User

_STATUSES = ", ".join(f"'{s}'" for s in TaskStatus)
_PRIORITIES = ", ".join(f"'{p}'" for p in TaskPriority)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="ck_tasks_status"),
        CheckConstraint(f"priority IN ({_PRIORITIES})", name="ck_tasks_priority"),
        CheckConstraint("spent_minutes >= 0", name="ck_tasks_spent_nonneg"),
        # The board's default query: one project's open tasks, newest first.
        Index("ix_tasks_project_status", "project_id", "status"),
        # Reading one column in hand-ordered sequence.
        Index("ix_tasks_status_position", "status", "position"),
        # "My day": what is on one person's plate and when it is due.
        Index("ix_tasks_assignee_due", "assignee_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # No index=True on project_id/assignee_id: the composite indexes above lead
    # with each of them, and Postgres uses a leading-column prefix happily.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.TODO, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(16), default=TaskPriority.NORMAL, nullable=False
    )

    # Unassigned is a real state (the manager captured it before deciding who).
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    # Everything is stored UTC and rendered in the viewer's tz.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    spent_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Hand-ordering within a board column. A float, not an integer rank, so
    # dropping a card between two others is one UPDATE of one row rather than
    # renumbering everything below it.
    position: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    source: Mapped[str] = mapped_column(String(8), default=TaskSource.WEB, nullable=False)
    # Where the task came from in Telegram. Keeping these lets the bot EDIT the
    # original card when the status changes, instead of posting a second message.
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)

    project: Mapped["Project"] = relationship(back_populates="tasks")
    assignee: Mapped["User | None"] = relationship(foreign_keys=[assignee_id])
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    activity: Mapped[list["Activity"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status} {self.title[:30]!r}>"
