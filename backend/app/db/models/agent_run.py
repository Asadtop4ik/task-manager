from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.task import Task


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "task_revision", "attempt_index", name="uq_agent_run_task_attempt"
        ),
        Index("ix_agent_runs_status_created", "status", "created_at"),
        CheckConstraint("executor IN ('github', 'local')", name="ck_agent_runs_executor"),
        Index("ix_agent_runs_executor_status_lease", "executor", "status", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_index: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(8), default="pr", server_default="pr", nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    ci_status: Mapped[str | None] = mapped_column(String(16))
    ci_verified_sha: Mapped[str | None] = mapped_column(String(40))
    ci_url: Mapped[str | None] = mapped_column(Text)
    github_run_url: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(String(40))
    merged_sha: Mapped[str | None] = mapped_column(String(40))
    deployed_sha: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_notice_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_notice_message_id: Mapped[int | None] = mapped_column(BigInteger)
    qa_ready_url: Mapped[str | None] = mapped_column(Text)
    qa_ready_sha: Mapped[str | None] = mapped_column(String(40))
    qa_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qa_deploy_dispatch_status: Mapped[str | None] = mapped_column(String(16))
    qa_deploy_dispatch_error: Mapped[str | None] = mapped_column(Text)
    qa_deploy_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runner_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pr_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_status: Mapped[str | None] = mapped_column(String(16))
    review_sha: Mapped[str | None] = mapped_column(String(40))
    review_summary: Mapped[str | None] = mapped_column(Text)
    review_findings: Mapped[list[dict[str, object]] | None] = mapped_column(JSON)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executor: Mapped[str] = mapped_column(
        String(8), default="github", server_default="github", nullable=False
    )
    lease_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_kind: Mapped[str | None] = mapped_column(String(12))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the current lease_id was minted. A hard ceiling on top of the
    # renewable lease_until: an agent-svc that keeps heartbeating without
    # ever finishing would otherwise hold a lease forever.
    lease_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many times a local-executor review lease has expired for the head it
    # is tracking. Reset to 0 whenever `review_attempts_sha` no longer matches
    # `head_sha` (a new commit means a fresh review, not a retried one).
    review_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    review_attempts_sha: Mapped[str | None] = mapped_column(String(40))
    # Codex's own free text explaining ops requests it did *not* make (e.g.
    # "SUPER_ADMIN_TG_IDS isn't set; ask the owner to add it first"), stripped
    # of the trailer line by agent-svc before it ever reaches here. Display
    # only, never parsed. See `app.schemas.agent_run.AgentRunCallback.ops_note`.
    ops_note: Mapped[str | None] = mapped_column(Text)

    task: Mapped["Task"] = relationship()


class AgentRunAction(Base, TimestampMixin):
    """Idempotent owner requests that are completed by trusted GitHub workflows."""

    __tablename__ = "agent_run_actions"

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_data: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    # How many times a local-executor correction lease has expired while this
    # action was "in_progress". Only meaningful for local corrections.
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
