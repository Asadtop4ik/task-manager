"""Every model, imported here so `Base.metadata` is complete.

Alembic's env.py imports only this module — a model that is not re-exported here
is invisible to autogenerate and will silently never get a table.
"""

from app.db.base import Base
from app.db.models.agent_event import AgentEvent
from app.db.models.agent_intake import AgentIntake
from app.db.models.agent_run import AgentRun, AgentRunAction
from app.db.models.misc import Activity, Attachment, Comment, Reminder
from app.db.models.project import Membership, Project
from app.db.models.project_discussion import ProjectDiscussion
from app.db.models.task import Task
from app.db.models.team import JoinRequest, TeamInvite
from app.db.models.user import User

__all__ = [
    "Activity",
    "AgentEvent",
    "AgentIntake",
    "AgentRun",
    "AgentRunAction",
    "Attachment",
    "Base",
    "Comment",
    "JoinRequest",
    "Membership",
    "Project",
    "ProjectDiscussion",
    "Reminder",
    "Task",
    "TeamInvite",
    "User",
]
