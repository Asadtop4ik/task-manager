from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.task import Task
    from app.db.models.user import User


class Comment(Base, TimestampMixin):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text, nullable=False)

    task: Mapped["Task"] = relationship(back_populates="comments")
    author: Mapped["User | None"] = relationship()


class Attachment(Base, TimestampMixin):
    """A pointer into Telegram, not a copy of the bytes.

    Storing `tg_file_id` and streaming on demand keeps this stack out of the backup
    rotation entirely — kans-shop's media volume exists only because it had to.
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tg_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)

    task: Mapped["Task"] = relationship(back_populates="attachments")


class Activity(Base):
    """Audit log and notification source in one table.

    Every mutation writes a row here; the notifier reads rows rather than being
    called from each handler, so "who gets a Telegram message" lives in one place
    instead of being re-decided in every endpoint.
    """

    __tablename__ = "activity"
    __table_args__ = (Index("ix_activity_task_created", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # No index=True: ix_activity_task_created already leads with task_id.
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Only delete/restore events use this as a durable Telegram card outbox.
    card_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped["Task"] = relationship(back_populates="activity")
    actor: Mapped["User | None"] = relationship()


class Reminder(Base):
    """Durable mirror of a scheduled arq job.

    arq owns the timing; this table owns the answer to "was this person already
    told?". Without it a worker restart re-sends every reminder it had queued.
    """

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_pending", "fire_at", "sent_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # arq's job id, so a rescheduled reminder can cancel the one it replaces.
    job_id: Mapped[str | None] = mapped_column(String(64))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
