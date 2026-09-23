"""Every model, imported here so `Base.metadata` is complete.

Alembic's env.py imports only this module — a model that is not re-exported here
is invisible to autogenerate and will silently never get a table.
"""

from app.db.base import Base
from app.db.models.agent_run import AgentRun
from app.db.models.misc import Activity, Attachment, Comment, Reminder
from app.db.models.project import Membership, Project
from app.db.models.task import Task
from app.db.models.user import User

__all__ = [
    "Activity",
    "AgentRun",
    "Attachment",
    "Base",
    "Comment",
    "Membership",
    "Project",
    "Reminder",
    "Task",
    "User",
]
