from aiogram.fsm.state import State, StatesGroup


class NewTask(StatesGroup):
    """The guided /new flow, one inline keyboard per step."""

    project = State()
    title = State()
    assignee = State()
    priority = State()
    due = State()


class QuickCapture(StatesGroup):
    """A parsed line waiting for confirmation, or a title being retyped."""

    confirming = State()
    editing_title = State()


class CardAction(StatesGroup):
    """Text the card asked for: a comment body, or a new deadline."""

    comment = State()
    snooze = State()
