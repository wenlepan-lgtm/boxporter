"""Web console API tests: auth, sessions, boxes, commands, events, audit."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boxporter.api.app import create_app
from boxporter.api.auth import hash_password
from boxporter.core.schemas import TaskSpec
from boxporter.storage.store import Store

CLIENT_HEADERS = {"X-BoxPorter-Client": "tests"}


@pytest.fixture
def client(
    store: Store,
    project: str,
    tmp_path: Path,
    make_spec: Callable[..., TaskSpec],
) -> TestClient:
    del make_spec
    with store.db.transaction():
        store.settings.set(store.db.conn, "admin_password_hash", hash_password("secret"))
    app = create_app(store)
    return TestClient(app)


def login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == 200


def test_unauthenticated_rejected(client: TestClient) -> None:
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/events/stream").status_code == 401


def test_login_flow_and_sessions(client: TestClient) -> None:
    bad = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert bad.status_code == 401
    login(client)
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["device_label"] == "web"

    reauth = client.post(
        "/api/auth/reauthenticate",
        json={"password": "secret"},
        headers=CLIENT_HEADERS,
    )
    assert reauth.status_code == 200

    logout = client.post("/api/auth/logout", headers=CLIENT_HEADERS)
    assert logout.status_code == 200
    assert client.get("/api/tasks").status_code == 401


def test_csrf_header_required(client: TestClient) -> None:
    login(client)
    response = client.post(
        "/api/tasks",
        json={"spec": {}},
    )
    assert response.status_code == 400
    assert "client header" in response.json()["detail"]


def test_task_lifecycle_via_api(
    client: TestClient, make_spec: Callable[..., TaskSpec], tmp_path: Path
) -> None:
    login(client)
    spec = make_spec("api-task")
    response = client.post(
        "/api/tasks",
        json={"spec": spec.to_dict()},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert client.post(
        "/api/tasks/api-task/ready", headers=CLIENT_HEADERS
    ).status_code == 200
    detail = client.get("/api/tasks/api-task").json()
    assert detail["task"]["state"] == "READY"
    assert detail["task"]["box"] == "PENDING"

    dashboard = client.get("/api/projects/demo/dashboard").json()
    assert dashboard["counts"]["PENDING"] == 1
    assert dashboard["counts"]["ACTIVE"] == 0

    assert client.post(
        "/api/tasks/api-task/cancel", headers=CLIENT_HEADERS
    ).status_code == 200
    assert client.get("/api/tasks/api-task").json()["task"]["state"] == "CANCELED"


def test_high_risk_requires_reauth(client: TestClient) -> None:
    login(client)
    response = client.post(
        "/api/settings/mode",
        json={"mode": "AWAY"},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 401
    assert "reauthentication" in response.json()["detail"]

    assert client.post(
        "/api/auth/reauthenticate",
        json={"password": "secret"},
        headers=CLIENT_HEADERS,
    ).status_code == 200
    response = client.post(
        "/api/settings/mode",
        json={"mode": "AWAY"},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 200
    assert client.get("/api/settings/mode").json()["mode"] == "AWAY"


def test_events_endpoint_and_audit(client: TestClient) -> None:
    login(client)
    assert client.post(
        "/api/settings/mode",
        json={"mode": "SUPERVISED"},
        headers=CLIENT_HEADERS,
    ).status_code in {200, 401}
    events = client.get("/api/events").json()
    assert events["latest_seq"] > 0
    types = [event["event_type"] for event in events["events"]]
    assert "PROJECT_CREATED" in types
    assert "AUTH_LOGIN" in types


def test_health_endpoint_public(client: TestClient) -> None:
    response = client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_remote_operations_are_audited(
    client: TestClient, make_spec: Callable[..., TaskSpec]
) -> None:
    login(client)
    spec = make_spec("audited-task")
    response = client.post(
        "/api/tasks",
        json={"spec": spec.to_dict()},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 200
    events = client.get("/api/events").json()["events"]
    remote = [event for event in events if event["event_type"] == "REMOTE_OPERATION"]
    assert len(remote) >= 1
    assert remote[0]["actor_type"] == "user"
    assert remote[0]["payload"]["ip"] is not None

    # Failed operations are audited as well.
    before = len(remote)
    assert client.post(
        "/api/tasks/no-such-task/ready", headers=CLIENT_HEADERS
    ).status_code == 409
    events = client.get("/api/events").json()["events"]
    remote = [event for event in events if event["event_type"] == "REMOTE_OPERATION"]
    assert len(remote) > before
    assert remote[-1]["payload"]["operation"] == "failed"
