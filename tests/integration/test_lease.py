"""Lease, heartbeat and fencing token tests (ADR-004)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from boxporter.application.commands import CreateTask, ReadyTask, StartExecutorRun
from boxporter.core.lease import (
    LeaseConflict,
    LeaseManager,
    StaleLeaseError,
)
from boxporter.core.schemas import TaskSpec
from boxporter.storage.store import Store


@pytest.fixture
def ready_run(store: Store, make_spec: Callable[..., TaskSpec]) -> str:
    assert store.execute(CreateTask(spec=make_spec("lease-task"), actor_type="user")).ok
    assert store.execute(ReadyTask(task_id="lease-task", actor_type="user")).ok
    result = store.execute(
        StartExecutorRun(
            task_id="lease-task", runner="mock", identity="agent-a",
            session_id="sess-a", actor_type="daemon",
        )
    )
    return str(result.data["run_id"])


def test_acquire_heartbeat_release(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    lease = manager.acquire(ready_run, pid=4242)
    assert lease.run_id == ready_run
    assert lease.pid == 4242
    assert lease.fencing_token == 1

    refreshed = manager.heartbeat(ready_run, lease.fencing_token)
    assert refreshed.heartbeat_at >= lease.heartbeat_at

    manager.release(ready_run, lease.fencing_token)
    assert manager.get(ready_run) is None


def test_conflict_on_same_task_role(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    manager.acquire(ready_run, pid=1)
    with pytest.raises(LeaseConflict):
        manager.acquire(ready_run, pid=2)


def test_expired_lease_replaced_with_higher_token(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    old = manager.acquire(ready_run, pid=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = store.db.conn
    conn.execute("UPDATE leases SET expires_at = ? WHERE run_id = ?",
                 (past.isoformat(), ready_run))
    new = manager.acquire(ready_run, pid=2, now=datetime.now(timezone.utc))
    assert new.fencing_token > old.fencing_token


def test_stale_token_cannot_heartbeat(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    lease = manager.acquire(ready_run, pid=1)
    with pytest.raises(StaleLeaseError, match="fencing token mismatch"):
        manager.heartbeat(ready_run, lease.fencing_token + 999)


def test_stale_token_cannot_release(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    lease = manager.acquire(ready_run, pid=1)
    with pytest.raises(StaleLeaseError, match="fencing token mismatch"):
        manager.release(ready_run, lease.fencing_token - 1)
    assert manager.get(ready_run) is not None


def test_expired_lease_rejects_heartbeat(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    lease = manager.acquire(ready_run, pid=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = store.db.conn
    conn.execute("UPDATE leases SET expires_at = ? WHERE run_id = ?",
                 (past.isoformat(), ready_run))
    with pytest.raises(StaleLeaseError, match="expired"):
        manager.heartbeat(ready_run, lease.fencing_token,
                          now=datetime.now(timezone.utc))


def test_expired_leases_listing(store: Store, ready_run: str) -> None:
    manager = LeaseManager(store)
    manager.acquire(ready_run, pid=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert manager.expired_leases(now=future) != []
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert manager.expired_leases(now=past) == []
