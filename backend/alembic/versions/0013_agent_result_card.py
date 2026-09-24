"""Keep one Telegram result card across PR, merge and deploy updates.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("telegram_message_id", sa.BigInteger()))


def downgrade() -> None:
    op.drop_column("agent_runs", "telegram_message_id")
