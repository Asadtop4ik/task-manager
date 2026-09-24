"""One resumable, private Codex conversation per user and project."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ProjectDiscussion(Base, TimestampMixin):
    __tablename__ = "project_discussions"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_project_discussion_user_project"),
        Index("ix_project_discussion_queue", "status", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="idle", nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(100))
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    pending_text: Mapped[str | None] = mapped_column(Text)
    pending_images: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    response_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notified_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
