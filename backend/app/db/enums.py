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
# status change is allowed to be.
#
# Any open status can move to any other open status. A board is a thing you drag
# work around on, in both directions: starting something by mistake, or parking
# it back in the queue, is an ordinary correction and refusing it just makes the
# board feel broken.
#
# What the table still guards is the terminal states. Finishing means passing
# through work, so nothing jumps from the backlog straight to done, and a task
# that is done or cancelled reopens to todo and nowhere else — that is what stops
# a stale Telegram card from dropping a closed task back into whatever it used to
# be.
_OPEN = (
    TaskStatus.BACKLOG,
    TaskStatus.TODO,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.REVIEW,
)

# Only work that has actually been worked on can be called done.
_CAN_FINISH = (TaskStatus.IN_PROGRESS, TaskStatus.REVIEW)

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    **{
        status: frozenset(
            {other for other in _OPEN if other != status}
            | {TaskStatus.CANCELLED}
            | ({TaskStatus.DONE} if status in _CAN_FINISH else set())
        )
        for status in _OPEN
    },
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
