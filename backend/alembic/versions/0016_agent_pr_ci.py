"""Verify the exact PR commit before announcing it to the task owner.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("ci_status", sa.String(16)))
    op.add_column("agent_runs", sa.Column("ci_verified_sha", sa.String(40)))
    op.add_column("agent_runs", sa.Column("ci_url", sa.Text()))
    op.add_column("agent_runs", sa.Column("pr_opened_at", sa.DateTime(timezone=True)))
    # Earlier PR-ready callbacks did not prove CI. Re-check open PRs rather than
    # grandfathering a green state; preserve the original PR creation time.
    op.execute("""UPDATE agent_runs
           SET status = 'pr_opened', ci_status = 'pending',
               pr_opened_at = pr_ready_at, pr_ready_at = NULL,
               notified_at = CASE WHEN telegram_message_id IS NOT NULL
                                  THEN NULL ELSE notified_at END
         WHERE status = 'pr_ready' AND pr_url IS NOT NULL""")


def downgrade() -> None:
    op.execute("""UPDATE agent_runs SET status = 'pr_ready', pr_ready_at = pr_opened_at
         WHERE status = 'pr_opened' AND pr_url IS NOT NULL""")
    op.drop_column("agent_runs", "pr_opened_at")
    op.drop_column("agent_runs", "ci_url")
    op.drop_column("agent_runs", "ci_verified_sha")
    op.drop_column("agent_runs", "ci_status")
