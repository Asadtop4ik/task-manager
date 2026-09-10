import pytest

from app.db.enums import OPEN_STATUSES, TASK_TRANSITIONS, TaskStatus, can_transition


def test_every_status_has_a_transition_rule() -> None:
    # A status missing from the table can never be left, which would strand a task.
    assert set(TASK_TRANSITIONS) == set(TaskStatus)


def test_terminal_states_only_reopen_to_todo() -> None:
    for status in (TaskStatus.DONE, TaskStatus.CANCELLED):
        assert TASK_TRANSITIONS[status] == frozenset({TaskStatus.TODO})


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.TODO, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.DONE),
        (TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS),
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
