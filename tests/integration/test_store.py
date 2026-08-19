"""Integration tests: storage, idempotency, event cursor, crash reopen."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from boxporter.application.commands import CreateGoal, CreateProject, CreateTask, ReadyTask
from boxporter.application.queries import events_since, latest_seq
from boxporter.core.errors import ConcurrencyError
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import TaskState
from boxporter.storage.db import Database
from boxporter.storage.store import Store


def test_migrations_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "data" / "boxporter.sqlite"
    db = Database(path)
    db.open()
    store = Store(db)
    result = store.execute(
        CreateProject(
            project_id="demo",
            name="Demo",
            workspace_root=str(tmp_path),
            actor_type="user",
        )
    )
    assert result.ok
    seq = latest_seq(store)
    assert seq == 1
    db.close()

    reopened = Database(path)
    reopened.open()
    assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    store2 = Store(reopened)
    assert store2.projects.get(reopened.conn, "demo").id == "demo"
    assert latest_seq(store2) == seq
    reopened.close()


def test_idempotent_replay(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    spec = make_spec("task-1")
    op_id = "op-create-1"
    first = store.execute(CreateTask(spec=spec, actor_type="user"), operation_id=op_id)
    second = store.execute(CreateTask(spec=spec, actor_type="user"), operation_id=op_id)
    assert first.ok
    assert second.replayed
    assert second.data == first.data
    assert latest_seq(store) == 2  # project + task created exactly once


def test_version_conflict_blocks_stale_write(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    store.execute(CreateTask(spec=make_spec("task-1"), actor_type="user"))
    store.execute(ReadyTask(task_id="task-1", actor_type="user"))
    task = store.tasks.get(store.db.conn, "task-1")
    assert task.state == TaskState.READY
    with pytest.raises(ConcurrencyError):
        store.tasks.update_state(
            store.db.conn, "task-1", TaskState.READY, expected_version=1
        )


def test_event_cursor(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    store.execute(CreateTask(spec=make_spec("t-1"), actor_type="user"))
    store.execute(CreateTask(spec=make_spec("t-2"), actor_type="user"))
    store.execute(
        CreateGoal(
            goal_id="g-1",
            project_id=project,
            title="Goal",
            outcome="Outcome",
            actor_type="user",
        )
    )
    all_events = events_since(store, 0)
    assert [e.seq for e in all_events] == sorted(e.seq for e in all_events)
    assert len(all_events) == 4  # project + 2 tasks + goal
    tail = events_since(store, all_events[1].seq)
    assert len(tail) == 2
    assert events_since(store, latest_seq(store)) == []
