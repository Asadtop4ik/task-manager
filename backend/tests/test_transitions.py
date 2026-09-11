import pytest

from app.db.enums import OPEN_STATUSES, TASK_TRANSITIONS, TaskStatus, can_transition


def test_every_status_has_a_transition_rule() -> None:
    # A status missing from the table can never be left, which would strand a task.
    assert set(TASK_TRANSITIONS) == set(TaskStatus)


def test_every_open_status_reaches_every_other_open_status() -> None:
    """The board has to work in both directions, column to column."""
    open_statuses = set(TaskStatus) - {TaskStatus.DONE, TaskStatus.CANCELLED}
    for current in open_statuses:
        for target in open_statuses - {current}:
            assert can_transition(current, target), f"{current} -> {target}"


def test_only_started_work_can_be_finished() -> None:
    # Nothing jumps from the queue straight to done without being worked on.
    assert not can_transition(TaskStatus.BACKLOG, TaskStatus.DONE)
    assert not can_transition(TaskStatus.TODO, TaskStatus.DONE)
    assert can_transition(TaskStatus.IN_PROGRESS, TaskStatus.DONE)
    assert can_transition(TaskStatus.REVIEW, TaskStatus.DONE)


def test_terminal_states_only_reopen_to_todo() -> None:
    for status in (TaskStatus.DONE, TaskStatus.CANCELLED):
        assert TASK_TRANSITIONS[status] == frozenset({TaskStatus.TODO})


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.TODO, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
        (TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS),
        # Dragging a card back where it came from. A board is used in both
        # directions and refusing this made it feel broken.
        (TaskStatus.IN_PROGRESS, TaskStatus.TODO),
        (TaskStatus.REVIEW, TaskStatus.TODO),
        (TaskStatus.BLOCKED, TaskStatus.BACKLOG),
        (TaskStatus.REVIEW, TaskStatus.BLOCKED),
    ],
)
def test_allowed_transitions(current: TaskStatus, target: TaskStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # The case this table exists for: a stale Telegram card must not be able
        # to jump a backlog item straight to done, or reopen a finished task
        # into whatever it was before.
        (TaskStatus.BACKLOG, TaskStatus.DONE),
        (TaskStatus.DONE, TaskStatus.IN_PROGRESS),
        (TaskStatus.TODO, TaskStatus.TODO),
    ],
)
def test_rejected_transitions(current: TaskStatus, target: TaskStatus) -> None:
    assert not can_transition(current, target)


def test_open_statuses_exclude_terminal_ones() -> None:
    assert set(TaskStatus) - {TaskStatus.DONE, TaskStatus.CANCELLED} == OPEN_STATUSES
