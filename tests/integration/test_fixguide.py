"""Regression tests for the fix guide: runner factory, observe-driven
lifecycle closure, honest reconcile, approval guard (P0-A/B/C, P1-E)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from boxporter.application.commands import (
    CreateTask,
    DecideApproval,
    ReadyTask,
    RequestApproval,
)
from boxporter.core.lease import LeaseManager
from boxporter.core.recovery import RecoveryEngine
from boxporter.core.scheduler import Scheduler, SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState, TaskState
from boxporter.core.watchdog import WatchDog
from boxporter.runners.base import RunObservation
from boxporter.runners.mock import MockRunner
from boxporter.storage.store import Store


def build_scheduler(
    store: Store,
    runner: MockRunner,
    policy: SchedulingPolicy | None = None,
    worktrees_root: Path | None = None,
) -> Scheduler:
    from boxporter.runners.base import RunnerRegistry

    registry = RunnerRegistry()
    registry.register(runner)
    leases = LeaseManager(store)
    return Scheduler(
        store, registry, leases, WatchDog(store, leases), RecoveryEngine(store),
        policy, worktrees_root=worktrees_root,
    )


def add_ready_task(
    store: Store, make_spec: Callable[..., TaskSpec], task_id: str, **kwargs: object
) -> None:
    assert store.execute(CreateTask(spec=make_spec(task_id, **kwargs), actor_type="user")).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok


# -- P0-A: runner factory ------------------------------------------------


def test_factory_errors_on_empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from boxporter.core.errors import BoxPorterError
    from boxporter.runners import build_registry

    for name in (
        "BOXPORTER_RUNNER",
        "BOXPORTER_OPENHANDS_API_KEY",
        "BOXPORTER_DSH_COMMAND",
        "BOXPORTER_EXECUTOR_COMMAND",
        "BOXPORTER_ALLOW_MOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BoxPorterError, match="no runner configured"):
        build_registry()


def test_factory_mock_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from boxporter.core.errors import BoxPorterError
    from boxporter.runners import build_registry

    monkeypatch.setenv("BOXPORTER_RUNNER", "mock")
    monkeypatch.delenv("BOXPORTER_ALLOW_MOCK", raising=False)
    with pytest.raises(BoxPorterError, match="BOXPORTER_ALLOW_MOCK"):
        build_registry()
    monkeypatch.setenv("BOXPORTER_ALLOW_MOCK", "1")
    registry = build_registry()
    assert "mock" in registry.names()


def test_factory_openhands_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from boxporter.core.errors import BoxPorterError
    from boxporter.runners import build_registry

    monkeypatch.setenv("BOXPORTER_RUNNER", "openhands")
    monkeypatch.delenv("BOXPORTER_OPENHANDS_API_KEY", raising=False)
    with pytest.raises(BoxPorterError, match="BOXPORTER_OPENHANDS_API_KEY"):
        build_registry()
    monkeypatch.setenv("BOXPORTER_OPENHANDS_API_KEY", "test-key")
    monkeypatch.setenv("BOXPORTER_OPENHANDS_MODEL", "test/model")
    registry = build_registry()
    assert "openhands" in registry.names()


# -- P0-B: observe closure -----------------------------------------------


def test_observe_executor_success_auto_submits(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    from tests.phase3 import init_repo

    workspace = tmp_path / "workspace"
    init_repo(workspace)
    add_ready_task(store, make_spec, "t-observe")
    runner = MockRunner()
    scheduler = build_scheduler(
        store, runner, worktrees_root=tmp_path / "worktrees"
    )
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-observe")[0]
    assert run.worktree is not None

    reports = Path(run.worktree) / "reports"
    reports.mkdir()
    for name, content in (
        ("result.md", "# Result\nfixed\n"),
        ("verify.md", "# Verify\ntests pass\n"),
        ("executor.md", "# Executor\ndone\n"),
    ):
        (reports / name).write_text(content, encoding="utf-8")
    runner.set_observation(
        run.id, RunObservation(state=RunState.SUCCEEDED, last_activity_at="t")
    )
    scheduler.tick()
    assert store.tasks.get(store.db.conn, "t-observe").state == TaskState.REVIEW_PENDING
    attempt = store.attempts.get_by_task_number(store.db.conn, "t-observe", 1)
    submission = store.submissions.get_for_attempt(store.db.conn, attempt.id)
    assert submission is not None


def test_observe_executor_success_without_reports_fails(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    from tests.phase3 import init_repo

    workspace = tmp_path / "workspace"
    init_repo(workspace)
    add_ready_task(store, make_spec, "t-noreports")
    runner = MockRunner()
    scheduler = build_scheduler(
        store, runner, worktrees_root=tmp_path / "worktrees"
    )
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-noreports")[0]
    runner.set_observation(
        run.id, RunObservation(state=RunState.SUCCEEDED, last_activity_at="t")
    )
    scheduler.tick()
    task = store.tasks.get(store.db.conn, "t-noreports")
    assert task.state == TaskState.FAILED
    assert store.runs.get(store.db.conn, run.id).stop_reason == "reports-missing-in-worktree"


def test_observe_crash_fails_run(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    from tests.phase3 import init_repo

    workspace = tmp_path / "workspace"
    init_repo(workspace)
    add_ready_task(store, make_spec, "t-crash")
    runner = MockRunner()
    scheduler = build_scheduler(
        store, runner, worktrees_root=tmp_path / "worktrees"
    )
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-crash")[0]
    runner.set_observation(
        run.id,
        RunObservation(
            state=RunState.CRASHED, last_activity_at="t",
            detail={"reason": "segfault"},
        ),
    )
    scheduler.tick()
    assert store.tasks.get(store.db.conn, "t-crash").state == TaskState.FAILED
    assert run.id not in scheduler.handles


def test_observe_full_auto_loop_to_pass(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    from tests.phase3 import init_repo

    workspace = tmp_path / "workspace"
    init_repo(workspace)
    add_ready_task(store, make_spec, "t-loop")
    runner = MockRunner()
    scheduler = build_scheduler(
        store, runner, worktrees_root=tmp_path / "worktrees"
    )
    scheduler.tick()
    executor_run = store.runs.list_for_task(store.db.conn, "t-loop")[0]
    reports = Path(executor_run.worktree) / "reports"
    reports.mkdir()
    for name, content in (
        ("result.md", "# Result\nok\n"),
        ("verify.md", "# Verify\nok\n"),
        ("executor.md", "# Executor\nok\n"),
    ):
        (reports / name).write_text(content, encoding="utf-8")
    runner.set_observation(
        executor_run.id, RunObservation(state=RunState.SUCCEEDED, last_activity_at="t")
    )
    scheduler.tick()  # auto submit -> REVIEW_PENDING
    scheduler.tick()  # auto start reviewer
    reviewer_run = next(
        run
        for run in store.runs.list_for_task(store.db.conn, "t-loop")
        if run.role == "reviewer"
    )
    assert reviewer_run.worktree is not None
    assert reviewer_run.worktree != executor_run.worktree
    review_dir = Path(reviewer_run.worktree) / "reports"
    review_dir.mkdir()
    (review_dir / "review.md").write_text("# Review\n\n## 结论\nPASS\n", encoding="utf-8")
    (review_dir / "review_evidence.json").write_text(
        json.dumps({"test_exit_code": 0, "production_risk": "low"}), encoding="utf-8"
    )
    runner.set_observation(
        reviewer_run.id, RunObservation(state=RunState.SUCCEEDED, last_activity_at="t")
    )
    scheduler.tick()  # auto record review -> PASS
    assert store.tasks.get(store.db.conn, "t-loop").state == TaskState.PASS


# -- P0-C: reconcile honesty --------------------------------------------


def test_reconcile_does_not_fake_handles_without_pid(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    from boxporter.core.reconcile import Reconcile

    add_ready_task(store, make_spec, "t-reattach")
    runner = MockRunner()
    scheduler = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-reattach")[0]
    leases = LeaseManager(store)
    lease = leases.get(run.id)
    assert lease is not None and lease.pid is None  # mock runs have no pid

    handles: dict[str, object] = {}
    report = Reconcile(store, leases).run(handles=handles)
    assert run.id in report.crashed_runs
    assert handles == {}  # no fabricated observable handles
    assert store.runs.get(store.db.conn, run.id).stop_reason == (
        "reconciliation: runner session cannot be re-attached"
    )
    from boxporter.application.queries import events_since

    types = [event.event_type for event in events_since(store, 0)]
    assert "RECONCILE_NO_REATTACH" in types


def test_reconcile_reattaches_pid_backed_runs(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    import os

    from boxporter.core.reconcile import Reconcile

    add_ready_task(store, make_spec, "t-pid")
    runner = MockRunner()
    scheduler = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-pid")[0]
    leases = LeaseManager(store)
    conn = store.db.conn
    conn.execute("UPDATE leases SET pid = ? WHERE run_id = ?", (os.getpid(), run.id))
    handles: dict[str, object] = {}
    report = Reconcile(store, leases).run(handles=handles)
    assert run.id in report.reattached_runs
    assert handles[run.id].pid == os.getpid()  # type: ignore[union-attr]


# -- P1-E: approval guard -----------------------------------------------


def test_high_risk_task_requires_approval(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-high", risk_level="high")
    runner = MockRunner()
    policy = SchedulingPolicy(allowed_risk_levels=frozenset({"low", "medium", "high"}))
    scheduler = build_scheduler(store, runner, policy)
    scheduler.tick()
    assert store.tasks.get(store.db.conn, "t-high").state == TaskState.READY
    from boxporter.application.queries import events_since

    types = [event.event_type for event in events_since(store, 0)]
    assert "APPROVAL_REJECTED" in types

    approval = store.execute(
        RequestApproval(
            task_id="t-high",
            action="execute-high-risk-task",
            target="task://t-high",
            risk_level="high",
            max_uses=1,
            actor_type="user",
        )
    )
    assert store.execute(
        DecideApproval(
            approval_id=str(approval.data["approval_id"]),
            decision="approve",
            actor_type="user",
        )
    ).ok
    scheduler.tick()
    assert store.tasks.get(store.db.conn, "t-high").state == TaskState.WORKING
    granted = store.approvals.list_for_task(store.db.conn, "t-high")[0]
    assert granted.used_count == 1


def test_send_message_high_risk_requires_approval(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-send")
    runner = MockRunner()
    scheduler = build_scheduler(store, runner)
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-send")[0]
    assert scheduler.send_message(run.id, "sudo rm -rf /", action="sudo") is False

    approval = store.execute(
        RequestApproval(
            task_id="t-send",
            action="sudo",
            target="task://t-send",
            actor_type="user",
        )
    )
    store.execute(
        DecideApproval(
            approval_id=str(approval.data["approval_id"]),
            decision="approve",
            actor_type="user",
        )
    )
    assert scheduler.send_message(run.id, "sudo ls", action="sudo") is True
    assert runner.messages == [(run.id, "sudo ls")]
