"""Keep structured lifecycle events for every Codex flow.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE")
        ),
        sa.Column(
            "agent_intake_id",
            sa.Integer(),
            sa.ForeignKey("agent_intakes.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "project_discussion_id",
            sa.Integer(),
            sa.ForeignKey("project_discussions.id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24)),
        sa.Column("error", sa.Text()),
        sa.Column("github_run_url", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "num_nonnulls(agent_run_id, agent_intake_id, project_discussion_id) = 1",
            name="ck_agent_events_one_subject",
        ),
    )
    op.create_index("ix_agent_events_created", "agent_events", ["created_at", "id"])
    op.create_index("ix_agent_events_run", "agent_events", ["agent_run_id", "id"])
    op.create_index("ix_agent_events_intake", "agent_events", ["agent_intake_id", "id"])
    op.create_index(
        "ix_agent_events_discussion", "agent_events", ["project_discussion_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_discussion", table_name="agent_events")
    op.drop_index("ix_agent_events_intake", table_name="agent_events")
    op.drop_index("ix_agent_events_run", table_name="agent_events")
    op.drop_index("ix_agent_events_created", table_name="agent_events")
    op.drop_table("agent_events")
