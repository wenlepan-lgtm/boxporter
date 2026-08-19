"""Task entry-point tests: import endpoint, structured errors, readiness
(remediation §5 功能验收)."""

from __future__ import annotations

import json
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
def client(store: Store, project: str, make_spec: Callable[..., TaskSpec]) -> TestClient:
    del make_spec
    with store.db.transaction():
        store.settings.set(store.db.conn, "admin_password_hash", hash_password("pw"))
    app = create_app(store)
    test_client = TestClient(app)
    test_client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return test_client


def spec_payload(task_id: str, tmp_path: Path, **kwargs: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "BOXPORTER_TASK_V2",
        "task_id": task_id,
        "project_id": "demo",
        "title": f"任务 {task_id}",
        "objective": "实现并验证",
        "priority": 50,
        "risk_level": "low",
        "workspace": str(tmp_path / "workspace"),
        "acceptance_criteria": ["测试通过"],
    }
    payload.update(kwargs)
    return payload


def test_import_via_json_text(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/tasks/import",
        data={"spec_json": json.dumps(spec_payload("import-1", tmp_path))},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["task_id"] == "import-1"
    assert body["trace_id"]
    assert response.headers["X-Trace-Id"] == body["trace_id"]


def test_import_via_file_upload(client: TestClient, tmp_path: Path) -> None:
    payload = json.dumps(spec_payload("import-2", tmp_path))
    response = client.post(
        "/api/tasks/import",
        files={"file": ("spec.json", payload, "application/json")},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["task_id"] == "import-2"


def test_import_invalid_json_is_structured(client: TestClient) -> None:
    response = client.post(
        "/api/tasks/import",
        data={"spec_json": "{not json"},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_JSON"
    assert body["field"] and "line" in body["field"]
    assert body["hint"]
    assert body["trace_id"]


def test_import_empty_input(client: TestClient) -> None:
    response = client.post("/api/tasks/import", headers=CLIENT_HEADERS)
    assert response.status_code == 400
    assert response.json()["code"] == "EMPTY_INPUT"


def test_import_invalid_schema_structured(client: TestClient, tmp_path: Path) -> None:
    payload = spec_payload("import-3", tmp_path)
    del payload["objective"]
    response = client.post(
        "/api/tasks/import",
        data={"spec_json": json.dumps(payload)},
        headers=CLIENT_HEADERS,
    )
    assert response.status_code == 409
    body = response.json()
    error = body["error"]
    assert error["code"] == "VALIDATION"
    assert error["message"]
    assert error["trace_id"]
    assert error["hint"]


def test_import_duplicate_conflict(client: TestClient, tmp_path: Path) -> None:
    payload = spec_payload("import-4", tmp_path)
    first = client.post(
        "/api/tasks/import",
        data={"spec_json": json.dumps(payload)},
        headers=CLIENT_HEADERS,
    )
    assert first.status_code == 200
    second = client.post(
        "/api/tasks/import",
        data={"spec_json": json.dumps(payload)},
        headers=CLIENT_HEADERS,
    )
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["field"] == "task_id"
    assert "换一个 task_id" in error["hint"]


def test_import_idempotent_with_key(client: TestClient, tmp_path: Path) -> None:
    payload = spec_payload("import-5", tmp_path)
    headers = {**CLIENT_HEADERS, "Idempotency-Key": "op-import-5"}
    first = client.post(
        "/api/tasks/import", data={"spec_json": json.dumps(payload)}, headers=headers
    )
    second = client.post(
        "/api/tasks/import", data={"spec_json": json.dumps(payload)}, headers=headers
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["data"] == first.json()["data"]


def test_readiness_ready(client: TestClient, tmp_path: Path) -> None:
    payload = spec_payload("ready-1", tmp_path)
    response = client.post("/api/tasks", json={"spec": payload}, headers=CLIENT_HEADERS)
    assert response.status_code == 200
    readiness = client.get("/api/tasks/ready-1/readiness").json()
    assert readiness["ready"] is True
    assert readiness["gaps"] == []
    assert readiness["state"] == "PENDING"


def test_readiness_gaps_structured(client: TestClient, tmp_path: Path) -> None:
    payload = spec_payload("ready-2", tmp_path, workspace=str(tmp_path / "missing-dir"))
    assert client.post("/api/tasks", json={"spec": payload}, headers=CLIENT_HEADERS).status_code == 200
    readiness = client.get("/api/tasks/ready-2/readiness").json()
    assert readiness["ready"] is False
    fields = {gap["field"] for gap in readiness["gaps"]}
    assert "workspace" in fields
    gap = next(gap for gap in readiness["gaps"] if gap["field"] == "workspace")
    assert gap["hint"]


def test_readiness_dependency_gap(client: TestClient, tmp_path: Path) -> None:
    dep_payload = spec_payload("dep-base", tmp_path)
    assert client.post("/api/tasks", json={"spec": dep_payload}, headers=CLIENT_HEADERS).status_code == 200
    payload = spec_payload("dep-child", tmp_path, dependencies=["dep-base"])
    assert client.post("/api/tasks", json={"spec": payload}, headers=CLIENT_HEADERS).status_code == 200
    readiness = client.get("/api/tasks/dep-child/readiness").json()
    assert readiness["ready"] is False
    gap = next(gap for gap in readiness["gaps"] if gap["field"] == "dependencies")
    assert "PASSED" in gap["hint"]


def test_ready_failure_is_structured(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/tasks/no-such-task/ready", headers=CLIENT_HEADERS
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["trace_id"]
