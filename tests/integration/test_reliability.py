"""Reliability features: backoff, fingerprints, approvals, context packs,
progress detection, memory gating, SSE cursor replay (acceptance 12/13/22)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxporter.api.app import create_app
from boxporter.api.auth import hash_password
from boxporter.application.commands import (
    CreateTask,
    DecideApproval,
    FailRun,
    ReadyTask,
    RequestApproval,
)
from boxporter.application.queries import events_since, latest_seq
from boxporter.core.contextpack import build_context_pack
from boxporter.core.lease import LeaseManager
from boxporter.core.recovery import RecoveryEngine, backoff_seconds, error_fingerprint
from boxporter.core.scheduler import Scheduler, SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState, TaskState
from boxporter.core.watchdog import FindingKind, WatchDog
from boxporter.runners.base import RunnerRegistry
from boxporter.runners.mock import MockRunner
from boxporter.storage.store import Store

CLIENT_HEADERS = {"X-BoxPorter-Client": "tests"}


def add_ready_task(
    store: Store, make_spec: Callable[..., TaskSpec], task_id: str, **kwargs: object
) -> None:
    assert store.execute(
        CreateTask(spec=make_spec(task_id, **kwargs), actor_type="user")
    ).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok


def build_scheduler(store: Store, runner: MockRunner, policy: SchedulingPolicy | None = None):
    registry = RunnerRegistry()
    registry.register(runner)
    leases = LeaseManager(store)
    watchdog = WatchDog(store, leases)
    scheduler = Scheduler(store, registry, leases, watchdog, RecoveryEngine(store), policy)
    return scheduler


def fail_current_run(store: Store, task_id: str, reason: str) -> str:
    run = next(
        run
        for run in store.runs.list_for_task(store.db.conn, task_id)
        if run.state == RunState.RUNNING
    )
    assert store.execute(
        FailRun(run_id=run.id, kind="crash", stop_reason=reason, actor_type="daemon")
    ).ok
    return run.id


def test_error_fingerprint_stability() -> None:
    assert error_fingerprint("Model 5xx transient failure") == error_fingerprint(
        "model 5xx transient failure"
    )
    assert error_fingerprint("segfault") != error_fingerprint("timeout")


def test_backoff_increases_and_jitters() -> None:
    assert backoff_seconds(1) < backoff_seconds(2) < backoff_seconds(3)
    assert backoff_seconds(1) >= 60 * 0.8
    assert backoff_seconds(100) <= 3600 * 1.2


def test_retry_waits_for_backoff(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-backoff")
    scheduler = build_scheduler(store, MockRunner())
    scheduler.tick()
    fail_current_run(store, "t-backoff", "model 5xx")

    now = datetime.now(timezone.utc)
    result = scheduler.tick(now=now + timedelta(seconds=10))  # inside window
    assert result.action == "idle"
    assert store.tasks.get(store.db.conn, "t-backoff").state == TaskState.FAILED

    result = scheduler.tick(now=now + timedelta(minutes=10))  # window elapsed
    assert result.action == "started_runs"
    assert store.tasks.get(store.db.conn, "t-backoff").current_attempt == 2


def test_repeated_fingerprint_stops_retries(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-fingerprint", max_attempts=8)
    scheduler = build_scheduler(
        store, MockRunner(), SchedulingPolicy(max_recoveries_per_attempt=8)
    )
    future = datetime.now(timezone.utc)
    for _ in range(3):
        scheduler.tick(now=future)
        fail_current_run(store, "t-fingerprint", "same broken dependency")
        future += timedelta(minutes=10)
    scheduler.tick(now=future)  # 3 identical fingerprints -> STOP -> BLOCKED
    assert store.tasks.get(store.db.conn, "t-fingerprint").state == TaskState.BLOCKED


def test_different_fingerprints_keep_retrying(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-mixed", max_attempts=8)
    scheduler = build_scheduler(
        store, MockRunner(), SchedulingPolicy(max_recoveries_per_attempt=8)
    )
    future = datetime.now(timezone.utc)
    scheduler.tick(now=future)
    fail_current_run(store, "t-mixed", "error-a")
    future += timedelta(minutes=10)
    scheduler.tick(now=future)
    fail_current_run(store, "t-mixed", "error-b")
    future += timedelta(minutes=10)
    scheduler.tick(now=future)
    fail_current_run(store, "t-mixed", "error-a")
    future += timedelta(minutes=10)
    scheduler.tick(now=future)
    assert store.tasks.get(store.db.conn, "t-mixed").state == TaskState.WORKING
    assert store.tasks.get(store.db.conn, "t-mixed").current_attempt == 4


def test_no_progress_finding(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-progress")
    scheduler = build_scheduler(store, MockRunner())
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-progress")[0]
    # Old run without any progress signal -> diagnosed, not killed.
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = store.db.conn
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                 (past.isoformat(), run.id))
    leases = LeaseManager(store)
    watchdog = WatchDog(store, leases, now=datetime.now(timezone.utc))
    findings = watchdog.check()
    assert any(f.kind == FindingKind.NO_PROGRESS for f in findings)

    scheduler.record_progress(run.id, "checkpoint")
    findings = watchdog.check()
    assert not any(f.kind == FindingKind.NO_PROGRESS for f in findings)


def test_approvals_lifecycle(store: Store, make_spec: Callable[..., TaskSpec]) -> None:
    add_ready_task(store, make_spec, "t-approval")
    result = store.execute(
        RequestApproval(
            task_id="t-approval",
            action="read private logs",
            target="logs/app.log",
            risk_level="high",
            ttl_seconds=3600,
            actor_type="executor",
        )
    )
    approval_id = str(result.data["approval_id"])

    pending = store.approvals.list_for_task(store.db.conn, "t-approval")
    assert len(pending) == 1 and pending[0].status == "pending"

    approved = store.execute(
        DecideApproval(approval_id=approval_id, decision="approve", actor_type="user")
    )
    assert approved.ok
    assert store.approvals.get(store.db.conn, approval_id).status == "approved"

    # Consumption respects max_uses.
    now = datetime.now(timezone.utc)
    assert store.approvals.consume(
        store.db.conn, approval_id, by="executor", at=now.isoformat()
    )
    assert not store.approvals.consume(
        store.db.conn, approval_id, by="executor", at=now.isoformat()
    )

    # Events trail the whole lifecycle.
    types = [event.event_type for event in events_since(store, 0)]
    assert "APPROVAL_REQUESTED" in types
    assert "APPROVAL_DECIDED" in types


def test_approval_reject_and_double_decide(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-approval-2")
    result = store.execute(
        RequestApproval(
            task_id="t-approval-2",
            action="send external email",
            target="ops@example.com",
            actor_type="executor",
        )
    )
    approval_id = str(result.data["approval_id"])
    assert store.execute(
        DecideApproval(approval_id=approval_id, decision="reject", actor_type="user")
    ).ok
    assert store.approvals.get(store.db.conn, approval_id).status == "rejected"
    with pytest.raises(Exception, match="not pending"):
        store.execute(
            DecideApproval(approval_id=approval_id, decision="approve", actor_type="user")
        )


def test_context_pack_contents(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-pack", constraints=("no external calls",))
    from boxporter.core.ids import new_id
    from boxporter.storage.metering import MemoryItem

    store.memory.insert(
        store.db.conn,
        MemoryItem(
            id=new_id("mem"),
            project_id="demo",
            kind="repo-fact",
            content="app runs on python 3.12",
            source="repo-fact",
            source_ref=None,
            expires_at=None,
            created_at="2026-08-14T00:00:00Z",
        ),
    )
    task = store.tasks.get(store.db.conn, "t-pack")
    pack = build_context_pack(store, task, "executor")
    value = json.loads(pack)
    assert value["schema"] == "BOXPORTER_CONTEXT_V1"
    assert value["task_ref"] == "task://t-pack"
    assert "no external calls" in value["constraints"]
    assert any("python 3.12" in item["content"] for item in value["project_facts"])
    assert "push or deploy to production" in value["forbidden_actions"]
    assert value["budget"]["current_attempt"] == 0  # READY: no attempt started yet


def test_prompt_sha_recorded_on_run(
    store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    add_ready_task(store, make_spec, "t-prompt")
    scheduler = build_scheduler(
        store, MockRunner(), SchedulingPolicy(max_recoveries_per_attempt=1)
    )
    scheduler.tick()
    run = store.runs.list_for_task(store.db.conn, "t-prompt")[0]
    assert run.prompt_sha is not None and len(run.prompt_sha) == 64

    from boxporter.core.prompts import PromptService

    PromptService(store).set("executor", "New executor instructions")
    fail_current_run(store, "t-prompt", "done")
    add_ready_task(store, make_spec, "t-prompt-2")
    scheduler.tick(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    run2 = store.runs.list_for_task(store.db.conn, "t-prompt-2")[0]
    assert run2.prompt_sha != run.prompt_sha


def test_approval_api_requires_reauth(
    store: Store, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    with store.db.transaction():
        store.settings.set(store.db.conn, "admin_password_hash", hash_password("pw"))
    app = create_app(store)
    client = TestClient(app)
    client.post(
        "/api/auth/login", json={"username": "admin", "password": "pw"}
    )
    add_ready_task(store, make_spec, "t-api-approval")
    created = client.post(
        "/api/approvals",
        json={"task_id": "t-api-approval", "action": "read logs", "target": "x"},
        headers=CLIENT_HEADERS,
    )
    assert created.status_code == 200
    approval_id = created.json()["data"]["approval_id"]
    denied = client.post(
        f"/api/approvals/{approval_id}/approve", headers=CLIENT_HEADERS
    )
    assert denied.status_code == 401
    client.post(
        "/api/auth/reauthenticate", json={"password": "pw"}, headers=CLIENT_HEADERS
    )
    ok = client.post(
        f"/api/approvals/{approval_id}/approve", headers=CLIENT_HEADERS
    )
    assert ok.status_code == 200
    assert client.get("/api/approvals").json()["approvals"][0]["status"] == "approved"


def test_sse_cursor_replay_no_gaps(store: Store) -> None:
    """Disconnect/reconnect contract: after a partial stream, events from
    the last confirmed cursor cover everything exactly once."""
    with store.db.transaction():
        store.settings.set(store.db.conn, "admin_password_hash", hash_password("pw"))
    app = create_app(store)
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})

    # Two batches of events with a "disconnect" between them.
    start = latest_seq(store)
    with store.db.transaction():
        for i in range(3):
            store.events.append(
                store.db.conn,
                aggregate_type="test",
                aggregate_id="x",
                event_type="TEST_EVENT",
                actor_type="system",
                payload={"i": i},
            )
    first_batch = events_since(store, start)

    with store.db.transaction():
        for i in range(3, 6):
            store.events.append(
                store.db.conn,
                aggregate_type="test",
                aggregate_id="x",
                event_type="TEST_EVENT",
                actor_type="system",
                payload={"i": i},
            )
    # Reconnect from the last confirmed cursor (first batch tail).
    cursor = first_batch[-1].seq
    replay = events_since(store, cursor)
    assert [record.seq for record in replay] == [cursor + 1 + i for i in range(3)]
    # No gaps and no duplicates.
    all_events = events_since(store, start)
    assert [record.seq for record in all_events] == sorted(
        record.seq for record in all_events
    )
    assert len(all_events) == len({record.seq for record in all_events})
