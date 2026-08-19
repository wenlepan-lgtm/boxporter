"""WatchDog: deterministic supervision over leases, processes and progress.

Zero-model: every check is a database / OS read. Findings trigger
recovery classification, never direct restarts (ADR-004).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from boxporter.core.clock import parse_iso_utc
from boxporter.core.lease import LeaseManager
from boxporter.core.state import RUN_ACTIVE_STATES, RunState
from boxporter.storage.events import EventType
from boxporter.storage.store import Store


class FindingKind:
    LEASE_MISSING = "LEASE_MISSING"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    STALE_HEARTBEAT = "STALE_HEARTBEAT"
    PROCESS_DEAD = "PROCESS_DEAD"
    RUN_CRASHED = "RUN_CRASHED"
    NO_PROGRESS = "NO_PROGRESS"


@dataclass(frozen=True)
class WatchFinding:
    run_id: str
    kind: str
    detail: dict[str, object]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class WatchDog:
    def __init__(
        self,
        store: Store,
        lease_manager: LeaseManager,
        *,
        heartbeat_stale_seconds: int = 120,
        no_progress_seconds: int = 600,
        now: datetime | None = None,
    ):
        self.store = store
        self.leases = lease_manager
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.no_progress_seconds = no_progress_seconds
        self._now = now or datetime.now(timezone.utc)

    def check(self) -> list[WatchFinding]:
        findings: list[WatchFinding] = []
        conn = self.store.db.conn
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATES)
        rows = conn.execute(
            f"SELECT id FROM runs WHERE state IN ({placeholders})",
            tuple(state.value for state in RUN_ACTIVE_STATES),
        ).fetchall()
        for row in rows:
            run = self.store.runs.get(conn, str(row["id"]))
            lease = self.leases.get(run.id)
            if lease is None:
                findings.append(
                    WatchFinding(
                        run.id,
                        FindingKind.LEASE_MISSING,
                        {"state": run.state.value},
                    )
                )
                continue
            if lease.expired(self._now):
                findings.append(
                    WatchFinding(
                        run.id,
                        FindingKind.LEASE_EXPIRED,
                        {"expires_at": lease.expires_at},
                    )
                )
                continue
            if lease.stale_heartbeat(
                self._now, threshold=self.heartbeat_stale_seconds
            ):
                findings.append(
                    WatchFinding(
                        run.id,
                        FindingKind.STALE_HEARTBEAT,
                        {"heartbeat_at": lease.heartbeat_at},
                    )
                )
            if lease.pid is not None and not _pid_alive(lease.pid):
                findings.append(
                    WatchFinding(
                        run.id,
                        FindingKind.PROCESS_DEAD,
                        {"pid": lease.pid},
                    )
                )
            # Effective-progress check: a RUNNING run with no machine signal
            # inside the window is diagnosed, never killed directly (ADR-004).
            if run.state == RunState.RUNNING and self._no_progress(run):
                findings.append(
                    WatchFinding(
                        run.id,
                        FindingKind.NO_PROGRESS,
                        {"no_progress_seconds": self.no_progress_seconds},
                    )
                )
        return findings

    def _no_progress(self, run: object) -> bool:
        from datetime import timedelta

        from boxporter.core.schemas import Run

        assert isinstance(run, Run)
        if run.started_at is None:
            return False
        if parse_iso_utc(run.started_at) > self._now - timedelta(
            seconds=self.no_progress_seconds
        ):
            return False  # still inside the initial grace window
        conn = self.store.db.conn
        row = conn.execute(
            "SELECT MAX(occurred_at) AS latest FROM events WHERE aggregate_type = 'run'"
            " AND aggregate_id = ? AND event_type = 'PROGRESS_SIGNAL'",
            (run.id,),
        ).fetchone()
        latest = row["latest"] if row is not None else None
        if latest is None:
            return True
        return parse_iso_utc(str(latest)) <= self._now - timedelta(
            seconds=self.no_progress_seconds
        )

    def record_finding(self, finding: WatchFinding) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=finding.run_id,
                event_type=EventType.WATCHDOG_FINDING,
                actor_type="daemon",
                payload={"kind": finding.kind, "detail": finding.detail},
            )
