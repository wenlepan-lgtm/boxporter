"""Four-box projection over fine-grained task states (ADR-003).

The boxes are a human-facing stage view. They are derived from `state`,
never stored as a second source of truth.
"""

from __future__ import annotations

from enum import Enum

from .state import TaskState


class Box(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    PASSED = "PASSED"
    ARCHIVED = "ARCHIVED"


TASK_STATE_BOX: dict[TaskState, Box] = {
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

# Boxes shown in the four-box kanban. ARCHIVED is not one of the four.
FOUR_BOXES: tuple[Box, Box, Box, Box] = (
    Box.PENDING,
    Box.ACTIVE,
    Box.BLOCKED,
    Box.PASSED,
)


def box_for(state: TaskState) -> Box:
    return TASK_STATE_BOX[state]
