from __future__ import annotations

from pathlib import Path

import pytest

from boxporter.core.errors import ValidationError
from boxporter.core.schemas import TASK_SPEC_SCHEMA, TaskSpec, canonical_json


def valid_spec(tmp_path: Path, **kwargs: object) -> TaskSpec:
    values: dict[str, object] = {
        "task_id": "fix-login",
        "project_id": "app",
        "title": "Fix login loop",
        "objective": "Locate root cause, fix, add regression test.",
        "priority": 90,
        "risk_level": "high",
        "workspace": str(tmp_path),
        "acceptance_criteria": ("no more redirect",),
    }
    values.update(kwargs)
    return TaskSpec(**values)  # type: ignore[arg-type]


def test_valid_spec_passes(tmp_path: Path) -> None:
    spec = valid_spec(tmp_path)
    spec.validate()
    assert spec.to_dict()["schema"] == TASK_SPEC_SCHEMA


def test_roundtrip_spec(tmp_path: Path) -> None:
    spec = valid_spec(
        tmp_path,
        goal_id="g-1",
        dependencies=("dep-a", "dep-b"),
        required_evidence=("git_diff", "test_commands_with_exit_codes"),
    )
    loaded = TaskSpec.from_dict(spec.to_dict())
    assert loaded == spec


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_id": "bad id!"},
        {"project_id": ""},
        {"title": "  "},
        {"objective": ""},
        {"priority": 101},
        {"risk_level": "extreme"},
        {"workspace": ""},
        {"max_attempts": 0},
        {"timeout_seconds": 0},
        {"token_budget": 0},
        {"acceptance_criteria": ()},
        {"required_evidence": ("nope",)},
    ],
)
def test_invalid_specs(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        valid_spec(tmp_path, **kwargs).validate()


def test_wrong_schema_rejected(tmp_path: Path) -> None:
    value = valid_spec(tmp_path).to_dict()
    value["schema"] = "BOXPORTER_TASK_V1"
    with pytest.raises(ValidationError):
        TaskSpec.from_dict(value)


def test_canonical_json_is_order_independent() -> None:
    first = canonical_json({"a": 1, "b": {"x": [1, 2], "y": "z"}})
    second = canonical_json({"b": {"y": "z", "x": [1, 2]}, "a": 1})
    assert first == second
    assert first == '{"a":1,"b":{"x":[1,2],"y":"z"}}'
