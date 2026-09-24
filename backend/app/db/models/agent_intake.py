"""A short, durable conversation before an agent task is created."""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User


class AgentIntake(Base, TimestampMixin):
    __tablename__ = "agent_intakes"
    __table_args__ = (
        CheckConstraint("mode IN ('pr', 'fast')", name="ck_agent_intakes_mode"),
        CheckConstraint(
            "status IN ('queued', 'analyzing', 'needs_answers', 'ready', 'failed', 'confirmed', 'cancelled')",
            name="ck_agent_intakes_status",
        ),
        Index("ix_agent_intakes_queue", "status", "lease_until"),
        Index("ix_agent_intakes_user_chat", "user_id", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    questions: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), unique=True
    )
    confirmed_mode: Mapped[str | None] = mapped_column(String(8))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analysis_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    notified_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bot_message_id: Mapped[int | None] = mapped_column(BigInteger)

    user: Mapped["User"] = relationship()
    project: Mapped["Project"] = relationship()
    task: Mapped["Task | None"] = relationship()
