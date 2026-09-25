"""Small, durable lifecycle events for the three Codex flows."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(agent_run_id, agent_intake_id, project_discussion_id) = 1",
            name="ck_agent_events_one_subject",
        ),
        Index("ix_agent_events_created", "created_at", "id"),
        Index("ix_agent_events_run", "agent_run_id", "id"),
        Index("ix_agent_events_intake", "agent_intake_id", "id"),
        Index("ix_agent_events_discussion", "project_discussion_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    agent_intake_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_intakes.id", ondelete="CASCADE")
    )
    project_discussion_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_discussions.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    github_run_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
