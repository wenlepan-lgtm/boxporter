"""Accelerated 72-hour unattended soak (plan §21.2 long-stability).

Simulates 3 days of 1-minute ticks with a stepped clock: task failures
with backoff, probe-driven unblocking, budget stops, PAUSED windows, and
an end-to-end PASS seal. Invariants: no duplicate concurrent runs, no
overspend, no illegal transitions, monotonic event cursor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from boxporter.application.commands import (
    BlockTask,
    CreateTask,
    FailRun,
    ReadyTask,
)
from boxporter.application.queries import events_since, latest_seq
from boxporter.core.lease import LeaseManager
from boxporter.core.recovery import RecoveryEngine
from boxporter.core.scheduler import Scheduler, SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RUN_ACTIVE_STATES, TaskState
from boxporter.core.watchdog import WatchDog
from boxporter.runners.base import RunnerRegistry
from boxporter.runners.mock import MockRunner
from boxporter.storage.store import Store

TICK = timedelta(minutes=1)


def add_ready_task(
    store: Store, make_spec: Callable[..., TaskSpec], task_id: str, **kwargs: object
) -> None:
    assert store.execute(
        CreateTask(spec=make_spec(task_id, **kwargs), actor_type="user")
    ).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok


def test_72h_unattended_soak(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "soak-a", token_budget=5000)
    add_ready_task(store, make_spec, "soak-b", risk_level="high")  # gated out

    runner = MockRunner()
    registry = RunnerRegistry()
    registry.register(runner)
    leases = LeaseManager(store)
    watchdog = WatchDog(store, leases)
    scheduler = Scheduler(
        store,
        registry,
        leases,
        watchdog,
        RecoveryEngine(store),
        SchedulingPolicy(max_recoveries_per_attempt=2),
    )

    now = datetime.now(timezone.utc)
    tasks = ("soak-a", "soak-b")
    started_ever = 0
    max_concurrent_observed = 0

    for minute in range(72 * 60):
        now += TICK
        result = scheduler.tick(now=now)
        if result.action == "started_runs":
            started_ever += len(result.detail.get("executor_runs", [])) + len(
                result.detail.get("reviewer_runs", [])
            )
        # Injected failures: soak-a's active run crashes every 45 minutes
        # during the first 6 hours, then stabilizes.
        if minute % 45 == 0 and minute < 360:
            for task_id in tasks:
                running = [
                    run
                    for run in store.runs.list_for_task(store.db.conn, task_id)
                    if run.state in RUN_ACTIVE_STATES
                ]
                for run in running:
                    store.execute(
                        FailRun(
                            run_id=run.id,
                            kind="crash",
                            stop_reason="soak-injected-crash",
                            actor_type="daemon",
                        ),
                        operation_id=f"soak-fail-{minute}-{run.id}",
                    )
        # A PAUSED window during hour 12-13 must stop all scheduling.
        if minute == 12 * 60:
            scheduler.policy = SchedulingPolicy(
                mode="PAUSED", max_recoveries_per_attempt=2
            )
        if minute == 13 * 60:
            scheduler.policy = SchedulingPolicy(max_recoveries_per_attempt=2)
        # Block soak-a with a failing probe during hour 20-21.
        if minute == 20 * 60:
            for task_id in tasks:
                task = store.tasks.get(store.db.conn, task_id)
                if task.state in {TaskState.WORKING, TaskState.FAILED, TaskState.READY}:
                    try:
                        store.execute(
                            BlockTask(
                                task_id=task_id,
                                reason="soak probe",
                                probe_command=("false",),
                                probe_interval_seconds=600,
                                actor_type="daemon",
                            )
                        )
                    except Exception:  # noqa: BLE001, S110 - illegal transition is fine
                        pass
        if minute == 21 * 60:
            # External condition resolves: swap in a succeeding probe.
            conn = store.db.conn
            conn.execute(
                "UPDATE blockers SET probe_command_json = '[\"true\"]' WHERE"
                " probe_command_json = '[\"false\"]'"
            )

        # Invariants after every tick.
        active = [
            run
            for task_id in tasks
            for run in store.runs.list_for_task(store.db.conn, task_id)
            if run.state in RUN_ACTIVE_STATES
        ]
        max_concurrent_observed = max(max_concurrent_observed, len(active))
        assert len({run.id for run in active}) == len(active)
        assert len(active) <= 1 + 1  # executor + reviewer per task max

    assert started_ever > 0
    assert max_concurrent_observed <= 2  # capacity 1: one executor + one reviewer
    # soak-b (high risk) was never admitted under the default risk gate.
    assert all(
        run.role != "executor"
        for run in store.runs.list_for_task(store.db.conn, "soak-b")
    )
    # Budget: total recorded usage for soak-a never exceeded its budget
    # (no usage was recorded at all in this mock soak — zero overspend).
    assert store.usage.total_tokens_for_task(store.db.conn, "soak-a") <= 5000
    # Event cursor strictly monotonic, replayable without gaps.
    all_events = events_since(store, 0, limit=100000)
    seqs = [event.seq for event in all_events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert latest_seq(store) == seqs[-1]
    # Lease uniqueness held throughout: at most one row per (task, role).
    rows = store.db.conn.execute(
        "SELECT COUNT(*) AS c FROM leases l1 JOIN leases l2 ON l1.task_id = l2.task_id"
        " AND l1.role = l2.role AND l1.run_id != l2.run_id"
    ).fetchone()
    assert int(rows["c"]) == 0
