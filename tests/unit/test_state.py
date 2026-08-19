from __future__ import annotations

import pytest

from boxporter.core.boxes import Box, box_for
from boxporter.core.errors import IllegalTransitionError
from boxporter.core.state import (
    RunState,
    TaskState,
    check_run_transition,
    check_task_transition,
)


@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        (TaskState.PENDING, TaskState.READY, True),
        (TaskState.PENDING, TaskState.CANCELED, True),
        (TaskState.PENDING, TaskState.WORKING, False),
        (TaskState.READY, TaskState.WORKING, True),
        (TaskState.READY, TaskState.PASS, False),
        (TaskState.WORKING, TaskState.REVIEW_PENDING, True),
        (TaskState.WORKING, TaskState.BLOCKED, True),
        (TaskState.WORKING, TaskState.FAILED, True),
        (TaskState.WORKING, TaskState.DONE, False),
        (TaskState.WORKING, TaskState.PASS, False),
        (TaskState.REVIEW_PENDING, TaskState.PASS, True),
        (TaskState.REVIEW_PENDING, TaskState.REVISE, True),
        (TaskState.REVIEW_PENDING, TaskState.BLOCKED, True),
        (TaskState.REVIEW_PENDING, TaskState.WORKING, False),
        (TaskState.REVISE, TaskState.READY, True),
        (TaskState.BLOCKED, TaskState.READY, True),
        (TaskState.BLOCKED, TaskState.WORKING, False),
        (TaskState.FAILED, TaskState.READY, True),
        (TaskState.FAILED, TaskState.BLOCKED, True),
        (TaskState.PASS, TaskState.DONE, True),
        (TaskState.DONE, TaskState.READY, False),
        (TaskState.CANCELED, TaskState.READY, False),
    ],
)
def test_task_transition_table(source: TaskState, target: TaskState, allowed: bool) -> None:
    if allowed:
        check_task_transition(source, target)
    else:
        with pytest.raises(IllegalTransitionError):
            check_task_transition(source, target)


def test_run_transition_table() -> None:
    check_run_transition(RunState.CREATED, RunState.STARTING)
    check_run_transition(RunState.STARTING, RunState.RUNNING)
    check_run_transition(RunState.RUNNING, RunState.CHECKPOINTING)
    check_run_transition(RunState.CHECKPOINTING, RunState.RUNNING)
    check_run_transition(RunState.RUNNING, RunState.SUCCEEDED)
    check_run_transition(RunState.RUNNING, RunState.CRASHED)
    check_run_transition(RunState.RUNNING, RunState.WAITING_APPROVAL)
    with pytest.raises(IllegalTransitionError):
        check_run_transition(RunState.SUCCEEDED, RunState.RUNNING)
    with pytest.raises(IllegalTransitionError):
        check_run_transition(RunState.CREATED, RunState.SUCCEEDED)


def test_box_projection_mapping() -> None:
    expected = {
        TaskState.PENDING: Box.PENDING,
        TaskState.READY: Box.PENDING,
        TaskState.WORKING: Box.ACTIVE,
        TaskState.REVIEW_PENDING: Box.ACTIVE,
        TaskState.REVISE: Box.ACTIVE,
        TaskState.FAILED: Box.ACTIVE,
        TaskState.BLOCKED: Box.BLOCKED,
        TaskState.PASS: Box.PASSED,
        TaskState.DONE: Box.PASSED,
        TaskState.CANCELED: Box.ARCHIVED,
    }
    for state, box in expected.items():
        assert box_for(state) == box
