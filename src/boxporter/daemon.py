"""24x7 porter daemon loop.

The daemon owns the scheduler, lease manager, watchdog and runners. It
never depends on a browser or user session (ADR-012): closing the Web
page does not change task or run lifecycle.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from pathlib import Path

from boxporter.core.lease import LeaseManager
from boxporter.core.reconcile import Reconcile
from boxporter.core.recovery import RecoveryEngine
from boxporter.core.scheduler import Scheduler, SchedulingPolicy, TickResult
from boxporter.core.watchdog import WatchDog
from boxporter.runners.base import RunnerRegistry
from boxporter.storage.store import Store


@dataclass(frozen=True)
class DaemonConfig:
    tick_seconds: float = 15.0
    heartbeat_seconds: float = 30.0
    lease_ttl_seconds: int = 300
    worktrees_root: Path | None = None


class PorterDaemon:
    def __init__(
        self,
        store: Store,
        runners: RunnerRegistry,
        policy: SchedulingPolicy | None = None,
        config: DaemonConfig | None = None,
    ):
        self.store = store
        self.runners = runners
        self.config = config or DaemonConfig()
        self.leases = LeaseManager(store, ttl_seconds=self.config.lease_ttl_seconds)
        self.watchdog = WatchDog(store, self.leases)
        self.recovery = RecoveryEngine(store)
        self.scheduler = Scheduler(
            store,
            runners,
            self.leases,
            self.watchdog,
            self.recovery,
            policy,
            worktrees_root=self.config.worktrees_root,
        )
        self.reconcile = Reconcile(store, self.leases)
        self._stop_requested = False

    def startup(self) -> None:
        """Reconciliation pass before accepting new scheduling (ADR-014).
        Also persists runner capabilities so the Web console and the daemon
        share one source of truth (ADR-015)."""
        self.reconcile.run(handles=self.scheduler.handles)
        capabilities = [
            self.runners.get(name).capabilities().__dict__
            for name in self.runners.names()
        ]
        with self.store.db.transaction():
            self.store.settings.set(
                self.store.db.conn, "runner_capabilities", capabilities
            )

    def run_once(self) -> TickResult:
        result = self.scheduler.tick()
        self.scheduler.heartbeat_all()
        return result

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop_requested", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop_requested", True))
        self.startup()
        last_heartbeat = 0.0
        last_policy_sync = 0.0
        while not self._stop_requested:
            now = time.monotonic()
            if now - last_policy_sync >= self.config.tick_seconds:
                self._sync_policy()
                last_policy_sync = now
            self.scheduler.tick()
            if now - last_heartbeat >= self.config.heartbeat_seconds:
                self.scheduler.heartbeat_all()
                last_heartbeat = now
            time.sleep(self.config.tick_seconds)

    def _sync_policy(self) -> None:
        """Re-read the operating policy from settings so remote mode
        changes (Web console) apply without restarting the daemon."""
        from boxporter.core.policy import PolicyService

        self.scheduler.apply_policy(PolicyService(self.store).read())

    def stop(self) -> None:
        self._stop_requested = True
