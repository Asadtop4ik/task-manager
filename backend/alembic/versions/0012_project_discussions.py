"""Persist private project discussions and their queued turns.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_discussions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("thread_id", sa.String(100)),
        sa.Column("messages", postgresql.JSONB(), nullable=False),
        sa.Column("pending_text", sa.Text()),
        sa.Column("pending_images", postgresql.JSONB(), nullable=False),
        sa.Column("response_text", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("notified_revision", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "project_id", name="uq_project_discussion_user_project"),
    )
    op.create_index("ix_project_discussion_queue", "project_discussions", ["status", "lease_until"])


def downgrade() -> None:
    op.drop_index("ix_project_discussion_queue", table_name="project_discussions")
    op.drop_table("project_discussions")
