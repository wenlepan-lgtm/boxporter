from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from boxporter.application.commands import CreateProject
from boxporter.core.schemas import TaskSpec
from boxporter.storage.db import Database
from boxporter.storage.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Database(tmp_path / "data" / "boxporter.sqlite")
    db.open()
    yield Store(db)
    db.close()


@pytest.fixture
def project(store: Store, tmp_path: Path) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = store.execute(
        CreateProject(
            project_id="demo",
            name="Demo Project",
            workspace_root=str(workspace),
            actor_type="user",
            actor_id="human-1",
        )
    )
    assert result.ok
    return "demo"


def _make_spec(
    task_id: str,
    workspace: Path,
    *,
    project_id: str = "demo",
    **kwargs: object,
) -> TaskSpec:
    defaults: dict[str, object] = {
        "title": f"Task {task_id}",
        "objective": f"Implement {task_id} with tests.",
        "priority": 50,
        "risk_level": "medium",
        "workspace": str(workspace),
        "acceptance_criteria": ("tests pass",),
    }
    defaults.update(kwargs)
    return TaskSpec(task_id=task_id, project_id=project_id, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def make_spec(tmp_path: Path, project: str) -> Callable[..., TaskSpec]:
    return lambda task_id, **kwargs: _make_spec(task_id, tmp_path / "workspace", **kwargs)
