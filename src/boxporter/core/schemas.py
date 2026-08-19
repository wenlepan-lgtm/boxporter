"""V2 protocol schemas: TaskSpec, aggregate row models, canonical JSON.

BOXPORTER_TASK_V2 is the task protocol document (plan §5.3). The database
columns hold query fields; `task_spec_json` holds the full canonical spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .clock import now_iso
from .errors import ValidationError
from .ids import validate_goal_id, validate_project_id, validate_task_id
from .state import AttemptState, RunState, TaskState

TASK_SPEC_SCHEMA = "BOXPORTER_TASK_V2"

PRIORITY_LOW = 10
PRIORITY_MEDIUM = 50
PRIORITY_HIGH = 90

RISK_LEVELS = frozenset({"low", "medium", "high"})

REQUIRED_EVIDENCE_KINDS = frozenset(
    {"changed_files", "git_diff", "test_commands_with_exit_codes", "remaining_risks"}
)


def canonical_json(value: dict[str, Any]) -> str:
    """Deterministic JSON serialization: sorted keys, ASCII, compact, no
    trailing whitespace. Hash inputs must always go through this function
    (ADR-005)."""
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class TaskSpec:
    """Validated BOXPORTER_TASK_V2 payload."""

    task_id: str
    project_id: str
    title: str
    objective: str
    priority: int
    risk_level: str
    workspace: str
    goal_id: str | None = None
    base_commit: str | None = None
    dependencies: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    executor_profile: str = "boxporter-executor"
    reviewer_profile: str = "boxporter-reviewer"
    max_attempts: int = 4
    timeout_seconds: int = 7200
    token_budget: int = 200000
    created_at: str = field(default_factory=now_iso)

    def validate(self) -> None:
        validate_task_id(self.task_id)
        validate_project_id(self.project_id)
        if self.goal_id is not None:
            validate_goal_id(self.goal_id)
        if not self.title.strip():
            raise ValidationError("task title must not be empty")
        if not self.objective.strip():
            raise ValidationError("task objective must not be empty")
        if not 0 <= self.priority <= 100:
            raise ValidationError("priority must be within [0, 100]")
        if self.risk_level not in RISK_LEVELS:
            raise ValidationError(f"risk_level must be one of {sorted(RISK_LEVELS)}")
        if not self.workspace.strip():
            raise ValidationError("workspace must be a non-empty absolute path")
        if self.max_attempts < 1:
            raise ValidationError("max_attempts must be >= 1")
        if self.timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive")
        if self.token_budget <= 0:
            raise ValidationError("token_budget must be positive")
        if not self.acceptance_criteria:
            raise ValidationError("acceptance_criteria must not be empty")
        for kind in self.required_evidence:
            if kind not in REQUIRED_EVIDENCE_KINDS:
                raise ValidationError(f"unknown required_evidence kind: {kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_SPEC_SCHEMA,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "title": self.title,
            "objective": self.objective,
            "priority": self.priority,
            "risk_level": self.risk_level,
            "workspace": self.workspace,
            "base_commit": self.base_commit,
            "dependencies": list(self.dependencies),
            "inputs": list(self.inputs),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_evidence": list(self.required_evidence),
            "executor_profile": self.executor_profile,
            "reviewer_profile": self.reviewer_profile,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "token_budget": self.token_budget,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskSpec:
        if value.get("schema") != TASK_SPEC_SCHEMA:
            raise ValidationError("unsupported task spec schema")
        try:
            spec = cls(
                task_id=str(value["task_id"]),
                project_id=str(value["project_id"]),
                goal_id=value.get("goal_id"),
                title=str(value["title"]),
                objective=str(value["objective"]),
                priority=int(value["priority"]),
                risk_level=str(value["risk_level"]),
                workspace=str(value["workspace"]),
                base_commit=value.get("base_commit"),
                dependencies=tuple(str(x) for x in value.get("dependencies", ())),
                inputs=tuple(str(x) for x in value.get("inputs", ())),
                constraints=tuple(str(x) for x in value.get("constraints", ())),
                acceptance_criteria=tuple(str(x) for x in value.get("acceptance_criteria", ())),
                required_evidence=tuple(str(x) for x in value.get("required_evidence", ())),
                executor_profile=str(value.get("executor_profile", "boxporter-executor")),
                reviewer_profile=str(value.get("reviewer_profile", "boxporter-reviewer")),
                max_attempts=int(value.get("max_attempts", 4)),
                timeout_seconds=int(value.get("timeout_seconds", 7200)),
                token_budget=int(value.get("token_budget", 200000)),
                created_at=str(value.get("created_at", now_iso())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"invalid task spec: {exc}") from exc
        spec.validate()
        return spec


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    workspace_root: str
    status: str
    config: dict[str, Any]
    created_at: str
    updated_at: str
    version: int = 1


@dataclass(frozen=True)
class Goal:
    id: str
    project_id: str
    title: str
    outcome: str
    success_criteria: tuple[str, ...]
    progress: float
    status: str
    created_at: str
    updated_at: str
    version: int = 1


@dataclass(frozen=True)
class Task:
    id: str
    project_id: str
    goal_id: str | None
    title: str
    objective: str
    state: TaskState
    priority: int
    risk_level: str
    current_attempt: int
    max_attempts: int
    timeout_seconds: int
    spec: TaskSpec
    created_at: str
    updated_at: str
    version: int


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str
    number: int
    state: AttemptState
    created_at: str
    recovery_count: int = 0
    next_retry_at: str | None = None
    error_fingerprint: str | None = None


@dataclass(frozen=True)
class Run:
    id: str
    attempt_id: str
    role: str
    runner: str
    provider: str | None
    model: str | None
    identity: str
    session_id: str
    state: RunState
    checkpoint_ref: str | None
    started_at: str | None
    ended_at: str | None
    stop_reason: str | None
    worktree: str | None = None
    prompt_sha: str | None = None


@dataclass(frozen=True)
class EventRecord:
    seq: int
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    occurred_at: str
    causation_id: str | None
    correlation_id: str | None


@dataclass(frozen=True)
class Submission:
    id: str
    attempt_id: str
    submission_sha256: str
    head_commit: str
    git_tree_sha: str
    manifest: dict[str, Any]
    frozen_at: str
    invalidated_at: str | None = None


@dataclass(frozen=True)
class Review:
    id: str
    submission_id: str
    run_id: str
    result: str
    report_ref: str
    evidence_sha256: str
    created_at: str


@dataclass(frozen=True)
class Artifact:
    id: str
    run_id: str | None
    submission_id: str | None
    kind: str
    uri: str
    sha256: str
    size_bytes: int | None
    redaction_status: str
    created_at: str
