"""Record agent delivery milestones for the first 20 real tasks.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("runner_started_at", "pr_ready_at", "merged_at", "deployed_at"):
        op.add_column("agent_runs", sa.Column(name, sa.DateTime(timezone=True)))


def downgrade() -> None:
    for name in ("deployed_at", "merged_at", "pr_ready_at", "runner_started_at"):
        op.drop_column("agent_runs", name)
