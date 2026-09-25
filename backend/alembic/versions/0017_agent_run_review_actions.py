"""Require an independent review and track owner release actions.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("review_status", sa.String(16)))
    op.add_column("agent_runs", sa.Column("review_sha", sa.String(40)))
    op.add_column("agent_runs", sa.Column("review_summary", sa.Text()))
    op.add_column("agent_runs", sa.Column("review_findings", sa.JSON()))
    op.add_column("agent_runs", sa.Column("owner_notice_chat_id", sa.BigInteger()))
    op.add_column("agent_runs", sa.Column("owner_notice_message_id", sa.BigInteger()))
    op.add_column("agent_runs", sa.Column("qa_ready_url", sa.Text()))
    op.add_column("agent_runs", sa.Column("qa_ready_sha", sa.String(40)))
    op.add_column("agent_runs", sa.Column("qa_ready_at", sa.DateTime(timezone=True)))
    # Existing PRs have no independent review evidence. Force them through a
    # fresh review before the API can consider them ready to merge.
    op.execute(
        """UPDATE agent_runs
              SET status = 'pr_opened', ci_status = 'pending',
                  ci_verified_sha = NULL, pr_ready_at = NULL,
                  notified_at = NULL
            WHERE status = 'pr_ready' AND pr_url IS NOT NULL"""
    )
    op.create_table(
        "agent_run_actions",
        sa.Column("action_id", sa.String(36), primary_key=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_agent_run_actions_agent_run_id", "agent_run_actions", ["agent_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_actions_agent_run_id", table_name="agent_run_actions")
    op.drop_table("agent_run_actions")
    op.drop_column("agent_runs", "review_findings")
    op.drop_column("agent_runs", "review_summary")
    op.drop_column("agent_runs", "review_sha")
    op.drop_column("agent_runs", "review_status")
    op.drop_column("agent_runs", "owner_notice_message_id")
    op.drop_column("agent_runs", "owner_notice_chat_id")
    op.drop_column("agent_runs", "qa_ready_at")
    op.drop_column("agent_runs", "qa_ready_sha")
    op.drop_column("agent_runs", "qa_ready_url")
