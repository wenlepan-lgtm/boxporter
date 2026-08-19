"""Explicit leases with fencing tokens (ADR-004).

Invariants:
- At most one *valid* lease per (task, role), enforced by a unique index.
- ``fencing_token`` increases monotonically per (task, role); a stale token
  cannot heartbeat or release the new holder's lease.
- Heartbeats come only from the lease manager, never from file mtimes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from boxporter.core.clock import now_iso, parse_iso_utc
from boxporter.core.errors import BoxPorterError
from boxporter.storage.store import Store


class LeaseConflict(BoxPorterError):
    """A valid lease already exists for this task and role."""


class StaleLeaseError(BoxPorterError):
    """The lease is expired, or the fencing token no longer matches."""


@dataclass(frozen=True)
class Lease:
    run_id: str
    task_id: str
    role: str
    owner_instance: str
    fencing_token: int
    pid: int | None
    heartbeat_at: str
    expires_at: str

    def expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return parse_iso_utc(self.expires_at) <= current

    def stale_heartbeat(
        self, now: datetime | None = None, threshold: int = 120
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        return current - parse_iso_utc(self.heartbeat_at) > timedelta(seconds=threshold)


class LeaseManager:
    def __init__(
        self,
        store: Store,
        ttl_seconds: int = 300,
        owner_instance: str = "porter-macmini-01",
    ):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.owner_instance = owner_instance

    def acquire(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        now: datetime | None = None,
    ) -> Lease:
        """Take the lease for a run. Raises LeaseConflict if a valid lease
        exists for the same (task, role). Expired leases are replaced."""
        conn = self.store.db.conn
        run = self.store.runs.get(conn, run_id)
        attempt = self.store.attempts.get(conn, run.attempt_id)
        now_value = now or datetime.now(timezone.utc)
        with self.store.db.transaction():
            existing = self._get_for(conn, attempt.task_id, run.role)
            if existing is not None and not existing.expired(now_value):
                raise LeaseConflict(
                    f"lease already held for task {attempt.task_id} role {run.role}"
                    f" (token {existing.fencing_token})"
                )
            token = self._next_token(conn)
            if existing is not None:
                conn.execute("DELETE FROM leases WHERE run_id = ?", (existing.run_id,))
            expires = (now_value + timedelta(seconds=self.ttl_seconds)).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO leases (run_id, task_id, role, owner_instance,"
                " fencing_token, pid, heartbeat_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    attempt.task_id,
                    run.role,
                    self.owner_instance,
                    token,
                    pid,
                    now_iso(),
                    expires,
                ),
            )
        lease = self.get(run_id)
        assert lease is not None
        return lease

    def heartbeat(
        self,
        run_id: str,
        fencing_token: int,
        *,
        now: datetime | None = None,
    ) -> Lease:
        """Extend the lease. Fails for expired leases or stale tokens."""
        conn = self.store.db.conn
        lease = self.get(run_id)
        if lease is None:
            raise StaleLeaseError(f"no lease for run {run_id}")
        if lease.fencing_token != fencing_token:
            raise StaleLeaseError(
                f"fencing token mismatch for run {run_id}: "
                f"got {fencing_token}, expected {lease.fencing_token}"
            )
        now_value = now or datetime.now(timezone.utc)
        if lease.expired(now_value):
            raise StaleLeaseError(f"lease expired for run {run_id}")
        expires = (now_value + timedelta(seconds=self.ttl_seconds)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        with self.store.db.transaction():
            conn.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE run_id = ?",
                (now_iso(), expires, run_id),
            )
        refreshed = self.get(run_id)
        assert refreshed is not None
        return refreshed

    def release(self, run_id: str, fencing_token: int) -> None:
        """Release the lease. A stale token cannot release a newer lease."""
        conn = self.store.db.conn
        lease = self.get(run_id)
        if lease is None:
            return
        if lease.fencing_token != fencing_token:
            raise StaleLeaseError(
                f"fencing token mismatch for run {run_id}: "
                f"got {fencing_token}, expected {lease.fencing_token}"
            )
        with self.store.db.transaction():
            conn.execute("DELETE FROM leases WHERE run_id = ?", (run_id,))

    def get(self, run_id: str) -> Lease | None:
        row = self.store.db.conn.execute(
            "SELECT * FROM leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_lease(row) if row is not None else None

    def expired_leases(self, now: datetime | None = None) -> list[Lease]:
        current = now or datetime.now(timezone.utc)
        rows = self.store.db.conn.execute(
            "SELECT * FROM leases ORDER BY expires_at"
        ).fetchall()
        return [
            lease
            for lease in (self._row_to_lease(row) for row in rows)
            if lease.expired(current)
        ]

    def _get_for(
        self, conn: sqlite3.Connection, task_id: str, role: str
    ) -> Lease | None:
        row = conn.execute(
            "SELECT * FROM leases WHERE task_id = ? AND role = ?", (task_id, role)
        ).fetchone()
        return self._row_to_lease(row) if row is not None else None

    @staticmethod
    def _next_token(conn: sqlite3.Connection) -> int:
        # Global monotonic counter: never reused across replacements.
        row = conn.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) + 1 AS token FROM leases"
        ).fetchone()
        return int(row["token"])

    @staticmethod
    def _row_to_lease(row: sqlite3.Row) -> Lease:
        return Lease(
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            role=str(row["role"]),
            owner_instance=str(row["owner_instance"]),
            fencing_token=int(row["fencing_token"]),
            pid=row["pid"],
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["expires_at"]),
        )
