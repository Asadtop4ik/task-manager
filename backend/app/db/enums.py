from enum import StrEnum


class UserRole(StrEnum):
    MANAGER = "manager"
    EXECUTOR = "executor"


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    CANCELLED = "cancelled"


# The one place the board, the bot's inline buttons and the API agree on what a
# status change is allowed to be. Anything not listed is rejected, which is what
# keeps a stale Telegram card from reopening a task closed a week ago.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.BACKLOG: frozenset({TaskStatus.TODO, TaskStatus.CANCELLED}),
    TaskStatus.TODO: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.BACKLOG, TaskStatus.CANCELLED}
    ),
    TaskStatus.IN_PROGRESS: frozenset(
        {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.TODO, TaskStatus.CANCELLED}
    ),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED}
    ),
    # Terminal states reopen to todo and nothing else — deliberately narrow.
    TaskStatus.DONE: frozenset({TaskStatus.TODO}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.TODO}),
}

OPEN_STATUSES = frozenset(
    {
        TaskStatus.BACKLOG,
        TaskStatus.TODO,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
        TaskStatus.REVIEW,
    }
)


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in TASK_TRANSITIONS.get(current, frozenset())


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskSource(StrEnum):
    BOT = "bot"
    WEB = "web"


class ActivityKind(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    DUE_CHANGED = "due_changed"
    COMMENTED = "commented"
    TIME_LOGGED = "time_logged"
    ATTACHED = "attached"


class ReminderKind(StrEnum):
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"
    DIGEST = "digest"
    SNOOZE = "snooze"
