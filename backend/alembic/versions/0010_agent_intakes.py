"""Store agent intake questions and Telegram image references before task creation.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_intakes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("images", postgresql.JSONB(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("brief", postgresql.JSONB(), nullable=False),
        sa.Column("answer_text", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), unique=True),
        sa.Column("confirmed_mode", sa.String(8)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.String(36)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("analysis_rounds", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("notified_revision", sa.Integer(), nullable=False),
        sa.Column("bot_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("mode IN ('pr', 'fast')", name="ck_agent_intakes_mode"),
        sa.CheckConstraint(
            "status IN ('queued', 'analyzing', 'needs_answers', 'ready', 'failed', 'confirmed', 'cancelled')",
            name="ck_agent_intakes_status",
        ),
    )
    op.create_index("ix_agent_intakes_queue", "agent_intakes", ["status", "lease_until"])
    op.create_index(
        "ix_agent_intakes_user_chat", "agent_intakes", ["user_id", "chat_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_intakes_user_chat", table_name="agent_intakes")
    op.drop_index("ix_agent_intakes_queue", table_name="agent_intakes")
    op.drop_table("agent_intakes")
