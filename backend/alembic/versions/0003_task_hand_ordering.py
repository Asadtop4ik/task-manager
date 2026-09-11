"""task hand ordering

Adds the float ordering key a board column is sorted by, so a card can be
dropped between two others with one UPDATE instead of renumbering the column.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11 06:01:05.146527
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('position', sa.Float(), server_default='0', nullable=False))
    op.create_index('ix_tasks_status_position', 'tasks', ['status', 'position'], unique=False)
    # Existing rows would all share position 0 and sort arbitrarily. Spread them
    # by id — that is the order they were created in, which is the order the
    # board showed before this migration.
    op.execute("UPDATE tasks SET position = id * 1024.0")


def downgrade() -> None:
    op.drop_index('ix_tasks_status_position', table_name='tasks')
    op.drop_column('tasks', 'position')
