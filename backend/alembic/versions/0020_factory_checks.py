"""Store the acceptance-check gate result and the owner's per-attempt note.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("check_gate", sa.JSON(), nullable=True))
    op.add_column("agent_runs", sa.Column("owner_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "owner_note")
    op.drop_column("agent_runs", "check_gate")
