"""Keep deleted tasks and their audit/agent history for owner restore.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column(
        "tasks",
        sa.Column(
            "deleted_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
    )
    op.create_index("ix_tasks_deleted_at", "tasks", ["deleted_at"])
    op.add_column("activity", sa.Column("card_synced_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("activity", "card_synced_at")
    op.drop_index("ix_tasks_deleted_at", table_name="tasks")
    op.drop_column("tasks", "deleted_by_id")
    op.drop_column("tasks", "deleted_at")
