"""Record whether a run requested direct validated deployment.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("mode", sa.String(8), server_default="pr", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "mode")
