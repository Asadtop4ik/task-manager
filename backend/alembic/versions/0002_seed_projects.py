"""seed the three live projects

The projects are not user-generated content — they are the three things this
team actually works on, and the bot's quick-capture parser matches on their
keys ("keto: fix the url"). Seeding them in a migration means a fresh database,
local or production, comes up usable instead of empty.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTS = [
    ("ketoshop", "Ketoshop", "#10b981"),
    ("qurbot", "QurBot", "#f59e0b"),
    ("kans-shop", "Kans Shop", "#6366f1"),
]


def upgrade() -> None:
    projects = sa.table(
        "projects",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
        sa.column("color", sa.String),
        sa.column("is_archived", sa.Boolean),
    )
    # ON CONFLICT rather than a plain insert: this migration must be safe to run
    # against a database where someone already created one of them by hand.
    for key, name, color in PROJECTS:
        op.execute(
            sa.dialects.postgresql.insert(projects)
            # is_archived has to be spelled out: the model's default= is applied
            # by SQLAlchemy in Python, so a raw INSERT like this one never sees it.
            .values(key=key, name=name, color=color, is_archived=False)
            .on_conflict_do_nothing(index_elements=["key"])
        )


def downgrade() -> None:
    keys = tuple(key for key, _, _ in PROJECTS)
    # Only removes rows that still have no tasks; dropping a project someone has
    # been filing work against would take the work with it (tasks.project_id is
    # ON DELETE RESTRICT, so this would fail loudly anyway).
    op.execute(
        sa.text(
            "DELETE FROM projects WHERE key IN :keys "
            "AND id NOT IN (SELECT DISTINCT project_id FROM tasks)"
        ).bindparams(sa.bindparam("keys", value=keys, expanding=True))
    )
