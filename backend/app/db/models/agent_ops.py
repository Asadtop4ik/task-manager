"""Codex-proposed, owner-approved env changes for a local-executor run.

Only non-secret keys from a root-owned allowlist ever reach this table (see
`app.services.agent_ops`); the applying root helper never prints a value, and
`AgentRunOut` (task-visible) never serializes one either.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.agent_run import AgentRun


class AgentOpsRequest(Base, TimestampMixin):
    __tablename__ = "agent_ops_requests"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "position", name="uq_agent_ops_requests_run_position"
        ),
        CheckConstraint("kind = 'env_set'", name="ck_agent_ops_requests_kind"),
        CheckConstraint(
            "op IN ('replace', 'list_add', 'list_remove')", name="ck_agent_ops_requests_op"
        ),
        CheckConstraint(
            "status IN ('proposed', 'invalid', 'rejected', 'approved', 'applying', "
            "'applied', 'failed', 'cancelled')",
            name="ck_agent_ops_requests_status",
        ),
        CheckConstraint("position BETWEEN 1 AND 3", name="ck_agent_ops_requests_position"),
        Index("ix_agent_ops_requests_status_lease", "status", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1..3, see the check in `app.services.agent_ops.store_proposals`.
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    project_key: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    op: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(256), nullable=False)
    # Codex's own text, display only, never parsed.
    reason: Mapped[str | None] = mapped_column(Text)
    restart_services: Mapped[list[str] | None] = mapped_column(JSON)
    # sha256 hex of the canonical request; see `app.services.agent_ops.request_hash`.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_reason: Mapped[str | None] = mapped_column(String(200))
    decision_action_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {code, exit, rolled_back, restarted, image_tag, message} — never values.
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)

    agent_run: Mapped["AgentRun"] = relationship()
