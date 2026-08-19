"""Typed repositories over a sqlite3 connection.

All mutations happen inside an explicit transaction supplied by the caller
(commands layer); nothing here commits or opens transactions.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from boxporter.core.clock import now_iso
from boxporter.core.errors import ConcurrencyError, NotFoundError
from boxporter.core.schemas import (
    Artifact,
    Attempt,
    Goal,
    Project,
    Review,
    Run,
    Submission,
    Task,
    TaskSpec,
)
from boxporter.core.state import RUN_ACTIVE_STATES, AttemptState, RunState, TaskState


class ProjectsRepo:
    def insert(self, conn: sqlite3.Connection, project: Project) -> None:
        conn.execute(
            "INSERT INTO projects (id, name, workspace_root, status, config_json,"
            " created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project.id,
                project.name,
                project.workspace_root,
                project.status,
                json.dumps(project.config, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                project.created_at,
                project.updated_at,
                project.version,
            ),
        )

    def get(self, conn: sqlite3.Connection, project_id: str) -> Project:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"project not found: {project_id}")
        return Project(
            id=str(row["id"]),
            name=str(row["name"]),
            workspace_root=str(row["workspace_root"]),
            status=str(row["status"]),
            config=json.loads(str(row["config_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=int(row["version"]),
        )


class GoalsRepo:
    def insert(self, conn: sqlite3.Connection, goal: Goal) -> None:
        conn.execute(
            "INSERT INTO goals (id, project_id, title, outcome, success_criteria_json,"
            " progress, status, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                goal.id,
                goal.project_id,
                goal.title,
                goal.outcome,
                json.dumps(list(goal.success_criteria), ensure_ascii=True, separators=(",", ":")),
                goal.progress,
                goal.status,
                goal.version,
                goal.created_at,
                goal.updated_at,
            ),
        )

    def get(self, conn: sqlite3.Connection, goal_id: str) -> Goal:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"goal not found: {goal_id}")
        return Goal(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            title=str(row["title"]),
            outcome=str(row["outcome"]),
            success_criteria=tuple(json.loads(str(row["success_criteria_json"]))),
            progress=float(row["progress"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=int(row["version"]),
        )

    def update_progress(
        self,
        conn: sqlite3.Connection,
        goal_id: str,
        progress: float,
        expected_version: int,
    ) -> None:
        now = now_iso()
        cursor = conn.execute(
            "UPDATE goals SET progress = ?, updated_at = ?, version = version + 1"
            " WHERE id = ? AND version = ?",
            (progress, now, goal_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyError(f"goal version conflict: {goal_id}")


class TasksRepo:
    def insert(self, conn: sqlite3.Connection, task: Task) -> None:
        conn.execute(
            "INSERT INTO tasks (id, project_id, goal_id, title, objective, state, priority,"
            " risk_level, current_attempt, max_attempts, timeout_seconds, version,"
            " task_spec_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.project_id,
                task.goal_id,
                task.title,
                task.objective,
                task.state.value,
                task.priority,
                task.risk_level,
                task.current_attempt,
                task.max_attempts,
                task.timeout_seconds,
                task.version,
                json.dumps(
                    task.spec.to_dict(),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                task.created_at,
                task.updated_at,
            ),
        )

    def get(self, conn: sqlite3.Connection, task_id: str) -> Task:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task not found: {task_id}")
        return self._row_to_task(row)

    def exists(self, conn: sqlite3.Connection, task_id: str) -> bool:
        row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row is not None

    def update_state(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        state: TaskState,
        expected_version: int,
        current_attempt: int | None = None,
    ) -> None:
        now = now_iso()
        cursor = conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ?, version = version + 1,"
            " current_attempt = COALESCE(?, current_attempt) WHERE id = ? AND version = ?",
            (state.value, now, current_attempt, task_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise ConcurrencyError(f"task version conflict: {task_id}")

    def list_by_project(self, conn: sqlite3.Connection, project_id: str) -> list[Task]:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY priority DESC, created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def dependencies_satisfied(self, conn: sqlite3.Connection, task: Task) -> bool:
        for dependency in task.spec.dependencies:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (dependency,)).fetchone()
            if row is None or str(row["state"]) not in {TaskState.PASS.value, TaskState.DONE.value}:
                return False
        return True

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            goal_id=row["goal_id"],
            title=str(row["title"]),
            objective=str(row["objective"]),
            state=TaskState(str(row["state"])),
            priority=int(row["priority"]),
            risk_level=str(row["risk_level"]),
            current_attempt=int(row["current_attempt"]),
            max_attempts=int(row["max_attempts"]),
            timeout_seconds=int(row["timeout_seconds"]),
            spec=TaskSpec.from_dict(json.loads(str(row["task_spec_json"]))),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=int(row["version"]),
        )


class AttemptsRepo:
    def insert(self, conn: sqlite3.Connection, attempt: Attempt) -> None:
        conn.execute(
            "INSERT INTO attempts (id, task_id, number, state, created_at) VALUES (?, ?, ?, ?, ?)",
            (attempt.id, attempt.task_id, attempt.number, attempt.state.value, attempt.created_at),
        )

    def get(self, conn: sqlite3.Connection, attempt_id: str) -> Attempt:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"attempt not found: {attempt_id}")
        return self._row_to_attempt(row)

    def get_by_task_number(
        self, conn: sqlite3.Connection, task_id: str, number: int
    ) -> Attempt:
        row = conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? AND number = ?", (task_id, number)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"attempt not found: {task_id}#{number}")
        return self._row_to_attempt(row)

    def list_for_task(
        self, conn: sqlite3.Connection, task_id: str
    ) -> list[Attempt]:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY number", (task_id,)
        ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def update_state(
        self, conn: sqlite3.Connection, attempt_id: str, state: AttemptState
    ) -> None:
        conn.execute(
            "UPDATE attempts SET state = ? WHERE id = ?", (state.value, attempt_id)
        )

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            number=int(row["number"]),
            state=AttemptState(str(row["state"])),
            created_at=str(row["created_at"]),
            recovery_count=int(row["recovery_count"]),
            next_retry_at=row["next_retry_at"],
            error_fingerprint=row["error_fingerprint"],
        )

    def record_failure(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
        *,
        fingerprint: str,
        next_retry_at: str | None,
    ) -> None:
        conn.execute(
            "UPDATE attempts SET recovery_count = recovery_count + 1,"
            " error_fingerprint = ?, next_retry_at = ? WHERE id = ?",
            (fingerprint, next_retry_at, attempt_id),
        )


class RunsRepo:
    def insert(self, conn: sqlite3.Connection, run: Run) -> None:
        conn.execute(
            "INSERT INTO runs (id, attempt_id, role, runner, provider, model, identity,"
            " session_id, state, checkpoint_ref, started_at, ended_at, stop_reason,"
            " worktree) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.attempt_id,
                run.role,
                run.runner,
                run.provider,
                run.model,
                run.identity,
                run.session_id,
                run.state.value,
                run.checkpoint_ref,
                run.started_at,
                run.ended_at,
                run.stop_reason,
                run.worktree,
            ),
        )

    def get(self, conn: sqlite3.Connection, run_id: str) -> Run:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run not found: {run_id}")
        return self._row_to_run(row)

    def set_worktree(self, conn: sqlite3.Connection, run_id: str, worktree: str) -> None:
        conn.execute("UPDATE runs SET worktree = ? WHERE id = ?", (worktree, run_id))

    def set_prompt_sha(self, conn: sqlite3.Connection, run_id: str, sha: str) -> None:
        conn.execute("UPDATE runs SET prompt_sha = ? WHERE id = ?", (sha, run_id))

    def update_state(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        state: RunState,
        *,
        checkpoint_ref: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        now = now_iso()
        conn.execute(
            "UPDATE runs SET state = ?, checkpoint_ref = COALESCE(?, checkpoint_ref),"
            " stop_reason = COALESCE(?, stop_reason),"
            " ended_at = CASE WHEN ? IN ('SUCCEEDED','TIMED_OUT','CRASHED','CANCELED')"
            " THEN ? ELSE ended_at END WHERE id = ?",
            (
                state.value,
                checkpoint_ref,
                stop_reason,
                state.value,
                now,
                run_id,
            ),
        )
        # A terminal run no longer holds execution rights: drop its lease in
        # the same transaction (ADR-004).
        if state not in RUN_ACTIVE_STATES:
            conn.execute("DELETE FROM leases WHERE run_id = ?", (run_id,))

    def has_active_run(
        self, conn: sqlite3.Connection, task_id: str, role: str
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM runs r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.task_id = ? AND r.role = ? AND r.state NOT IN"
            " ('SUCCEEDED','TIMED_OUT','CRASHED','CANCELED')",
            (task_id, role),
        ).fetchone()
        return row is not None

    def list_for_task(self, conn: sqlite3.Connection, task_id: str) -> list[Run]:
        rows = conn.execute(
            "SELECT r.* FROM runs r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.task_id = ? ORDER BY r.rowid",
            (task_id,),
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            role=str(row["role"]),
            runner=str(row["runner"]),
            provider=row["provider"],
            model=row["model"],
            identity=str(row["identity"]),
            session_id=str(row["session_id"]),
            state=RunState(str(row["state"])),
            checkpoint_ref=row["checkpoint_ref"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            stop_reason=row["stop_reason"],
            worktree=row["worktree"],
            prompt_sha=row["prompt_sha"],
        )


class SubmissionsRepo:
    def insert(self, conn: sqlite3.Connection, submission: Submission) -> None:
        conn.execute(
            "INSERT INTO submissions (id, attempt_id, submission_sha256, head_commit,"
            " git_tree_sha, manifest_json, frozen_at, invalidated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission.id,
                submission.attempt_id,
                submission.submission_sha256,
                submission.head_commit,
                submission.git_tree_sha,
                json.dumps(
                    submission.manifest,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                submission.frozen_at,
                submission.invalidated_at,
            ),
        )

    def get(self, conn: sqlite3.Connection, submission_id: str) -> Submission:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"submission not found: {submission_id}")
        return self._row_to_submission(row)

    def get_for_attempt(
        self, conn: sqlite3.Connection, attempt_id: str
    ) -> Submission | None:
        row = conn.execute(
            "SELECT * FROM submissions WHERE attempt_id = ? ORDER BY frozen_at DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        return self._row_to_submission(row) if row is not None else None

    def invalidate(self, conn: sqlite3.Connection, submission_id: str) -> None:
        conn.execute(
            "UPDATE submissions SET invalidated_at = ? WHERE id = ?",
            (now_iso(), submission_id),
        )

    @staticmethod
    def _row_to_submission(row: sqlite3.Row) -> Submission:
        return Submission(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            submission_sha256=str(row["submission_sha256"]),
            head_commit=str(row["head_commit"]),
            git_tree_sha=str(row["git_tree_sha"]),
            manifest=json.loads(str(row["manifest_json"])),
            frozen_at=str(row["frozen_at"]),
            invalidated_at=row["invalidated_at"],
        )


class ReviewsRepo:
    def insert(self, conn: sqlite3.Connection, review: Review) -> None:
        conn.execute(
            "INSERT INTO reviews (id, submission_id, run_id, result, report_ref,"
            " evidence_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                review.id,
                review.submission_id,
                review.run_id,
                review.result,
                review.report_ref,
                review.evidence_sha256,
                review.created_at,
            ),
        )

    def get_for_submission(
        self, conn: sqlite3.Connection, submission_id: str
    ) -> list[Review]:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE submission_id = ? ORDER BY created_at",
            (submission_id,),
        ).fetchall()
        return [self._row_to_review(row) for row in rows]

    @staticmethod
    def _row_to_review(row: sqlite3.Row) -> Review:
        return Review(
            id=str(row["id"]),
            submission_id=str(row["submission_id"]),
            run_id=str(row["run_id"]),
            result=str(row["result"]),
            report_ref=str(row["report_ref"]),
            evidence_sha256=str(row["evidence_sha256"]),
            created_at=str(row["created_at"]),
        )


class ArtifactsRepo:
    def insert(self, conn: sqlite3.Connection, artifact: Artifact) -> None:
        conn.execute(
            "INSERT INTO artifacts (id, run_id, submission_id, kind, uri, sha256,"
            " size_bytes, redaction_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.id,
                artifact.run_id,
                artifact.submission_id,
                artifact.kind,
                artifact.uri,
                artifact.sha256,
                artifact.size_bytes,
                artifact.redaction_status,
                artifact.created_at,
            ),
        )

    def for_submission(
        self, conn: sqlite3.Connection, submission_id: str
    ) -> list[Artifact]:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE submission_id = ? ORDER BY kind",
            (submission_id,),
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=str(row["id"]),
            run_id=row["run_id"],
            submission_id=row["submission_id"],
            kind=str(row["kind"]),
            uri=str(row["uri"]),
            sha256=str(row["sha256"]),
            size_bytes=row["size_bytes"],
            redaction_status=str(row["redaction_status"]),
            created_at=str(row["created_at"]),
        )


class OperationsRepo:
    def get(self, conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT result_json FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        value: Any = json.loads(str(row["result_json"]))
        if not isinstance(value, dict):
            return None
        return value

    def insert(
        self,
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        command: str,
        aggregate_type: str,
        aggregate_id: str,
        result: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO operations (operation_id, command, aggregate_type, aggregate_id,"
            " result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                command,
                aggregate_type,
                aggregate_id,
                json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                now_iso(),
            ),
        )
