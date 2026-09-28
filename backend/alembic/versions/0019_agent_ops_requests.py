"""Track Codex-proposed, owner-approved env changes for a local-executor run.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_ops_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_uuid", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "agent_run_id",
            sa.Integer(),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("project_key", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("op", sa.String(16), nullable=False),
        sa.Column("value", sa.String(256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("restart_services", sa.JSON(), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("policy_reason", sa.String(200), nullable=True),
        sa.Column("decision_action_id", sa.String(36), nullable=True, unique=True),
        sa.Column(
            "decided_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "agent_run_id", "position", name="uq_agent_ops_requests_run_position"
        ),
        sa.CheckConstraint("kind = 'env_set'", name="ck_agent_ops_requests_kind"),
        sa.CheckConstraint(
            "op IN ('replace', 'list_add', 'list_remove')", name="ck_agent_ops_requests_op"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'invalid', 'rejected', 'approved', 'applying', "
            "'applied', 'failed', 'cancelled')",
            name="ck_agent_ops_requests_status",
        ),
    )
    op.create_index(
        "ix_agent_ops_requests_agent_run_id", "agent_ops_requests", ["agent_run_id"]
    )
    op.create_index(
        "ix_agent_ops_requests_status_lease", "agent_ops_requests", ["status", "lease_until"]
    )


def downgrade() -> None:
    # `ops_pending` has no meaning to the pre-0019 code (no ops_requests rows
    # for it to wait on any more); a run that reaches this migration in that
    # state is stuck exactly like an implement lease that never came back.
    op.execute("UPDATE agent_runs SET status = 'failed' WHERE status = 'ops_pending'")
    op.drop_index("ix_agent_ops_requests_status_lease", table_name="agent_ops_requests")
    op.drop_index("ix_agent_ops_requests_agent_run_id", table_name="agent_ops_requests")
    op.drop_table("agent_ops_requests")
