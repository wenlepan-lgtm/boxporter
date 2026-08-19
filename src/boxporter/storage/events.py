"""Append-only event log with monotonic sequence cursor (ADR-002, ADR-013).

Every state change and supervision decision is appended here, in the same
transaction as the state mutation. ``seq`` is the Web event cursor.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from boxporter.core.clock import now_iso
from boxporter.core.ids import new_id
from boxporter.core.schemas import EventRecord


class EventType:
    PROJECT_CREATED = "PROJECT_CREATED"
    GOAL_CREATED = "GOAL_CREATED"
    TASK_CREATED = "TASK_CREATED"
    TASK_READY = "TASK_READY"
    TASK_WORKING = "TASK_WORKING"
    TASK_REVIEW_PENDING = "TASK_REVIEW_PENDING"
    TASK_PASS = "TASK_PASS"
    TASK_DONE = "TASK_DONE"
    TASK_REVISE = "TASK_REVISE"
    TASK_BLOCKED = "TASK_BLOCKED"
    TASK_UNBLOCKED = "TASK_UNBLOCKED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELED = "TASK_CANCELED"
    ATTEMPT_CREATED = "ATTEMPT_CREATED"
    ATTEMPT_SUBMITTED = "ATTEMPT_SUBMITTED"
    ATTEMPT_REVISED = "ATTEMPT_REVISED"
    ATTEMPT_PASSED = "ATTEMPT_PASSED"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    RUN_CREATED = "RUN_CREATED"
    RUN_STARTED = "RUN_STARTED"
    RUN_RUNNING = "RUN_RUNNING"
    RUN_CHECKPOINTING = "RUN_CHECKPOINTING"
    RUN_SUCCEEDED = "RUN_SUCCEEDED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CRASHED = "RUN_CRASHED"
    RUN_CANCELED = "RUN_CANCELED"
    RUN_WAITING = "RUN_WAITING"
    RUN_STALLED = "RUN_STALLED"
    RUN_TIMED_OUT = "RUN_TIMED_OUT"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    FILE_CHANGED = "FILE_CHANGED"
    COMMAND_FINISHED = "COMMAND_FINISHED"
    TOOL_RESULT = "TOOL_RESULT"
    ERROR_RECORDED = "ERROR_RECORDED"
    PROGRESS_SIGNAL = "PROGRESS_SIGNAL"
    WATCHDOG_FINDING = "WATCHDOG_FINDING"
    RECOVERY_APPLIED = "RECOVERY_APPLIED"
    SUBMISSION_FROZEN = "SUBMISSION_FROZEN"
    SUBMISSION_INVALIDATED = "SUBMISSION_INVALIDATED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    EVIDENCE_SEALED = "EVIDENCE_SEALED"
    SECURITY_FINDING = "SECURITY_FINDING"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    APPROVAL_CONSUMED = "APPROVAL_CONSUMED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    MEMORY_ADDED = "MEMORY_ADDED"
    RECONCILE_NO_REATTACH = "RECONCILE_NO_REATTACH"
    RUN_OBSERVED_TERMINAL = "RUN_OBSERVED_TERMINAL"


class ActorType:
    SYSTEM = "system"
    USER = "user"
    DAEMON = "daemon"
    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"


class EventStore:
    def append(
        self,
        conn: sqlite3.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        actor_type: str,
        payload: dict[str, Any],
        actor_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        event_id = new_id("evt")
        occurred_at = now_iso()
        conn.execute(
            "INSERT INTO events (event_id, aggregate_type, aggregate_id, event_type,"
            " actor_type, actor_id, payload_json, occurred_at, causation_id,"
            " correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                aggregate_type,
                aggregate_id,
                event_type,
                actor_type,
                actor_id,
                json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                occurred_at,
                causation_id,
                correlation_id,
            ),
        )
        row = conn.execute("SELECT seq FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return int(row["seq"])

    def since(self, conn: sqlite3.Connection, after_seq: int, limit: int = 500) -> list[EventRecord]:
        rows = conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (after_seq, limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def for_aggregate(
        self,
        conn: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
        after_seq: int = 0,
    ) -> list[EventRecord]:
        rows = conn.execute(
            "SELECT * FROM events WHERE aggregate_type = ? AND aggregate_id = ?"
            " AND seq > ? ORDER BY seq",
            (aggregate_type, aggregate_id, after_seq),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            seq=int(row["seq"]),
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            actor_type=str(row["actor_type"]),
            actor_id=row["actor_id"],
            payload=json.loads(str(row["payload_json"])),
            occurred_at=str(row["occurred_at"]),
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
        )
