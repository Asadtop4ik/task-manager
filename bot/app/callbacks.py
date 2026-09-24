from aiogram.filters.callback_data import CallbackData


class TaskAction(CallbackData, prefix="t"):
    """Buttons on a task card.

    Telegram caps callback data at 64 bytes, so this stays to an action name and
    an id rather than anything descriptive.
    """

    action: str  # start | done | review | block | comment | snooze | refresh
    task_id: int


class NewTaskChoice(CallbackData, prefix="n"):
    """A step of the guided /new flow."""

    field: str  # project | assignee | priority | due
    value: str


class QuickConfirm(CallbackData, prefix="q"):
    action: str  # create | cancel | edit


class AgentIntakeAction(CallbackData, prefix="ai"):
    action: str  # confirm | edit | cancel | retry | fallback
    intake_id: int


class JoinAction(CallbackData, prefix="j"):
    action: str  # approve | reject | project | confirm | cancel
    request_id: int
    project_id: int = 0
