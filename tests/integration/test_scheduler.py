"""Watchdog, recovery and scheduler tick tests (Phase 2 acceptance)."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from boxporter.application.commands import (
    CreateTask,
    FailRun,
    ReadyTask,
    SubmitExecutorRun,
)
from boxporter.core.lease import LeaseManager
from boxporter.core.recovery import RecoveryAction, RecoveryEngine
from boxporter.core.scheduler import Scheduler, SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState, TaskState
from boxporter.core.watchdog import FindingKind, WatchDog
from boxporter.runners.base import RunnerRegistry
from boxporter.runners.mock import MockRunner
from boxporter.storage.store import Store


def build_scheduler(
    store: Store,
    runner: MockRunner,
    policy: SchedulingPolicy | None = None,
    worktrees_root: object | None = None,
) -> tuple[Scheduler, LeaseManager, WatchDog]:
    from pathlib import Path

    registry = RunnerRegistry()
    registry.register(runner)
    leases = LeaseManager(store)
    watchdog = WatchDog(store, leases)
    recovery = RecoveryEngine(store)
    scheduler = Scheduler(
        store,
        registry,
        leases,
        watchdog,
        recovery,
        policy,
        worktrees_root=Path(worktrees_root) if worktrees_root is not None else None,
    )
    return scheduler, leases, watchdog


def add_ready_task(store: Store, make_spec: Callable[..., TaskSpec], task_id: str,
                   risk: str = "low") -> None:
    assert store.execute(CreateTask(spec=make_spec(task_id, risk_level=risk),
                                    actor_type="user")).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok


def test_tick_idle_is_zero_model(store: Store, make_spec: Callable[..., TaskSpec]) -> None:
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(store, runner)
    result = scheduler.tick()
    assert result.action == "idle"
    assert result.model_call is False
    assert runner.started == []


def test_tick_paused_starts_nothing(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(
        store, runner, SchedulingPolicy(mode="PAUSED")
    )
    result = scheduler.tick()
    assert result.action == "paused"
    assert runner.started == []


def test_tick_starts_executor_and_acquires_lease(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, leases, _ = build_scheduler(store, runner)
    result = scheduler.tick()
    assert result.action == "started_runs"
    assert result.model_call is False  # mock runner needs no model
    assert len(runner.started) == 1
    task = store.tasks.get(store.db.conn, "task-1")
    assert task.state == TaskState.WORKING
    runs = store.runs.list_for_task(store.db.conn, "task-1")
    assert len(runs) == 1
    assert runs[0].state == RunState.RUNNING
    assert leases.get(runs[0].id) is not None
    second = scheduler.tick()
    assert second.action == "idle"


def test_tick_risk_gate(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "risky", risk="high")
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(store, runner)
    assert scheduler.tick().action == "idle"
    assert store.tasks.get(store.db.conn, "risky").state == TaskState.READY


def test_tick_capacity_limit(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    add_ready_task(store, make_spec, "task-2")
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(store, runner)
    scheduler.tick()
    second = scheduler.tick()
    assert second.action == "idle"
    assert len(runner.started) == 1
    assert store.tasks.get(store.db.conn, "task-2").state == TaskState.READY


def test_tick_auto_review_after_completion(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: object
) -> None:
    from pathlib import Path

    from tests.phase3 import init_repo, make_report_dir

    root = Path(store.db.path).parent.parent
    workspace = root / "workspace"
    init_repo(workspace)

    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(
        store, runner, worktrees_root=root / "worktrees"
    )
    scheduler.tick()
    runs = store.runs.list_for_task(store.db.conn, "task-1")
    executor_run = runs[0]
    assert executor_run.worktree is not None

    executor_wt = Path(executor_run.worktree)
    (executor_wt / "app.py").write_text("value = 2\n", encoding="utf-8")
    from boxporter.core.gitworktree import git

    git("add", "app.py", cwd=executor_wt)
    git("commit", "-q", "-m", "fix", cwd=executor_wt)
    report_dir = make_report_dir(root / "task-1")
    result = store.execute(
        SubmitExecutorRun(
            run_id=executor_run.id,
            report_dir=str(report_dir),
            worktree=str(executor_wt),
            actor_type="daemon",
        )
    )
    assert result.ok, result.message

    tick_result = scheduler.tick()
    assert tick_result.action == "started_runs"
    runs = store.runs.list_for_task(store.db.conn, "task-1")
    reviewer_run = next(run for run in runs if run.role == "reviewer")
    assert reviewer_run.worktree is not None
    assert reviewer_run.worktree != executor_run.worktree
    # Reviewer worktree must sit on the frozen submission head commit.
    from boxporter.core.gitworktree import git as git2

    reviewer_head = git2("rev-parse", "HEAD", cwd=Path(reviewer_run.worktree)).stdout.strip()
    executor_head = git2("rev-parse", "HEAD", cwd=executor_wt).stdout.strip()
    assert reviewer_head == executor_head


def test_watchdog_fails_dead_process_and_recovers(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, leases, _ = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    lease = leases.get(run.id)
    assert lease is not None
    conn = store.db.conn
    conn.execute(
        "UPDATE leases SET pid = ? WHERE run_id = ?",
        (999999, run.id),  # pid that can never exist on this host
    )
    # Immediately: FailRun happens, but backoff prevents a same-tick retry.
    first = scheduler.tick()
    assert first.action in {"idle", "started_runs"}
    task = store.tasks.get(store.db.conn, "task-1")
    assert task.state == TaskState.FAILED
    assert task.current_attempt == 1

    # After the backoff window the retry proceeds within the recovery budget.
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    second = scheduler.tick(now=future)
    assert second.action == "started_runs"
    task = store.tasks.get(store.db.conn, "task-1")
    assert task.current_attempt == 2
    assert task.state == TaskState.WORKING


def test_recovery_budget_exhausted_blocks(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    policy = SchedulingPolicy(max_recoveries_per_attempt=1)
    scheduler, _, _ = build_scheduler(store, runner, policy)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    conn = store.db.conn
    conn.execute("UPDATE leases SET pid = ? WHERE run_id = ?", (999999, run.id))

    scheduler.tick()  # failure 1 -> budget (1) exhausted -> BLOCKED
    task = store.tasks.get(store.db.conn, "task-1")
    assert task.current_attempt == 1
    assert task.state == TaskState.BLOCKED


def test_recovery_retries_then_blocks_after_budget(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    policy = SchedulingPolicy(max_recoveries_per_attempt=2)
    scheduler, _, _ = build_scheduler(store, runner, policy)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    conn = store.db.conn
    conn.execute("UPDATE leases SET pid = ? WHERE run_id = ?", (999999, run.id))

    scheduler.tick()  # failure 1 (backoff window starts)
    assert store.tasks.get(store.db.conn, "task-1").state == TaskState.FAILED

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    scheduler.tick(now=future)  # backoff elapsed -> attempt 2
    assert store.tasks.get(store.db.conn, "task-1").current_attempt == 2

    run2 = next(
        run
        for run in store.runs.list_for_task(store.db.conn, "task-1")
        if run.state == RunState.RUNNING
    )
    conn.execute("UPDATE leases SET pid = ? WHERE run_id = ?", (999999, run2.id))
    scheduler.tick()  # failure 2 -> budget exhausted -> BLOCKED
    assert store.tasks.get(store.db.conn, "task-1").state == TaskState.BLOCKED


def test_watchdog_findings(store: Store, make_spec: Callable[..., TaskSpec]) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, leases, watchdog = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    lease = leases.get(run.id)
    assert lease is not None

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = store.db.conn
    conn.execute("UPDATE leases SET expires_at = ? WHERE run_id = ?",
                 (past.isoformat(), run.id))
    findings = watchdog.check()
    assert any(f.kind == FindingKind.LEASE_EXPIRED for f in findings)

    conn.execute("DELETE FROM leases WHERE run_id = ?", (run.id,))
    findings = watchdog.check()
    assert any(f.kind == FindingKind.LEASE_MISSING for f in findings)


def test_watchdog_process_alive(store: Store, make_spec: Callable[..., TaskSpec]) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, leases, watchdog = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    lease = leases.get(run.id)
    assert lease is not None
    conn = store.db.conn
    conn.execute("UPDATE leases SET pid = ? WHERE run_id = ?", (os.getpid(), run.id))
    findings = watchdog.check()
    assert not any(f.kind == FindingKind.PROCESS_DEAD for f in findings)


def test_recovery_engine_after_failure_budget(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "task-1")
    runner = MockRunner()
    scheduler, _, _ = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "task-1")[0]
    assert store.execute(
        FailRun(run_id=run.id, kind="crash", stop_reason="test", actor_type="daemon")
    ).ok
    recovery = RecoveryEngine(store)
    # Inside the backoff window the engine says RETRY_LATER; after it, retry.
    decision = recovery.after_failure("task-1")
    assert decision.action == RecoveryAction.RETRY_LATER
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    decision = recovery.after_failure("task-1", now=future)
    assert decision.action == RecoveryAction.BEGIN_NEXT_ATTEMPT
