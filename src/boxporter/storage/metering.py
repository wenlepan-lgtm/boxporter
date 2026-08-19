"""Usage metering, external blockers, and deduplicated notifications."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from boxporter.core.clock import now_iso, parse_iso_utc
from boxporter.core.errors import NotFoundError
from boxporter.core.ids import new_id


@dataclass(frozen=True)
class UsageRecord:
    id: str
    run_id: str
    tokens_in: int
    tokens_out: int
    cost: float
    tool_calls: int
    recorded_at: str


class UsageRepo:
    def insert(self, conn: sqlite3.Connection, record: UsageRecord) -> None:
        conn.execute(
            "INSERT INTO usage (id, run_id, tokens_in, tokens_out, cost, tool_calls,"
            " recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.run_id,
                record.tokens_in,
                record.tokens_out,
                record.cost,
                record.tool_calls,
                record.recorded_at,
            ),
        )

    def total_tokens_for_run(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS total FROM usage"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["total"])

    def total_tokens_for_task(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(u.tokens_in + u.tokens_out), 0) AS total FROM usage u"
            " JOIN runs r ON r.id = u.run_id"
            " JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["total"])

    def total_tokens_since(
        self, conn: sqlite3.Connection, since: str
    ) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS total FROM usage"
            " WHERE recorded_at >= ?",
            (since,),
        ).fetchone()
        return int(row["total"])

    def usage_by_project_since(
        self, conn: sqlite3.Connection, since: str
    ) -> dict[str, int]:
        rows = conn.execute(
            "SELECT t.project_id, COALESCE(SUM(u.tokens_in + u.tokens_out), 0) AS total"
            " FROM usage u JOIN runs r ON r.id = u.run_id"
            " JOIN attempts a ON a.id = r.attempt_id"
            " JOIN tasks t ON t.id = a.task_id"
            " WHERE u.recorded_at >= ? GROUP BY t.project_id",
            (since,),
        ).fetchall()
        return {str(row["project_id"]): int(row["total"]) for row in rows}


@dataclass(frozen=True)
class Blocker:
    id: str
    task_id: str
    reason: str
    probe_command: tuple[str, ...]
    probe_interval_seconds: int
    next_probe_at: str | None
    created_at: str
    resolved_at: str | None


class BlockersRepo:
    def insert(self, conn: sqlite3.Connection, blocker: Blocker) -> None:
        conn.execute(
            "INSERT INTO blockers (id, task_id, reason, probe_command_json,"
            " probe_interval_seconds, next_probe_at, created_at, resolved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                blocker.id,
                blocker.task_id,
                blocker.reason,
                json.dumps(list(blocker.probe_command), ensure_ascii=True),
                blocker.probe_interval_seconds,
                blocker.next_probe_at,
                blocker.created_at,
                blocker.resolved_at,
            ),
        )

    def list_open_for_task(
        self, conn: sqlite3.Connection, task_id: str
    ) -> list[Blocker]:
        rows = conn.execute(
            "SELECT * FROM blockers WHERE task_id = ? AND resolved_at IS NULL",
            (task_id,),
        ).fetchall()
        return [self._row_to_blocker(row) for row in rows]

    def resolve(self, conn: sqlite3.Connection, blocker_id: str) -> None:
        conn.execute(
            "UPDATE blockers SET resolved_at = ? WHERE id = ?",
            (now_iso(), blocker_id),
        )

    def update_probe(self, conn: sqlite3.Connection, blocker_id: str, next_at: str) -> None:
        conn.execute(
            "UPDATE blockers SET next_probe_at = ? WHERE id = ?",
            (next_at, blocker_id),
        )

    def all_open(self, conn: sqlite3.Connection) -> list[Blocker]:
        rows = conn.execute(
            "SELECT * FROM blockers WHERE resolved_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [self._row_to_blocker(row) for row in rows]

    @staticmethod
    def _row_to_blocker(row: sqlite3.Row) -> Blocker:
        raw = json.loads(str(row["probe_command_json"]))
        return Blocker(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            reason=str(row["reason"]),
            probe_command=tuple(str(item) for item in raw),
            probe_interval_seconds=int(row["probe_interval_seconds"]),
            next_probe_at=row["next_probe_at"],
            created_at=str(row["created_at"]),
            resolved_at=row["resolved_at"],
        )


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str | None
    run_id: str | None
    action: str
    target: str
    risk_level: str
    max_uses: int
    used_count: int
    expires_at: str
    status: str
    requested_by: str | None
    decided_by: str | None
    decided_at: str | None
    created_at: str


APPROVAL_ACTIVE = frozenset({"pending", "approved"})


class ApprovalsRepo:
    def insert(self, conn: sqlite3.Connection, approval: Approval) -> None:
        conn.execute(
            "INSERT INTO approvals (id, task_id, run_id, action, target, risk_level,"
            " max_uses, used_count, expires_at, status, requested_by, decided_by,"
            " decided_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval.id,
                approval.task_id,
                approval.run_id,
                approval.action,
                approval.target,
                approval.risk_level,
                approval.max_uses,
                approval.used_count,
                approval.expires_at,
                approval.status,
                approval.requested_by,
                approval.decided_by,
                approval.decided_at,
                approval.created_at,
            ),
        )

    def get(self, conn: sqlite3.Connection, approval_id: str) -> Approval:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"approval not found: {approval_id}")
        return self._row_to_approval(row)

    def list_for_task(self, conn: sqlite3.Connection, task_id: str) -> list[Approval]:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def list_all(self, conn: sqlite3.Connection) -> list[Approval]:
        rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC").fetchall()
        return [self._row_to_approval(row) for row in rows]

    def consume(
        self, conn: sqlite3.Connection, approval_id: str, *, by: str, at: str
    ) -> bool:
        """Mark one use of an approved approval; returns False when expired
        or exhausted (does not mutate in that case)."""
        approval = self.get(conn, approval_id)
        if approval.status != "approved" or parse_iso_utc(
            approval.expires_at
        ) <= parse_iso_utc(at):
            return False
        if approval.used_count >= approval.max_uses:
            return False
        conn.execute(
            "UPDATE approvals SET used_count = used_count + 1 WHERE id = ?",
            (approval_id,),
        )
        return True

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> Approval:
        return Approval(
            id=str(row["id"]),
            task_id=row["task_id"],
            run_id=row["run_id"],
            action=str(row["action"]),
            target=str(row["target"]),
            risk_level=str(row["risk_level"]),
            max_uses=int(row["max_uses"]),
            used_count=int(row["used_count"]),
            expires_at=str(row["expires_at"]),
            status=str(row["status"]),
            requested_by=row["requested_by"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True)
class MemoryItem:
    id: str
    project_id: str
    kind: str
    content: str
    source: str
    source_ref: str | None
    expires_at: str | None
    created_at: str


MEMORY_SOURCES = frozenset(
    {"pass-evidence", "user-confirmed", "repo-fact", "adr"}
)


class MemoryRepo:
    def insert(self, conn: sqlite3.Connection, item: MemoryItem) -> None:
        if item.source not in MEMORY_SOURCES:
            raise ValueError(f"memory source must be one of {sorted(MEMORY_SOURCES)}")
        conn.execute(
            "INSERT INTO memory_items (id, project_id, kind, content, source,"
            " source_ref, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.project_id,
                item.kind,
                item.content,
                item.source,
                item.source_ref,
                item.expires_at,
                item.created_at,
            ),
        )

    def list_for_project(
        self, conn: sqlite3.Connection, project_id: str
    ) -> list[MemoryItem]:
        rows = conn.execute(
            "SELECT * FROM memory_items WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def get(self, conn: sqlite3.Connection, memory_id: str) -> MemoryItem:
        row = conn.execute(
            "SELECT * FROM memory_items WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"memory item not found: {memory_id}")
        return self._row_to_memory(row)

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            source=str(row["source"]),
            source_ref=row["source_ref"],
            expires_at=row["expires_at"],
            created_at=str(row["created_at"]),
        )


class NotificationsRepo:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        dedup_key: str,
        payload: dict[str, Any],
        channel: str = "log",
    ) -> bool:
        """Insert a notification unless the dedup key already exists.
        Returns True when a new notification was created."""
        existing = conn.execute(
            "SELECT 1 FROM notifications WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO notifications (id, kind, dedup_key, payload_json, channel,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id("ntf"),
                kind,
                dedup_key,
                json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                channel,
                now_iso(),
            ),
        )
        return True

    def list_since(self, conn: sqlite3.Connection, since: str | None = None) -> list[dict[str, Any]]:
        if since is None:
            rows = conn.execute("SELECT * FROM notifications ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE created_at >= ? ORDER BY created_at DESC",
                (since,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "dedup_key": str(row["dedup_key"]),
                "payload": json.loads(str(row["payload_json"])),
                "channel": str(row["channel"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
