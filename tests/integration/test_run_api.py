"""Run API, componentized health and blockers endpoint tests
(UI/backend interaction audit, stage A)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxporter.api.app import create_app
from boxporter.api.auth import hash_password
from boxporter.application.commands import (
    BlockTask,
    CreateTask,
    MarkRunRunning,
    ReadyTask,
    StartExecutorRun,
)
from boxporter.core.schemas import TaskSpec
from boxporter.storage.store import Store

CLIENT_HEADERS = {"X-BoxPorter-Client": "tests"}


@pytest.fixture
def client(store: Store, project: str, make_spec: Callable[..., TaskSpec]) -> TestClient:
    del make_spec
    with store.db.transaction():
        store.settings.set(store.db.conn, "admin_password_hash", hash_password("pw"))
    app = create_app(store)
    test_client = TestClient(app)
    test_client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return test_client


def start_run(store: Store, make_spec: Callable[..., TaskSpec], task_id: str) -> str:
    assert store.execute(CreateTask(spec=make_spec(task_id), actor_type="user")).ok
    assert store.execute(ReadyTask(task_id=task_id, actor_type="user")).ok
    result = store.execute(
        StartExecutorRun(task_id=task_id, runner="mock", identity="a",
                         session_id="s", actor_type="daemon")
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    return run_id


def reauth(client: TestClient) -> None:
    response = client.post(
        "/api/auth/reauthenticate", json={"password": "pw"}, headers=CLIENT_HEADERS
    )
    assert response.status_code == 200


def test_run_details_endpoint(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = start_run(store, make_spec, "t-run")
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == run_id
    assert body["run"]["state"] == "RUNNING"
    assert body["task_id"] == "t-run"
    assert body["attempt"] == 1
    assert body["run"]["prompt_sha"] or body["run"]["prompt_sha"] is None


def test_run_events_endpoint_with_cursor(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = start_run(store, make_spec, "t-run-events")
    first = client.get(f"/api/runs/{run_id}/events").json()
    assert first["events"], "run lifecycle events expected"
    cursor = first["events"][0]["seq"]
    second = client.get(f"/api/runs/{run_id}/events?after_cursor={cursor}").json()
    for record in second["events"]:
        assert record["seq"] > cursor


def test_run_missing_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/no-such-run").status_code == 404


def test_resume_running_run_rejected(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = start_run(store, make_spec, "t-resume-running")
    reauth(client)
    response = client.post(f"/api/runs/{run_id}/resume", headers=CLIENT_HEADERS)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "RESUME_UNSUPPORTED"
    assert body["hint"]


def test_resume_crashed_run_points_to_retry(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    from boxporter.application.commands import FailRun

    run_id = start_run(store, make_spec, "t-resume-crashed")
    store.execute(FailRun(run_id=run_id, kind="crash", stop_reason="boom",
                          actor_type="daemon"))
    reauth(client)
    response = client.post(f"/api/runs/{run_id}/resume", headers=CLIENT_HEADERS)
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "RESUME_UNSUPPORTED"
    assert "/retry" in body["hint"]
    assert body["trace_id"]


def test_resume_stalled_run_transitions_to_running(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = start_run(store, make_spec, "t-resume-stalled")
    from boxporter.core.state import RunState

    store.runs.update_state(store.db.conn, run_id, RunState.STALLED)
    reauth(client)
    response = client.post(f"/api/runs/{run_id}/resume", headers=CLIENT_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert store.runs.get(store.db.conn, run_id).state.value == "RUNNING"


def test_health_components(
    client: TestClient, store: Store, tmp_path: Path
) -> None:
    health = client.get("/api/system/health").json()
    components = health["components"]
    for name in (
        "control_plane",
        "database",
        "openhands",
        "runner_registry",
        "disk",
        "backup",
        "remote_access",
    ):
        assert name in components, f"missing health component: {name}"
    assert isinstance(health["warnings"], list)
    assert health["components"]["remote_access"]["status"] == "local-only"


def test_blockers_endpoint(
    client: TestClient, store: Store, make_spec: Callable[..., TaskSpec]
) -> None:
    run_id = start_run(store, make_spec, "t-blocked")
    assert store.execute(
        BlockTask(task_id="t-blocked", reason="device offline",
                  probe_command=("true",), actor_type="user")
    ).ok
    del run_id
    response = client.get("/api/blockers")
    assert response.status_code == 200
    blockers = response.json()["blockers"]
    assert any(item["task_id"] == "t-blocked" for item in blockers)
    blocker = next(item for item in blockers if item["task_id"] == "t-blocked")
    assert blocker["probe_command"] == ["true"]
    assert blocker["next_probe_at"]
