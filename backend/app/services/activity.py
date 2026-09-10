from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ActivityKind
from app.db.models import Activity, User


def record(
    session: AsyncSession,
    *,
    task_id: int,
    actor: User | None,
    kind: ActivityKind,
    payload: dict[str, Any] | None = None,
) -> Activity:
    """Append one audit row.

    This is the notification source as well as the audit log: the notifier reads
    rows rather than being called from each handler, so "who gets a Telegram
    message" is decided in one place instead of re-derived in every endpoint.
    Callers add it to the session; the surrounding request commits.
    """
    entry = Activity(
        task_id=task_id,
        actor_id=actor.id if actor else None,
        kind=kind,
        payload=payload or {},
    )
    session.add(entry)
    return entry
