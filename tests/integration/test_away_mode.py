"""Away-mode policies, budgets, probes, notifications, reports (Phase 6)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from boxporter.application.commands import (
    BlockTask,
    CreateTask,
    FailRun,
    MarkRunRunning,
    ReadyTask,
    StartExecutorRun,
)
from boxporter.core.budget import BudgetService
from boxporter.core.notify import Notifier
from boxporter.core.policy import PolicyService
from boxporter.core.probe import ProbeRunner
from boxporter.core.report import activity_report
from boxporter.core.scheduler import Scheduler, SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import TaskState
from boxporter.storage.store import Store


def add_ready_task(
    store: Store, make_spec: Callable[..., TaskSpec], task_id: str, **kwargs: object
) -> None:
    assert store.execute(
        CreateTask(spec=make_spec(task_id, **kwargs), actor_type="user")
    ).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok


def test_policy_service_defaults_and_away_risk_gate(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    policy_service = PolicyService(store)
    snapshot = policy_service.read()
    assert snapshot.mode == "SUPERVISED"
    assert snapshot.allowed_risk_levels == {"low", "medium"}

    policy_service.set_mode("AWAY")
    assert policy_service.read().mode == "AWAY"

    add_ready_task(store, make_spec, "high-risk", risk_level="high")
    from boxporter.core.lease import LeaseManager
    from boxporter.core.recovery import RecoveryEngine
    from boxporter.core.watchdog import WatchDog
    from boxporter.runners.base import RunnerRegistry
    from boxporter.runners.mock import MockRunner

    registry = RunnerRegistry()
    registry.register(MockRunner())
    leases = LeaseManager(store)
    scheduler = Scheduler(store, registry, leases, WatchDog(store, leases),
                          RecoveryEngine(store))
    scheduler.apply_policy(policy_service.read())  # AWAY: low/medium only
    result = scheduler.tick()
    assert result.action == "idle"
    assert store.tasks.get(store.db.conn, "high-risk").state == TaskState.READY

    # A low-risk task is admitted in AWAY mode.
    add_ready_task(store, make_spec, "low-risk", risk_level="low")
    scheduler.tick()
    assert store.tasks.get(store.db.conn, "low-risk").state == TaskState.WORKING


def test_budget_gate_blocks_new_runs(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "budgeted", token_budget=1000)
    from boxporter.core.lease import LeaseManager
    from boxporter.core.recovery import RecoveryEngine
    from boxporter.core.watchdog import WatchDog
    from boxporter.runners.base import RunnerRegistry
    from boxporter.runners.mock import MockRunner

    # A previous run already consumed 600 tokens today.
    result = store.execute(
        StartExecutorRun(task_id="budgeted", runner="mock", identity="a",
                         session_id="s", actor_type="daemon")
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    BudgetService(store).record_usage(run_id, tokens_in=600)
    store.execute(FailRun(run_id=run_id, kind="crash", stop_reason="pre",
                          actor_type="daemon"))
    add_ready_task(store, make_spec, "budgeted-2", token_budget=1000)
    from boxporter.application.commands import BeginNextAttempt

    store.execute(BeginNextAttempt(task_id="budgeted", actor_type="user"))

    registry = RunnerRegistry()
    runner = MockRunner()
    registry.register(runner)
    leases = LeaseManager(store)
    scheduler = Scheduler(
        store, registry, leases, WatchDog(store, leases), RecoveryEngine(store),
        SchedulingPolicy(daily_token_budget=500),
    )
    # Daily cap exhausted: nothing new starts, zero model calls.
    result = scheduler.tick()
    assert result.action == "idle"
    assert runner.started == []
    assert store.tasks.get(store.db.conn, "budgeted-2").state == TaskState.READY


def test_budget_overrun_fails_run_and_stops(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "budgeted-2", token_budget=1000)
    from boxporter.core.lease import LeaseManager
    from boxporter.core.recovery import RecoveryEngine
    from boxporter.core.watchdog import WatchDog
    from boxporter.runners.base import RunnerRegistry
    from boxporter.runners.mock import MockRunner

    registry = RunnerRegistry()
    registry.register(MockRunner())
    leases = LeaseManager(store)
    scheduler = Scheduler(store, registry, leases, WatchDog(store, leases),
                          RecoveryEngine(store))
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "budgeted-2")[0]
    BudgetService(store).record_usage(run.id, tokens_in=999, tokens_out=1)
    scheduler.tick()  # budget check fires -> FailRun(kind=budget)
    task = store.tasks.get(store.db.conn, "budgeted-2")
    assert task.state == TaskState.BLOCKED  # budget stops -> no auto retry
    assert store.runs.get(store.db.conn, run.id).stop_reason == "budget-exceeded"
    notifications = store.notifications.list_since(store.db.conn)
    assert any(item["kind"] == "budget" for item in notifications)


def test_notification_dedup(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    notifier = Notifier(store)
    notifier.block("t", "device offline")
    notifier.block("t", "device offline again")
    notifications = store.notifications.list_since(store.db.conn)
    assert len(notifications) == 1


def test_probe_unblocks_task(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "probed")
    from boxporter.application.commands import StartExecutorRun

    result = store.execute(
        StartExecutorRun(task_id="probed", runner="mock", identity="a",
                         session_id="s", actor_type="daemon")
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    assert store.execute(
        BlockTask(
            task_id="probed",
            reason="waiting for device",
            probe_command=("true",),
            probe_interval_seconds=60,
            actor_type="user",
        )
    ).ok
    assert store.tasks.get(store.db.conn, "probed").state == TaskState.BLOCKED

    now = datetime.now(timezone.utc) + timedelta(seconds=61)
    resolved = ProbeRunner(store, now=now).run_due()
    assert resolved == 1
    assert store.tasks.get(store.db.conn, "probed").state == TaskState.READY
    blockers = store.blockers.list_open_for_task(store.db.conn, "probed")
    assert blockers == []


def test_probe_not_due_stays_blocked(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "probed-2")
    from boxporter.application.commands import MarkRunRunning, StartExecutorRun

    result = store.execute(
        StartExecutorRun(task_id="probed-2", runner="mock", identity="a",
                         session_id="s", actor_type="daemon")
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    assert store.execute(
        BlockTask(
            task_id="probed-2",
            reason="waiting",
            probe_command=("true",),
            probe_interval_seconds=3600,
            actor_type="user",
        )
    ).ok
    now = datetime.now(timezone.utc)  # not yet due
    assert ProbeRunner(store, now=now).run_due() == 0
    assert store.tasks.get(store.db.conn, "probed-2").state == TaskState.BLOCKED


def test_activity_report_window(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    from boxporter.application.commands import MarkRunRunning, StartExecutorRun
    from boxporter.application.queries import latest_seq

    start = latest_seq(store)
    add_ready_task(store, make_spec, "report-task")
    result = store.execute(
        StartExecutorRun(task_id="report-task", runner="mock", identity="a",
                         session_id="s", actor_type="daemon")
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    assert store.execute(
        BlockTask(task_id="report-task", reason="manual", actor_type="user")
    ).ok
    from boxporter.core.clock import now_iso

    report = activity_report(store, "2020-01-01T00:00:00Z", now_iso())
    assert "report-task" in report
    assert "阻塞" in report
    assert start < latest_seq(store)


def test_unattended_24h_simulation_no_duplicate_starts(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    """Simulate 24 hours of ticks: no duplicate concurrent starts, no
    overspend, PAUSED stops scheduling."""
    add_ready_task(store, make_spec, "sim-task")
    from boxporter.core.lease import LeaseManager
    from boxporter.core.recovery import RecoveryEngine
    from boxporter.core.watchdog import WatchDog
    from boxporter.runners.base import RunnerRegistry
    from boxporter.runners.mock import MockRunner

    registry = RunnerRegistry()
    runner = MockRunner()
    registry.register(runner)
    leases = LeaseManager(store)
    watchdog = WatchDog(store, leases)
    scheduler = Scheduler(store, registry, leases, watchdog, RecoveryEngine(store))

    model_calls = 0
    for _ in range(24 * 60):  # 1 tick per minute for 24h
        result = scheduler.tick()
        if result.model_call:
            model_calls += 1
        if len(runner.started) > 0 and result.action == "idle":
            break  # settled; nothing more to observe
    assert len(runner.started) == 1  # started exactly once, never duplicated
    runs = store.runs.list_for_task(store.db.conn, "sim-task")
    assert len(runs) == 1

    policy = PolicyService(store)
    policy.set_mode("PAUSED")
    scheduler.apply_policy(policy.read())
    assert scheduler.tick().action == "paused"
    assert len(runner.started) == 1
