"""External-condition probes for BLOCKED tasks (plan §9.1, §10.6).

Probes are deterministic machine checks (a command exit code), never model
calls. A task stays BLOCKED with zero retries until a probe succeeds; the
scheduler runs due probes on every tick.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from boxporter.application.commands import UnblockTask
from boxporter.core.clock import parse_iso_utc
from boxporter.storage.store import Store


class ProbeRunner:
    def __init__(self, store: Store, now: datetime | None = None):
        self.store = store
        self._now = now or datetime.now(timezone.utc)

    def run_due(self) -> int:
        """Run due probes; returns how many blockers were resolved."""
        resolved = 0
        conn = self.store.db.conn
        for blocker in self.store.blockers.all_open(conn):
            if blocker.next_probe_at is not None and parse_iso_utc(
                blocker.next_probe_at
            ) > self._now:
                continue
            if not blocker.probe_command:
                continue  # no machine probe; waits for a human
            result = subprocess.run(
                list(blocker.probe_command),
                capture_output=True,
                check=False,
                timeout=60,
            )
            if result.returncode == 0:
                with self.store.db.transaction():
                    self.store.blockers.resolve(conn, blocker.id)
                outcome = self.store.execute(
                    UnblockTask(
                        task_id=blocker.task_id,
                        note=f"probe succeeded: {blocker.id}",
                        actor_type="daemon",
                    ),
                    operation_id=f"probe-unblock-{blocker.id}",
                )
                if outcome.ok:
                    resolved += 1
            else:
                next_at = (
                    self._now + timedelta(seconds=blocker.probe_interval_seconds)
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
                with self.store.db.transaction():
                    self.store.blockers.update_probe(conn, blocker.id, next_at)
        return resolved
