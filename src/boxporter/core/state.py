"""Typed task and run state machines with legal transition tables.

ADR-003: the four boxes are projections; fine-grained state lives here.
All transitions must go through :func:`check_task_transition` /
:func:`check_run_transition`; commands may not mutate state columns directly.
"""

from __future__ import annotations

from enum import Enum

from .errors import IllegalTransitionError


class TaskState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    WORKING = "WORKING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVISE = "REVISE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    PASS = "PASS"
    DONE = "DONE"
    CANCELED = "CANCELED"


class RunState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CHECKPOINTING = "CHECKPOINTING"
    SUCCEEDED = "SUCCEEDED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    STALLED = "STALLED"
    TIMED_OUT = "TIMED_OUT"
    CRASHED = "CRASHED"
    CANCELED = "CANCELED"


class AttemptState(str, Enum):
    OPEN = "OPEN"
    SUBMITTED = "SUBMITTED"
    REVISED = "REVISED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.CANCELED}),
    TaskState.READY: frozenset({TaskState.WORKING, TaskState.CANCELED}),
    TaskState.WORKING: frozenset(
        {TaskState.REVIEW_PENDING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELED}
    ),
    TaskState.REVIEW_PENDING: frozenset(
        {TaskState.PASS, TaskState.REVISE, TaskState.BLOCKED}
    ),
    TaskState.REVISE: frozenset({TaskState.READY, TaskState.CANCELED}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.CANCELED}),
    TaskState.FAILED: frozenset({TaskState.READY, TaskState.BLOCKED, TaskState.CANCELED}),
    TaskState.PASS: frozenset({TaskState.DONE}),
    TaskState.DONE: frozenset(),
    TaskState.CANCELED: frozenset(),
}

# Terminal task states: no further task-level transitions are possible.
TASK_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.DONE, TaskState.CANCELED}
)

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.STARTING, RunState.CANCELED}),
    RunState.STARTING: frozenset({RunState.RUNNING, RunState.CRASHED, RunState.CANCELED}),
    RunState.RUNNING: frozenset(
        {
            RunState.CHECKPOINTING,
            RunState.SUCCEEDED,
            RunState.WAITING_APPROVAL,
            RunState.STALLED,
            RunState.TIMED_OUT,
            RunState.CRASHED,
            RunState.CANCELED,
        }
    ),
    RunState.CHECKPOINTING: frozenset({RunState.RUNNING, RunState.CRASHED}),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.CANCELED, RunState.CRASHED}
    ),
    RunState.STALLED: frozenset({RunState.RUNNING, RunState.CANCELED, RunState.CRASHED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.TIMED_OUT: frozenset(),
    RunState.CRASHED: frozenset(),
    RunState.CANCELED: frozenset(),
}

# Run states that still hold (or may still hold) execution rights.
RUN_ACTIVE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.CREATED,
        RunState.STARTING,
        RunState.RUNNING,
        RunState.CHECKPOINTING,
        RunState.WAITING_APPROVAL,
        RunState.STALLED,
    }
)


def check_task_transition(current: TaskState, target: TaskState) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal task transition: {current.value} -> {target.value}")


def check_run_transition(current: RunState, target: RunState) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise IllegalTransitionError(f"illegal run transition: {current.value} -> {target.value}")
