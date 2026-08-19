"""Identifier generation and validation."""

from __future__ import annotations

import re
import uuid

from .errors import ValidationError

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GOAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValidationError("task id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_project_id(project_id: str) -> None:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValidationError("project id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def validate_goal_id(goal_id: str) -> None:
    if not GOAL_ID_RE.fullmatch(goal_id):
        raise ValidationError("goal id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
