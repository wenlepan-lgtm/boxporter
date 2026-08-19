"""Command runner with a real subprocess + host-restart reconciliation."""

from __future__ import annotations

import time
from collections.abc import Callable

from boxporter.application.commands import CreateTask, ReadyTask, StartExecutorRun
from boxporter.core.lease import LeaseManager
from boxporter.core.reconcile import Reconcile
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState, TaskState
from boxporter.runners.command import CommandRunner
from boxporter.storage.db import Database
from boxporter.storage.store import Store


def spawn_task(store: Store, make_spec: Callable[..., TaskSpec]) -> str:
    assert store.execute(CreateTask(spec=make_spec("cmd-task"), actor_type="user")).ok
    assert store.execute(ReadyTask(task_id="cmd-task", actor_type="user")).ok
    result = store.execute(
        StartExecutorRun(
            task_id="cmd-task", runner="command", identity="agent-a",
            session_id="sess-a", actor_type="daemon",
        )
    )
    return str(result.data["run_id"])


def test_command_runner_process_lifecycle(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = spawn_task(store, make_spec)
    run = store.runs.get(store.db.conn, run_id)
    runner = CommandRunner(["sleep", "30"])
    from boxporter.runners.base import RunSpec

    spec = RunSpec(
        run_id=run_id,
        task_id="cmd-task",
        attempt=1,
        role="executor",
        workspace=run.session_id and store.tasks.get(store.db.conn, "cmd-task").spec.workspace or ".",
        task=store.tasks.get(store.db.conn, "cmd-task").spec,
        session_id="sess-a",
        runner_profile="boxporter-executor",
    )
    handle = runner.start(spec)
    assert handle.pid is not None

    observation = runner.inspect(handle)
    assert observation.state == RunState.RUNNING
    assert observation.pid == handle.pid

    stop = runner.stop(handle, "test done")
    assert stop.stopped
    time.sleep(0.1)
    final = runner.inspect(handle)
    assert final.exit_code is not None or final.state in {RunState.SUCCEEDED, RunState.CRASHED}


def test_command_runner_observes_exit(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = spawn_task(store, make_spec)
    runner = CommandRunner(["sh", "-c", "exit 3"])
    from boxporter.runners.base import RunSpec

    spec = RunSpec(
        run_id=run_id,
        task_id="cmd-task",
        attempt=1,
        role="executor",
        workspace=store.tasks.get(store.db.conn, "cmd-task").spec.workspace,
        task=store.tasks.get(store.db.conn, "cmd-task").spec,
        session_id="sess-a",
        runner_profile="boxporter-executor",
    )
    handle = runner.start(spec)
    assert handle.pid is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        observation = runner.inspect(handle)
        if observation.state != RunState.RUNNING:
            break
        time.sleep(0.05)
    assert observation.state == RunState.CRASHED
    assert observation.exit_code == 3


def test_reconcile_crashes_runs_with_expired_lease(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = spawn_task(store, make_spec)
    leases = LeaseManager(store)
    leases.acquire(run_id, pid=None)

    from datetime import datetime, timedelta, timezone

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = store.db.conn
    conn.execute("UPDATE leases SET expires_at = ? WHERE run_id = ?",
                 (past.isoformat(), run_id))

    report = Reconcile(store, leases).run(handles={})
    assert run_id in report.crashed_runs
    assert store.runs.get(store.db.conn, run_id).state == RunState.CRASHED
    assert store.tasks.get(store.db.conn, "cmd-task").state == TaskState.FAILED
    assert leases.get(run_id) is None


def test_reconcile_reattaches_live_runs(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    import os

    run_id = spawn_task(store, make_spec)
    leases = LeaseManager(store)
    leases.acquire(run_id, pid=os.getpid())  # our own pid is alive

    handles: dict[str, object] = {}
    report = Reconcile(store, leases).run(handles=handles)
    assert run_id in report.reattached_runs
    assert report.crashed_runs == ()


def test_reconcile_after_db_reopen(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: object
) -> None:
    """Simulate a host restart: the database connection dies while a run is
    active with an expired lease. After reopening, reconciliation crashes
    the run and state is consistent."""
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    run_id = spawn_task(store, make_spec)
    leases = LeaseManager(store)
    leases.acquire(run_id, pid=None)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    store.db.conn.execute(
        "UPDATE leases SET expires_at = ? WHERE run_id = ?",
        (past.isoformat(), run_id),
    )
    path = Path(store.db.path)
    store.db.close()

    db = Database(path)
    db.open()
    reopened = Store(db)
    report = Reconcile(reopened, LeaseManager(reopened)).run(handles={})
    assert run_id in report.crashed_runs
    assert reopened.runs.get(db.conn, run_id).state == RunState.CRASHED
    db.close()
