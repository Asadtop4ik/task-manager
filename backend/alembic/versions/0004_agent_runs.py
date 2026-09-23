"""Track agent jobs and bind projects to an explicitly configured repository.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("repo_full_name", sa.String(200), nullable=True))
    op.add_column("projects", sa.Column("default_branch", sa.String(120), nullable=True))
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_revision", sa.String(64), nullable=False),
        sa.Column("repo_full_name", sa.String(200), nullable=False),
        sa.Column("base_branch", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("github_run_url", sa.Text(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("task_id", "task_revision", name="uq_agent_run_task_revision"),
    )
    op.create_index("ix_agent_runs_status_created", "agent_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_status_created", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_column("projects", "default_branch")
    op.drop_column("projects", "repo_full_name")
