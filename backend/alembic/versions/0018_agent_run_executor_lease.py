"""Add a local agent-svc lease alongside the existing GitHub executor.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("executor", sa.String(8), server_default="github", nullable=False),
    )
    op.add_column("agent_runs", sa.Column("lease_id", sa.String(36)))
    op.add_column("agent_runs", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("agent_runs", sa.Column("lease_kind", sa.String(12)))
    op.add_column("agent_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column("agent_runs", sa.Column("lease_issued_at", sa.DateTime(timezone=True)))
    op.add_column(
        "agent_runs",
        sa.Column("review_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("agent_runs", sa.Column("review_attempts_sha", sa.String(40)))
    op.add_column(
        "agent_run_actions",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_agent_runs_executor", "agent_runs", "executor IN ('github', 'local')"
    )
    op.create_index(
        "ix_agent_runs_executor_status_lease",
        "agent_runs",
        ["executor", "status", "lease_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_executor_status_lease", table_name="agent_runs")
    op.drop_constraint("ck_agent_runs_executor", "agent_runs", type_="check")
    op.drop_column("agent_run_actions", "attempts")
    op.drop_column("agent_runs", "review_attempts_sha")
    op.drop_column("agent_runs", "review_attempts")
    op.drop_column("agent_runs", "lease_issued_at")
    op.drop_column("agent_runs", "heartbeat_at")
    op.drop_column("agent_runs", "lease_kind")
    op.drop_column("agent_runs", "lease_until")
    op.drop_column("agent_runs", "lease_id")
    op.drop_column("agent_runs", "executor")
