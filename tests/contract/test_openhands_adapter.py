"""Adapter contract tests for the OpenHands runner (ADR-010).

These run against a fake runtime bridge: no model, no server, no SDK
install required. The real SDK bridge is covered by an env-gated test in
``test_openhands_sdk_real.py``.
"""

from __future__ import annotations

import pytest

from boxporter.core.errors import BoxPorterError
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState
from boxporter.runners.base import RunnerUnsupported, RunSpec
from boxporter.runners.openhands import (
    BridgeObservation,
    OpenHandsAdapter,
    OpenHandsConfig,
)


class FakeBridge:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, object]] = {}
        self.started_prompts: list[str] = []
        self.stopped: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.next_state = RunState.RUNNING

    def start(self, spec: RunSpec, config: OpenHandsConfig) -> object:
        session = {"spec": spec, "config": config, "state": RunState.RUNNING, "events": 0}
        self.sessions[spec.session_id] = session
        self.started_prompts.append(f"{spec.role}:{spec.task.objective}")
        return session

    def inspect(self, session: object) -> BridgeObservation:
        assert isinstance(session, dict)
        session["events"] = int(session.get("events", 0)) + 1
        state = RunState(session["state"]) if "state" in session else self.next_state
        return BridgeObservation(state=state, last_activity_at="t", events=int(session["events"]))

    def send(self, session: object, message: str) -> None:
        assert isinstance(session, dict)
        self.sent.append((str(session["spec"].session_id), message))

    def stop(self, session: object, reason: str) -> None:
        assert isinstance(session, dict)
        self.stopped.append((str(session["spec"].session_id), reason))
        session["state"] = RunState.CANCELED

    def close(self, session: object) -> None:
        pass


_run_counter = 0


def make_spec(role: str = "executor", task_id: str = "task-1") -> RunSpec:
    global _run_counter
    _run_counter += 1
    spec = TaskSpec(
        task_id=task_id,
        project_id="demo",
        title="Demo task",
        objective="Implement the thing.",
        priority=50,
        risk_level="low",
        workspace="/tmp/demo",
        acceptance_criteria=("works",),
    )
    return RunSpec(
        run_id=f"run-{_run_counter}",
        task_id=task_id,
        attempt=1,
        role=role,
        workspace="/tmp/demo",
        task=spec,
        session_id=f"session-{role}-{_run_counter}",
        runner_profile="boxporter-executor",
    )


@pytest.fixture
def adapter() -> tuple[OpenHandsAdapter, FakeBridge]:
    bridge = FakeBridge()
    return OpenHandsAdapter(OpenHandsConfig(model="fake/model"), bridge=bridge), bridge


def test_capabilities(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, _ = adapter
    capabilities = client.capabilities()
    assert capabilities.name == "openhands"
    assert capabilities.requires_model is True
    assert capabilities.supports_resume is False


def test_start_inspect_success(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, bridge = adapter
    handle = client.start(make_spec())
    observation = client.inspect(handle)
    assert observation.state == RunState.RUNNING

    bridge.sessions[min(bridge.sessions)]["state"] = RunState.SUCCEEDED
    observation = client.inspect(handle)
    assert observation.state == RunState.SUCCEEDED


def test_start_crash_mapped(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, bridge = adapter
    handle = client.start(make_spec())
    bridge.sessions[min(bridge.sessions)]["state"] = RunState.CRASHED
    assert client.inspect(handle).state == RunState.CRASHED


def test_sessions_are_isolated(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, bridge = adapter
    handle_a = client.start(make_spec(task_id="task-a", role="executor"))
    handle_b = client.start(make_spec(task_id="task-b", role="reviewer"))
    assert len(bridge.sessions) == 2
    session_ids = sorted(bridge.sessions)
    assert session_ids[0] != session_ids[1]
    assert bridge.sessions[session_ids[0]] is not bridge.sessions[session_ids[1]]
    bridge.sessions[session_ids[0]]["state"] = RunState.CRASHED
    assert client.inspect(handle_a).state == RunState.CRASHED
    assert client.inspect(handle_b).state == RunState.RUNNING


def test_stop_closes_session(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, bridge = adapter
    handle = client.start(make_spec())
    session_id = min(bridge.sessions)
    result = client.stop(handle, "user requested")
    assert result.stopped
    assert (session_id, "user requested") in bridge.stopped
    with pytest.raises(BoxPorterError, match="no openhands session"):
        client.inspect(handle)


def test_unsupported_operations_raise(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, _ = adapter
    handle = client.start(make_spec())
    with pytest.raises(RunnerUnsupported, match="checkpoint"):
        client.checkpoint(handle)
    with pytest.raises(RunnerUnsupported, match="resume"):
        from boxporter.runners.base import CheckpointRef

        client.resume(CheckpointRef(ref="x", location="y"), make_spec())


def test_send_forwards_message(adapter: tuple[OpenHandsAdapter, FakeBridge]) -> None:
    client, bridge = adapter
    handle = client.start(make_spec())
    client.send(handle, "continue")
    session_id = min(bridge.sessions)
    assert (session_id, "continue") in bridge.sent
