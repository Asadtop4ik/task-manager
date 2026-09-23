"""Permit one new agent run after a failed or cancelled attempt.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("attempt_index", sa.Integer(), server_default="1", nullable=False),
    )
    op.drop_constraint("uq_agent_run_task_revision", "agent_runs", type_="unique")
    op.create_unique_constraint(
        "uq_agent_run_task_attempt",
        "agent_runs",
        ["task_id", "task_revision", "attempt_index"],
    )


def downgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM agent_runs GROUP BY task_id, task_revision "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicates is not None:
        raise RuntimeError("cannot downgrade while retry runs exist")
    op.drop_constraint("uq_agent_run_task_attempt", "agent_runs", type_="unique")
    op.create_unique_constraint(
        "uq_agent_run_task_revision", "agent_runs", ["task_id", "task_revision"]
    )
    op.drop_column("agent_runs", "attempt_index")
