"""Idempotent command handlers for the V2 protocol kernel.

Each command validates preconditions, applies exactly one state-machine
transition set, appends events and returns a serializable result. Everything
runs inside the transaction opened by ``Store.execute``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from boxporter.core.acceptance import check_acceptance
from boxporter.core.clock import now_iso, parse_iso_utc
from boxporter.core.errors import NotFoundError
from boxporter.core.gitworktree import WorktreeManager
from boxporter.core.ids import new_id, validate_goal_id, validate_project_id
from boxporter.core.redaction import scan_files
from boxporter.core.schemas import (
    RISK_LEVELS,
    Artifact,
    Attempt,
    Goal,
    Project,
    Review,
    Run,
    Submission,
    Task,
    TaskSpec,
    canonical_json,
)
from boxporter.core.state import (
    RUN_ACTIVE_STATES,
    AttemptState,
    RunState,
    TaskState,
    check_run_transition,
    check_task_transition,
)
from boxporter.core.submission import (
    REPORT_FILES,
    SubmissionManifest,
    artifact_manifest_sha256,
    build_artifact_manifest,
    sha256_file,
)
from boxporter.storage.events import ActorType, EventType
from boxporter.storage.metering import Approval, Blocker, MemoryItem

from .base import Command, CommandFailed, CommandResult

if TYPE_CHECKING:
    from boxporter.storage.store import Store

EXECUTOR_ROLE = "executor"
REVIEWER_ROLE = "reviewer"

REVIEW_RESULTS = frozenset({"PASS", "REVISE", "BLOCKED"})

_EVENT_ACTORS = frozenset(
    {ActorType.SYSTEM, ActorType.USER, ActorType.DAEMON, ActorType.EXECUTOR, ActorType.REVIEWER}
)


def _check_actor(actor_type: str) -> None:
    if actor_type not in _EVENT_ACTORS:
        raise CommandFailed(f"unknown actor type: {actor_type}")


def _get(store: Store, task_id: str) -> Task:
    return store.tasks.get(store.db.conn, task_id)


def _cancel_active_runs(store: Store, task_id: str) -> int:
    cancelled = 0
    for run in store.runs.list_for_task(store.db.conn, task_id):
        if run.state in RUN_ACTIVE_STATES:
            check_run_transition(run.state, RunState.CANCELED)
            store.runs.update_state(
                store.db.conn, run.id, RunState.CANCELED, stop_reason="task-canceled"
            )
            store.events.append(
                store.db.conn,
                aggregate_type="run",
                aggregate_id=run.id,
                event_type=EventType.RUN_CANCELED,
                actor_type=ActorType.SYSTEM,
                payload={"task_id": task_id, "reason": "task-canceled"},
            )
            cancelled += 1
    return cancelled


@dataclass(frozen=True)
class CreateProject(Command):
    command: ClassVar[str] = "create_project"
    aggregate_type: ClassVar[str] = "project"

    project_id: str
    name: str
    workspace_root: str
    actor_type: str = ActorType.USER
    actor_id: str | None = None
    config: dict[str, object] = field(default_factory=dict)

    @property
    def aggregate_id(self) -> str:
        return self.project_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        validate_project_id(self.project_id)
        if not self.name.strip():
            raise CommandFailed("project name must not be empty")
        conn = store.db.conn
        try:
            store.projects.get(conn, self.project_id)
        except NotFoundError:
            pass
        else:
            raise CommandFailed(f"project already exists: {self.project_id}")
        now = now_iso()
        store.projects.insert(
            conn,
            Project(
                id=self.project_id,
                name=self.name,
                workspace_root=self.workspace_root,
                status="active",
                config=dict(self.config),
                created_at=now,
                updated_at=now,
            ),
        )
        store.events.append(
            conn,
            aggregate_type="project",
            aggregate_id=self.project_id,
            event_type=EventType.PROJECT_CREATED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"name": self.name, "workspace_root": self.workspace_root},
        )
        return CommandResult(
            ok=True, message=f"project created: {self.project_id}",
            data={"project_id": self.project_id},
        )


@dataclass(frozen=True)
class CreateGoal(Command):
    command: ClassVar[str] = "create_goal"
    aggregate_type: ClassVar[str] = "goal"

    goal_id: str
    project_id: str
    title: str
    outcome: str
    success_criteria: tuple[str, ...] = ()
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.goal_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        validate_goal_id(self.goal_id)
        conn = store.db.conn
        store.projects.get(conn, self.project_id)
        try:
            store.goals.get(conn, self.goal_id)
        except NotFoundError:
            pass
        else:
            raise CommandFailed(f"goal already exists: {self.goal_id}")
        now = now_iso()
        store.goals.insert(
            conn,
            Goal(
                id=self.goal_id,
                project_id=self.project_id,
                title=self.title,
                outcome=self.outcome,
                success_criteria=self.success_criteria,
                progress=0.0,
                status="active",
                created_at=now,
                updated_at=now,
            ),
        )
        store.events.append(
            conn,
            aggregate_type="goal",
            aggregate_id=self.goal_id,
            event_type=EventType.GOAL_CREATED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"project_id": self.project_id, "title": self.title},
        )
        return CommandResult(
            ok=True, message=f"goal created: {self.goal_id}",
            data={"goal_id": self.goal_id},
        )


@dataclass(frozen=True)
class CreateTask(Command):
    command: ClassVar[str] = "create_task"
    aggregate_type: ClassVar[str] = "task"

    spec: TaskSpec
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.spec.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        self.spec.validate()
        conn = store.db.conn
        store.projects.get(conn, self.spec.project_id)
        if self.spec.goal_id is not None:
            goal = store.goals.get(conn, self.spec.goal_id)
            if goal.project_id != self.spec.project_id:
                raise CommandFailed("goal belongs to a different project")
        for dependency in self.spec.dependencies:
            if not store.tasks.exists(conn, dependency):
                raise CommandFailed(f"dependency task not found: {dependency}")
        if store.tasks.exists(conn, self.spec.task_id):
            raise CommandFailed(f"task already exists: {self.spec.task_id}")
        now = now_iso()
        store.tasks.insert(
            conn,
            Task(
                id=self.spec.task_id,
                project_id=self.spec.project_id,
                goal_id=self.spec.goal_id,
                title=self.spec.title,
                objective=self.spec.objective,
                state=TaskState.PENDING,
                priority=self.spec.priority,
                risk_level=self.spec.risk_level,
                current_attempt=0,
                max_attempts=self.spec.max_attempts,
                timeout_seconds=self.spec.timeout_seconds,
                spec=self.spec,
                created_at=now,
                updated_at=now,
                version=1,
            ),
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=self.spec.task_id,
            event_type=EventType.TASK_CREATED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "project_id": self.spec.project_id,
                "goal_id": self.spec.goal_id,
                "title": self.spec.title,
                "risk_level": self.spec.risk_level,
                "priority": self.spec.priority,
                "dependencies": list(self.spec.dependencies),
            },
        )
        return CommandResult(
            ok=True, message=f"task created: {self.spec.task_id}",
            data={"task_id": self.spec.task_id, "state": TaskState.PENDING.value},
        )


@dataclass(frozen=True)
class ReadyTask(Command):
    command: ClassVar[str] = "ready_task"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        check_task_transition(task.state, TaskState.READY)
        gaps = self._readiness_gaps(store, task)
        if gaps:
            raise CommandFailed("task not ready: " + "; ".join(gaps))
        store.tasks.update_state(conn, task.id, TaskState.READY, task.version)
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_READY,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value},
        )
        return CommandResult(
            ok=True, message=f"task ready: {task.id}",
            data={"task_id": task.id, "state": TaskState.READY.value},
        )

    @staticmethod
    def _readiness_gaps(store: Store, task: Task) -> list[str]:
        gaps: list[str] = []
        if not Path(task.spec.workspace).expanduser().is_dir():
            gaps.append(f"workspace does not exist: {task.spec.workspace}")
        if not store.tasks.dependencies_satisfied(store.db.conn, task):
            gaps.append("dependencies not satisfied")
        return gaps


@dataclass(frozen=True)
class CancelTask(Command):
    command: ClassVar[str] = "cancel_task"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    reason: str = "canceled by user"
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        check_task_transition(task.state, TaskState.CANCELED)
        _cancel_active_runs(store, task.id)
        store.tasks.update_state(conn, task.id, TaskState.CANCELED, task.version)
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_CANCELED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value, "reason": self.reason},
        )
        return CommandResult(
            ok=True, message=f"task canceled: {task.id}",
            data={"task_id": task.id, "state": TaskState.CANCELED.value},
        )


@dataclass(frozen=True)
class BlockTask(Command):
    command: ClassVar[str] = "block_task"
    aggregate_type: ClassVar[str] = "task"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {ActorType.SYSTEM, ActorType.USER, ActorType.DAEMON, ActorType.EXECUTOR}
    )

    task_id: str
    reason: str
    probe_command: tuple[str, ...] = ()
    probe_interval_seconds: int = 900
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        if not self.reason.strip():
            raise CommandFailed("block reason must not be empty")
        conn = store.db.conn
        task = _get(store, self.task_id)
        check_task_transition(task.state, TaskState.BLOCKED)
        _cancel_active_runs(store, task.id)
        store.tasks.update_state(conn, task.id, TaskState.BLOCKED, task.version)
        next_probe = None
        if self.probe_command:
            from datetime import datetime, timedelta, timezone

            next_probe = (
                datetime.now(timezone.utc)
                + timedelta(seconds=self.probe_interval_seconds)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        store.blockers.insert(
            conn,
            Blocker(
                id=new_id("blk"),
                task_id=task.id,
                reason=self.reason,
                probe_command=self.probe_command,
                probe_interval_seconds=self.probe_interval_seconds,
                next_probe_at=next_probe,
                created_at=now_iso(),
                resolved_at=None,
            ),
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_BLOCKED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "from": task.state.value,
                "reason": self.reason,
                "probe": list(self.probe_command),
            },
        )
        return CommandResult(
            ok=True, message=f"task blocked: {task.id}",
            data={"task_id": task.id, "state": TaskState.BLOCKED.value, "reason": self.reason},
        )


@dataclass(frozen=True)
class UnblockTask(Command):
    command: ClassVar[str] = "unblock_task"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    note: str = ""
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        check_task_transition(task.state, TaskState.READY)
        store.tasks.update_state(conn, task.id, TaskState.READY, task.version)
        for blocker in store.blockers.list_open_for_task(conn, task.id):
            store.blockers.resolve(conn, blocker.id)
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_UNBLOCKED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value, "note": self.note},
        )
        return CommandResult(
            ok=True, message=f"task unblocked: {task.id}",
            data={"task_id": task.id, "state": TaskState.READY.value},
        )


@dataclass(frozen=True)
class StartExecutorRun(Command):
    command: ClassVar[str] = "start_executor_run"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    runner: str
    identity: str
    session_id: str
    provider: str | None = None
    model: str | None = None
    worktree: str | None = None
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        if store.runs.has_active_run(conn, task.id, EXECUTOR_ROLE):
            raise CommandFailed("an executor run is already active for this task")
        check_task_transition(task.state, TaskState.WORKING)
        number = task.current_attempt
        if number == 0:
            number = 1
            attempt = Attempt(
                id=new_id("attempt"),
                task_id=task.id,
                number=number,
                state=AttemptState.OPEN,
                created_at=now_iso(),
            )
            store.attempts.insert(conn, attempt)
            store.events.append(
                conn,
                aggregate_type="attempt",
                aggregate_id=attempt.id,
                event_type=EventType.ATTEMPT_CREATED,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                payload={"task_id": task.id, "number": number},
            )
        else:
            attempt = store.attempts.get_by_task_number(conn, task.id, number)
            if attempt.state != AttemptState.OPEN:
                raise CommandFailed(
                    f"attempt {number} is not open: {attempt.state.value}"
                )
        run = Run(
            id=new_id("run"),
            attempt_id=attempt.id,
            role=EXECUTOR_ROLE,
            runner=self.runner,
            provider=self.provider,
            model=self.model,
            identity=self.identity,
            session_id=self.session_id,
            state=RunState.STARTING,
            checkpoint_ref=None,
            started_at=now_iso(),
            ended_at=None,
            stop_reason=None,
            worktree=self.worktree,
        )
        store.runs.insert(conn, run)
        store.tasks.update_state(
            conn, task.id, TaskState.WORKING, task.version, current_attempt=number
        )
        for event_type in (EventType.RUN_CREATED, EventType.RUN_STARTED):
            store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=run.id,
                event_type=event_type,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                payload={
                    "task_id": task.id,
                    "attempt": number,
                    "role": EXECUTOR_ROLE,
                    "runner": self.runner,
                    "identity": self.identity,
                    "session_id": self.session_id,
                },
            )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_WORKING,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value, "attempt": number, "run_id": run.id},
        )
        return CommandResult(
            ok=True, message=f"executor run started: {run.id}",
            data={"run_id": run.id, "attempt": number, "task_id": task.id},
        )


@dataclass(frozen=True)
class StartReviewerRun(Command):
    command: ClassVar[str] = "start_reviewer_run"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    runner: str
    identity: str
    session_id: str
    provider: str | None = None
    model: str | None = None
    worktree: str | None = None
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        if task.state != TaskState.REVIEW_PENDING:
            raise CommandFailed(
                f"reviewer can only start from REVIEW_PENDING, got {task.state.value}"
            )
        if store.runs.has_active_run(conn, task.id, REVIEWER_ROLE):
            raise CommandFailed("a reviewer run is already active for this task")
        attempt = store.attempts.get_by_task_number(conn, task.id, task.current_attempt)
        run = Run(
            id=new_id("run"),
            attempt_id=attempt.id,
            role=REVIEWER_ROLE,
            runner=self.runner,
            provider=self.provider,
            model=self.model,
            identity=self.identity,
            session_id=self.session_id,
            state=RunState.STARTING,
            checkpoint_ref=None,
            started_at=now_iso(),
            ended_at=None,
            stop_reason=None,
            worktree=self.worktree,
        )
        store.runs.insert(conn, run)
        for event_type in (EventType.RUN_CREATED, EventType.RUN_STARTED):
            store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=run.id,
                event_type=event_type,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                payload={
                    "task_id": task.id,
                    "attempt": task.current_attempt,
                    "role": REVIEWER_ROLE,
                    "runner": self.runner,
                    "identity": self.identity,
                    "session_id": self.session_id,
                },
            )
        return CommandResult(
            ok=True, message=f"reviewer run started: {run.id}",
            data={"run_id": run.id, "attempt": task.current_attempt, "task_id": task.id},
        )


@dataclass(frozen=True)
class MarkRunRunning(Command):
    command: ClassVar[str] = "mark_run_running"
    aggregate_type: ClassVar[str] = "run"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {
            ActorType.SYSTEM,
            ActorType.DAEMON,
            ActorType.EXECUTOR,
            ActorType.REVIEWER,
            ActorType.USER,
        }
    )

    run_id: str
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.run_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        run = store.runs.get(conn, self.run_id)
        check_run_transition(run.state, RunState.RUNNING)
        store.runs.update_state(conn, run.id, RunState.RUNNING)
        store.events.append(
            conn,
            aggregate_type="run",
            aggregate_id=run.id,
            event_type=EventType.RUN_RUNNING,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": run.state.value},
        )
        return CommandResult(
            ok=True, message=f"run running: {run.id}",
            data={"run_id": run.id, "state": RunState.RUNNING.value},
        )


@dataclass(frozen=True)
class SubmitExecutorRun(Command):
    """Freeze the executor's worktree + reports into a Submission Manifest V2
    and move the task to REVIEW_PENDING (ADR-005).

    REVIEW_PENDING is only reachable through this freeze; a submission must
    exist before any review can happen.
    """

    command: ClassVar[str] = "submit_executor_run"
    aggregate_type: ClassVar[str] = "run"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {ActorType.SYSTEM, ActorType.DAEMON, ActorType.EXECUTOR}
    )

    run_id: str
    report_dir: str
    worktree: str
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.run_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        run = store.runs.get(conn, self.run_id)
        if run.role != EXECUTOR_ROLE:
            raise CommandFailed(f"run {run.id} is not an executor run")
        check_run_transition(run.state, RunState.SUCCEEDED)
        attempt = store.attempts.get(conn, run.attempt_id)
        task = _get(store, attempt.task_id)
        check_task_transition(task.state, TaskState.REVIEW_PENDING)

        worktree_path = Path(self.worktree).resolve()
        if not worktree_path.is_dir():
            raise CommandFailed(f"worktree does not exist: {worktree_path}")
        report_path = Path(self.report_dir).resolve()
        for name in REPORT_FILES:
            if not (report_path / name).is_file():
                raise CommandFailed(f"missing report file: {report_path / name}")

        manager = WorktreeManager(worktree_path.parent.parent / "worktrees")
        identity = manager.identity(worktree_path, base_commit=task.spec.base_commit)

        task_sha = hashlib.sha256(canonical_json(task.spec.to_dict()).encode()).hexdigest()
        artifact_manifest_json = build_artifact_manifest(
            {name: report_path / name for name in REPORT_FILES}
        )
        manifest = SubmissionManifest(
            task_id=task.id,
            attempt=attempt.number,
            base_commit=task.spec.base_commit,
            head_commit=identity.head_commit,
            git_tree_sha=identity.tree_sha,
            git_diff_sha256=identity.diff_sha256,
            task_sha256=task_sha,
            report_hashes={
                name: sha256_file(report_path / name) for name in REPORT_FILES
            },
            artifact_manifest_sha256=artifact_manifest_sha256(artifact_manifest_json),
            executor_run_id=run.id,
            executor_session_ref=run.session_id,
            executor_worktree=str(worktree_path),
            created_at=now_iso(),
        )
        submission_sha = manifest.submission_sha256
        stored_manifest = manifest.to_dict()
        stored_manifest["submission_sha256"] = submission_sha
        submission = Submission(
            id=new_id("sub"),
            attempt_id=attempt.id,
            submission_sha256=submission_sha,
            head_commit=identity.head_commit,
            git_tree_sha=identity.tree_sha,
            manifest=stored_manifest,
            frozen_at=now_iso(),
        )
        store.submissions.insert(conn, submission)
        for name in REPORT_FILES:
            path = report_path / name
            store.artifacts.insert(
                conn,
                Artifact(
                    id=new_id("art"),
                    run_id=run.id,
                    submission_id=submission.id,
                    kind=name,
                    uri=str(path),
                    sha256=sha256_file(path),
                    size_bytes=path.stat().st_size,
                    redaction_status="unscanned",
                    created_at=now_iso(),
                ),
            )

        store.runs.update_state(conn, run.id, RunState.SUCCEEDED)
        store.attempts.update_state(conn, attempt.id, AttemptState.SUBMITTED)
        store.tasks.update_state(conn, task.id, TaskState.REVIEW_PENDING, task.version)
        for event_type, payload in (
            (EventType.RUN_SUCCEEDED, {"task_id": task.id, "from": run.state.value}),
            (
                EventType.ATTEMPT_SUBMITTED,
                {"task_id": task.id, "number": attempt.number},
            ),
            (
                EventType.TASK_REVIEW_PENDING,
                {"from": task.state.value, "run_id": run.id},
            ),
        ):
            aggregate_type = "attempt" if event_type == EventType.ATTEMPT_SUBMITTED else (
                "task" if event_type == EventType.TASK_REVIEW_PENDING else "run"
            )
            aggregate_id = (
                attempt.id
                if event_type == EventType.ATTEMPT_SUBMITTED
                else task.id if event_type == EventType.TASK_REVIEW_PENDING else run.id
            )
            store.events.append(
                conn,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                payload=payload,
            )
        store.events.append(
            conn,
            aggregate_type="submission",
            aggregate_id=submission.id,
            event_type=EventType.SUBMISSION_FROZEN,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "task_id": task.id,
                "attempt": attempt.number,
                "submission_sha256": submission_sha,
                "head_commit": identity.head_commit,
                "tree_sha": identity.tree_sha,
            },
        )
        return CommandResult(
            ok=True, message=f"submission frozen: {submission_sha[:16]}",
            data={
                "run_id": run.id,
                "task_id": task.id,
                "submission_id": submission.id,
                "submission_sha256": submission_sha,
                "state": TaskState.REVIEW_PENDING.value,
            },
        )


@dataclass(frozen=True)
class FailRun(Command):
    command: ClassVar[str] = "fail_run"
    aggregate_type: ClassVar[str] = "run"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {ActorType.SYSTEM, ActorType.DAEMON, ActorType.EXECUTOR}
    )

    run_id: str
    kind: str = "crash"
    stop_reason: str = ""
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.run_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        if self.kind not in {"crash", "timeout", "budget"}:
            raise CommandFailed(f"unknown failure kind: {self.kind}")
        conn = store.db.conn
        run = store.runs.get(conn, self.run_id)
        if run.role != EXECUTOR_ROLE:
            raise CommandFailed(f"run {run.id} is not an executor run")
        target = RunState.TIMED_OUT if self.kind in {"timeout", "budget"} else RunState.CRASHED
        check_run_transition(run.state, target)
        attempt = store.attempts.get(conn, run.attempt_id)
        task = _get(store, attempt.task_id)
        check_task_transition(task.state, TaskState.FAILED)
        reason = self.stop_reason or (
            "budget-exceeded" if self.kind == "budget" else self.kind
        )
        store.runs.update_state(conn, run.id, target, stop_reason=reason)
        store.attempts.update_state(conn, attempt.id, AttemptState.FAILED)
        # Failure fingerprint + exponential backoff (plan §9.3, acceptance 12/13).
        from datetime import datetime, timedelta, timezone

        from boxporter.core.recovery import backoff_seconds, error_fingerprint

        fingerprint = error_fingerprint(reason)
        next_count = attempt.recovery_count + 1
        next_retry = (
            datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(next_count))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        store.attempts.record_failure(
            conn,
            attempt.id,
            fingerprint=fingerprint,
            next_retry_at=next_retry,
        )
        store.tasks.update_state(conn, task.id, TaskState.FAILED, task.version)
        store.events.append(
            conn,
            aggregate_type="run",
            aggregate_id=run.id,
            event_type=EventType.RUN_CRASHED if self.kind == "crash" else EventType.RUN_TIMED_OUT,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"task_id": task.id, "from": run.state.value, "reason": reason},
        )
        store.events.append(
            conn,
            aggregate_type="attempt",
            aggregate_id=attempt.id,
            event_type=EventType.ATTEMPT_FAILED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "task_id": task.id,
                "number": attempt.number,
                "kind": self.kind,
                "fingerprint": fingerprint,
                "next_retry_at": next_retry,
            },
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_FAILED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value, "kind": self.kind, "reason": reason},
        )
        return CommandResult(
            ok=True, message=f"executor run failed ({self.kind}): {run.id}",
            data={"run_id": run.id, "task_id": task.id, "state": TaskState.FAILED.value},
        )


@dataclass(frozen=True)
class ReviewTask(Command):
    command: ClassVar[str] = "review_task"
    aggregate_type: ClassVar[str] = "task"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {ActorType.SYSTEM, ActorType.DAEMON, ActorType.REVIEWER}
    )

    task_id: str
    reviewer_run_id: str
    result: str
    required_changes: tuple[str, ...] = ()
    note: str = ""
    review_dir: str | None = None
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        result = self.result.upper()
        if result not in REVIEW_RESULTS:
            raise CommandFailed(f"review result must be one of {sorted(REVIEW_RESULTS)}")
        if result == "REVISE" and not self.required_changes:
            raise CommandFailed("REVISE requires at least one required change")
        conn = store.db.conn
        task = _get(store, self.task_id)
        if task.state != TaskState.REVIEW_PENDING:
            raise CommandFailed(f"cannot review from state {task.state.value}")
        attempt = store.attempts.get_by_task_number(conn, task.id, task.current_attempt)
        submission = store.submissions.get_for_attempt(conn, attempt.id)
        if submission is None:
            raise CommandFailed("no frozen submission for the current attempt")
        if submission.invalidated_at is not None:
            raise CommandFailed("submission has been invalidated; resubmit required")
        reviewer = store.runs.get(conn, self.reviewer_run_id)
        if reviewer.role != REVIEWER_ROLE:
            raise CommandFailed(f"run {reviewer.id} is not a reviewer run")
        if reviewer.state != RunState.SUCCEEDED:
            raise CommandFailed(f"reviewer run is not SUCCEEDED: {reviewer.state.value}")
        reviewer_attempt = store.attempts.get(conn, reviewer.attempt_id)
        if reviewer_attempt.task_id != task.id or reviewer_attempt.number != task.current_attempt:
            raise CommandFailed("reviewer run does not belong to the current attempt")
        executor = self._find_executor(store, task)
        if reviewer.identity == executor.identity:
            raise CommandFailed("reviewer identity must differ from executor identity")
        if reviewer.session_id == executor.session_id:
            raise CommandFailed("reviewer session must differ from executor session")

        review_evidence = self._load_review_evidence()
        review_report_ref = ""
        if self.review_dir is not None:
            review_path = Path(self.review_dir).resolve()
            if (review_path / "review.md").is_file():
                review_report_ref = str(review_path / "review.md")
                self._store_review_artifacts(
                    store, submission.id, reviewer.id, review_path
                )

        if result == "PASS":
            problems = check_acceptance(
                store,
                task,
                submission,
                executor_run_id=executor.id,
                executor_session=executor.session_id,
                executor_worktree=executor.worktree,
                reviewer_run_id=reviewer.id,
                reviewer_session=reviewer.session_id,
                reviewer_worktree=reviewer.worktree,
                reviewer_evidence=review_evidence,
            )
            if problems:
                raise CommandFailed("acceptance gate failed: " + "; ".join(problems))
            target = TaskState.PASS
            attempt_state = AttemptState.PASSED
            event_type = EventType.TASK_PASS
            attempt_event = EventType.ATTEMPT_PASSED
        elif result == "REVISE":
            target = TaskState.REVISE
            attempt_state = AttemptState.REVISED
            event_type = EventType.TASK_REVISE
            attempt_event = EventType.ATTEMPT_REVISED
        else:
            target = TaskState.BLOCKED
            attempt_state = attempt.state
            event_type = EventType.TASK_BLOCKED
            attempt_event = None
        check_task_transition(task.state, target)

        review = Review(
            id=new_id("rev"),
            submission_id=submission.id,
            run_id=reviewer.id,
            result=result,
            report_ref=review_report_ref,
            evidence_sha256=self._evidence_sha256(),
            created_at=now_iso(),
        )
        store.reviews.insert(conn, review)
        store.tasks.update_state(conn, task.id, target, task.version)
        store.attempts.update_state(conn, attempt.id, attempt_state)
        if attempt_event is not None:
            store.events.append(
                conn,
                aggregate_type="attempt",
                aggregate_id=attempt.id,
                event_type=attempt_event,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                payload={"task_id": task.id, "number": attempt.number, "result": result},
            )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=event_type,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "from": task.state.value,
                "result": result,
                "reviewer_run_id": reviewer.id,
                "reviewer_identity": reviewer.identity,
                "required_changes": list(self.required_changes),
                "note": self.note,
                "submission_sha256": submission.submission_sha256,
            },
        )
        store.events.append(
            conn,
            aggregate_type="review",
            aggregate_id=review.id,
            event_type=EventType.REVIEW_RECORDED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "task_id": task.id,
                "submission_id": submission.id,
                "result": result,
                "run_id": reviewer.id,
            },
        )
        return CommandResult(
            ok=True, message=f"review recorded: {result}",
            data={"task_id": task.id, "state": target.value, "result": result},
        )

    def _load_review_evidence(self) -> dict[str, object] | None:
        if self.review_dir is None:
            return None
        evidence_path = Path(self.review_dir) / "review_evidence.json"
        if not evidence_path.is_file():
            return None
        try:
            value = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _evidence_sha256(self) -> str:
        if self.review_dir is None:
            return ""
        evidence_path = Path(self.review_dir) / "review_evidence.json"
        if not evidence_path.is_file():
            return ""
        return sha256_file(evidence_path)

    def _store_review_artifacts(
        self, store: Store, submission_id: str, reviewer_run_id: str, review_path: Path
    ) -> None:
        conn = store.db.conn
        for name in ("review.md", "review_evidence.json"):
            path = review_path / name
            if not path.is_file():
                continue
            store.artifacts.insert(
                conn,
                Artifact(
                    id=new_id("art"),
                    run_id=reviewer_run_id,
                    submission_id=submission_id,
                    kind=name,
                    uri=str(path),
                    sha256=sha256_file(path),
                    size_bytes=path.stat().st_size,
                    redaction_status="unscanned",
                    created_at=now_iso(),
                ),
            )

    @staticmethod
    def _find_executor(store: Store, task: Task) -> Run:
        for run in store.runs.list_for_task(store.db.conn, task.id):
            if run.role == EXECUTOR_ROLE and run.state == RunState.SUCCEEDED:
                return run
        raise CommandFailed("no succeeded executor run found for the current attempt")


@dataclass(frozen=True)
class FinalizeTaskDone(Command):
    """Seal the PASSED evidence package (ADR-005, ADR-007) and move the task
    to DONE. The package is immutable and offline-verifiable; a secret scan
    blocks sealing (ADR-008)."""

    command: ClassVar[str] = "finalize_task_done"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    evidence_root: str | None = None
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        check_task_transition(task.state, TaskState.DONE)
        if self.evidence_root is None:
            raise CommandFailed("evidence_root is required to seal evidence")
        attempt = store.attempts.get_by_task_number(conn, task.id, task.current_attempt)
        submission = store.submissions.get_for_attempt(conn, attempt.id)
        if submission is None:
            raise CommandFailed("no frozen submission to seal")
        executor = self._find_executor(store, task)
        package_dir = (
            Path(self.evidence_root).resolve() / "passed" / task.id
            / submission.submission_sha256
        )
        staging = package_dir.parent / f".{submission.submission_sha256}.tmp"
        if package_dir.exists() or staging.exists():
            raise CommandFailed(f"evidence package already sealed: {package_dir}")
        try:
            self._build_package(store, task, attempt, submission, executor, staging)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        os.replace(staging, package_dir)
        store.tasks.update_state(conn, task.id, TaskState.DONE, task.version)
        # Gated memory write (plan §12.3): PASS evidence is a valid source.
        store.memory.insert(
            conn,
            MemoryItem(
                id=new_id("mem"),
                project_id=task.project_id,
                kind="task-completed",
                content=f"{task.id}: {task.title} -> PASSED (submission"
                f" {submission.submission_sha256[:16]})",
                source="pass-evidence",
                source_ref=str(package_dir),
                expires_at=None,
                created_at=now_iso(),
            ),
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_DONE,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value},
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.EVIDENCE_SEALED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "package": str(package_dir),
                "submission_sha256": submission.submission_sha256,
            },
        )
        return CommandResult(
            ok=True, message=f"task done: {task.id}",
            data={"task_id": task.id, "state": TaskState.DONE.value,
                  "package": str(package_dir)},
        )

    def _build_package(
        self,
        store: Store,
        task: Task,
        attempt: Attempt,
        submission: Submission,
        executor: Run,
        staging: Path,
    ) -> None:
        conn = store.db.conn
        artifacts = store.artifacts.for_submission(conn, submission.id)
        by_kind = {artifact.kind: artifact for artifact in artifacts}
        staging.mkdir(parents=True)

        copies: dict[str, Path] = {}
        task_text = (
            f"# {task.title}\n\n"
            f"task_id: {task.id}\nproject: {task.project_id}\n"
            f"attempt: {attempt.number}\nstate: DONE\n\n"
            f"## Objective\n\n{task.objective}\n\n"
            f"## Acceptance criteria\n\n"
            + "".join(f"- {item}\n" for item in task.spec.acceptance_criteria)
            + "\n## Spec (BOXPORTER_TASK_V2)\n\n```json\n"
            + json.dumps(task.spec.to_dict(), ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        (staging / "task.md").write_text(task_text, encoding="utf-8")
        for name in ("result.md", "verify.md", "executor.md", "review.md"):
            artifact = by_kind.get(name)
            if artifact is None:
                continue
            source = Path(artifact.uri)
            target = staging / name
            shutil.copyfile(source, target)
            copies[name] = target
        review_evidence = by_kind.get("review_evidence.json")
        if review_evidence is not None:
            target = staging / "review-evidence" / "review_evidence.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(review_evidence.uri), target)
            copies["review-evidence/review_evidence.json"] = target

        (staging / "submission-manifest.json").write_text(
            json.dumps(submission.manifest, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (staging / "commit.json").write_text(
            json.dumps(
                {
                    "base_commit": task.spec.base_commit,
                    "head_commit": submission.head_commit,
                    "git_tree_sha": submission.git_tree_sha,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (staging / "trajectory.ref.json").write_text(
            json.dumps(
                {
                    "schema": "BOXPORTER_TRAJECTORY_REF_V1",
                    "executor_session_id": executor.session_id,
                    "executor_run_id": executor.id,
                    "redaction_status": "reference-only",
                    "note": "full session stays in the runtime store (ADR-007)",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        scanned = [staging / "task.md", *copies.values()]
        findings = scan_files(scanned)
        if findings:
            for finding in findings:
                store.events.append(
                    conn,
                    aggregate_type="task",
                    aggregate_id=task.id,
                    event_type=EventType.SECURITY_FINDING,
                    actor_type=ActorType.SYSTEM,
                    payload={
                        "file": finding.file,
                        "pattern": finding.pattern,
                        "stage": "evidence-seal",
                    },
                )
            raise CommandFailed(
                f"secret scan blocked sealing: {len(findings)} finding(s)"
            )

        manifest_files: dict[str, str] = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                manifest_files[str(path.relative_to(staging))] = sha256_file(path)
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "BOXPORTER_ARCHIVE_V2",
                    "task_id": task.id,
                    "submission_sha256": submission.submission_sha256,
                    "sealed_at": now_iso(),
                    "files": manifest_files,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _find_executor(store: Store, task: Task) -> Run:
        for run in store.runs.list_for_task(store.db.conn, task.id):
            if run.role == EXECUTOR_ROLE and run.state == RunState.SUCCEEDED:
                return run
        raise CommandFailed("no succeeded executor run found for the current attempt")


@dataclass(frozen=True)
class BeginNextAttempt(Command):
    command: ClassVar[str] = "begin_next_attempt"
    aggregate_type: ClassVar[str] = "task"

    task_id: str
    actor_type: str = ActorType.DAEMON
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        conn = store.db.conn
        task = _get(store, self.task_id)
        if task.state not in {TaskState.REVISE, TaskState.FAILED}:
            raise CommandFailed(
                f"new attempt requires REVISE or FAILED, got {task.state.value}"
            )
        if task.current_attempt >= task.max_attempts:
            raise CommandFailed(
                f"max attempts reached: {task.current_attempt}/{task.max_attempts}"
            )
        number = task.current_attempt + 1
        attempt = Attempt(
            id=new_id("attempt"),
            task_id=task.id,
            number=number,
            state=AttemptState.OPEN,
            created_at=now_iso(),
        )
        store.attempts.insert(conn, attempt)
        check_task_transition(task.state, TaskState.READY)
        store.tasks.update_state(
            conn, task.id, TaskState.READY, task.version, current_attempt=number
        )
        store.events.append(
            conn,
            aggregate_type="attempt",
            aggregate_id=attempt.id,
            event_type=EventType.ATTEMPT_CREATED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"task_id": task.id, "number": number},
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.TASK_READY,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={"from": task.state.value, "attempt": number},
        )
        return CommandResult(
            ok=True, message=f"attempt {number} started for {task.id}",
            data={"task_id": task.id, "attempt": number, "state": TaskState.READY.value},
        )


@dataclass(frozen=True)
class RequestApproval(Command):
    """Request a scoped, time-boxed approval for a precise action (ADR-009)."""

    command: ClassVar[str] = "request_approval"
    aggregate_type: ClassVar[str] = "task"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {
            ActorType.SYSTEM,
            ActorType.DAEMON,
            ActorType.EXECUTOR,
            ActorType.REVIEWER,
            ActorType.USER,
        }
    )

    task_id: str
    action: str
    target: str
    risk_level: str = "high"
    max_uses: int = 1
    ttl_seconds: int = 3600
    run_id: str | None = None
    actor_type: str = ActorType.EXECUTOR
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.task_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        if not self.action.strip() or not self.target.strip():
            raise CommandFailed("approval action and target must not be empty")
        if self.risk_level not in RISK_LEVELS:
            raise CommandFailed(f"risk_level must be one of {sorted(RISK_LEVELS)}")
        if self.max_uses < 1 or self.ttl_seconds < 1:
            raise CommandFailed("max_uses and ttl_seconds must be positive")
        from datetime import datetime, timedelta, timezone

        conn = store.db.conn
        task = _get(store, self.task_id)
        approval = Approval(
            id=new_id("appr"),
            task_id=task.id,
            run_id=self.run_id,
            action=self.action,
            target=self.target,
            risk_level=self.risk_level,
            max_uses=self.max_uses,
            used_count=0,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            status="pending",
            requested_by=self.actor_id or self.actor_type,
            decided_by=None,
            decided_at=None,
            created_at=now_iso(),
        )
        store.approvals.insert(conn, approval)
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=task.id,
            event_type=EventType.APPROVAL_REQUESTED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "approval_id": approval.id,
                "action": self.action,
                "target": self.target,
                "risk_level": self.risk_level,
                "expires_at": approval.expires_at,
            },
        )
        return CommandResult(
            ok=True, message=f"approval requested: {approval.id}",
            data={"approval_id": approval.id},
        )


@dataclass(frozen=True)
class DecideApproval(Command):
    """Approve or reject a pending approval. Approval is bound to the exact
    action, target, max uses and expiry; broad grants are rejected."""

    command: ClassVar[str] = "decide_approval"
    aggregate_type: ClassVar[str] = "approval"
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {ActorType.SYSTEM, ActorType.DAEMON, ActorType.USER}
    )

    approval_id: str
    decision: str
    actor_type: str = ActorType.USER
    actor_id: str | None = None

    @property
    def aggregate_id(self) -> str:
        return self.approval_id

    def execute(self, store: Store) -> CommandResult:
        _check_actor(self.actor_type)
        decision = self.decision.lower()
        if decision not in {"approve", "reject"}:
            raise CommandFailed("decision must be approve or reject")
        conn = store.db.conn
        approval = store.approvals.get(conn, self.approval_id)
        if approval.status != "pending":
            raise CommandFailed(f"approval is not pending: {approval.status}")
        if parse_iso_utc(approval.expires_at) <= datetime.now(timezone.utc):
            conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE id = ?",
                (approval.id,),
            )
            raise CommandFailed("approval has expired")
        decided_at = now_iso()
        conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (
                "approved" if decision == "approve" else "rejected",
                self.actor_id or self.actor_type,
                decided_at,
                approval.id,
            ),
        )
        store.events.append(
            conn,
            aggregate_type="task",
            aggregate_id=str(approval.task_id or ""),
            event_type=EventType.APPROVAL_DECIDED,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            payload={
                "approval_id": approval.id,
                "decision": decision,
                "action": approval.action,
                "target": approval.target,
            },
        )
        return CommandResult(
            ok=True, message=f"approval {decision}d: {approval.id}",
            data={"approval_id": approval.id, "status": decision},
        )
