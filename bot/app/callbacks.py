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


class DiscussionAction(CallbackData, prefix="pd"):
    action: str  # task | reset
    discussion_id: int


class JoinAction(CallbackData, prefix="j"):
    action: str  # approve | reject | project | confirm | cancel
    request_id: int
    project_id: int = 0


class AgentReleaseAction(CallbackData, prefix="ar"):
    """Owner controls for one exact PR head; compact enough for Telegram's 64-byte cap."""

    action: str  # merge | correct | detail
    run_id: str
    sha12: str


class AgentOpsAction(CallbackData, prefix="ao"):
    """Owner controls for one ops request; ask/yes/no/back stay well under 64 bytes.

    `h` is the first 10 hex characters of the request's `request_hash` — enough to
    detect a stale card (the request changed since this button was drawn) without
    paying Telegram's byte budget for the full 64-char hash.
    """

    action: str  # ask | yes | no | back
    ops_id: int
    h: str
