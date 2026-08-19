"""Env-gated test for the real OpenHands SDK bridge.

Runs only when the ``openhands`` extra is installed
(``uv sync --extra openhands``). Verifies the pinned SDK imports and that
the bridge's prompt/API surface matches the contract.
"""

from __future__ import annotations

import pytest

openhands_sdk = pytest.importorskip("openhands.sdk")

from boxporter.runners.openhands import PINNED_SDK_VERSION, SDKBridge, sdk_version


def test_pinned_sdk_version_available() -> None:
    version = sdk_version()
    assert version != "unavailable"
    assert version == PINNED_SDK_VERSION, (
        f"sdk {version} differs from pinned {PINNED_SDK_VERSION}:"
        " update docs/operations/runner-versions.md and re-run contract tests"
    )


def test_bridge_prompt_contains_contract_fields() -> None:
    from boxporter.core.schemas import TaskSpec
    from boxporter.runners.base import RunSpec

    spec = TaskSpec(
        task_id="task-1",
        project_id="demo",
        title="Demo",
        objective="Make it work.",
        priority=50,
        risk_level="low",
        workspace="/tmp/demo",
        acceptance_criteria=("works",),
    )
    run_spec = RunSpec(
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        role="executor",
        workspace="/tmp/demo",
        task=spec,
        session_id="session-1",
        runner_profile="boxporter-executor",
    )
    prompt = SDKBridge._prompt(run_spec)
    assert "EXECUTOR" in prompt
    assert "Make it work." in prompt
    assert "works" in prompt
    assert "session-1" in prompt
