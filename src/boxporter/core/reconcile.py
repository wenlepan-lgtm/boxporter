"""Host-restart reconciliation (ADR-002, ADR-014).

On startup, reconcile active runs against leases and processes:
- missing or expired lease -> the run lost execution rights -> crash it;
- dead pid -> crash it;
- live lease + live pid -> re-attach the handle so supervision continues.

Reconciliation never silently resumes a run whose lease lapsed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from boxporter.application.base import CommandFailed
from boxporter.application.commands import FailRun
from boxporter.core.lease import LeaseManager
from boxporter.core.state import RUN_ACTIVE_STATES
from boxporter.runners.base import RunHandle
from boxporter.storage.events import ActorType
from boxporter.storage.store import Store


@dataclass(frozen=True)
class ReconcileReport:
    crashed_runs: tuple[str, ...] = ()
    reattached_runs: tuple[str, ...] = ()
    untouched_runs: tuple[str, ...] = ()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class Reconcile:
    def __init__(
        self,
        store: Store,
        lease_manager: LeaseManager,
        now: datetime | None = None,
    ):
        self.store = store
        self.leases = lease_manager
        self._now = now or datetime.now(timezone.utc)

    def run(self, handles: dict[str, RunHandle] | None = None) -> ReconcileReport:
        crashed: list[str] = []
        reattached: list[str] = []
        untouched: list[str] = []
        conn = self.store.db.conn
        placeholders = ",".join("?" for _ in RUN_ACTIVE_STATES)
        rows = conn.execute(
            f"SELECT id FROM runs WHERE state IN ({placeholders})",
            tuple(state.value for state in RUN_ACTIVE_STATES),
        ).fetchall()
        for row in rows:
            run_id = str(row["id"])
            lease = self.leases.get(run_id)
            if lease is None or lease.expired(self._now):
                self._crash(run_id, "reconciliation: lease missing or expired")
                crashed.append(run_id)
                continue
            if lease.pid is None:
                # No pid-backed process exists for this runner session
                # (e.g. OpenHands SDK threads). Re-attaching would fabricate
                # a handle we cannot actually observe: crash it honestly.
                self._crash(
                    run_id, "reconciliation: runner session cannot be re-attached"
                )
                self._record_event(run_id, "reconciliation: no re-attachable session")
                crashed.append(run_id)
                continue
            if not _pid_alive(lease.pid):
                self._crash(run_id, "reconciliation: process dead")
                crashed.append(run_id)
                continue
            if handles is not None:
                handles[run_id] = RunHandle(
                    run_id=run_id,
                    runtime_id=f"pid://{lease.pid}",
                    pid=lease.pid,
                )
                reattached.append(run_id)
            else:
                untouched.append(run_id)
        return ReconcileReport(
            crashed_runs=tuple(crashed),
            reattached_runs=tuple(reattached),
            untouched_runs=tuple(untouched),
        )

    def _record_event(self, run_id: str, reason: str) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=run_id,
                event_type="RECONCILE_NO_REATTACH",
                actor_type="daemon",
                payload={"reason": reason},
            )

    def _crash(self, run_id: str, reason: str) -> None:
        try:
            self.store.execute(
                FailRun(
                    run_id=run_id,
                    kind="crash",
                    stop_reason=reason,
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"reconcile-fail-{run_id}",
            )
        except CommandFailed:
            pass
