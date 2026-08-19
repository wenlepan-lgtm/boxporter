"""In-process mock runner for tests and end-to-end simulation.

Never calls a model. Scriptable observations make it possible to drive
crash / stall / success scenarios deterministically.
"""

from __future__ import annotations

from boxporter.core.clock import now_iso
from boxporter.core.state import RunState

from .base import (
    ArtifactRef,
    CheckpointRef,
    RunHandle,
    RunnerCapabilities,
    RunObservation,
    RunSpec,
    StopResult,
)

MOCK_CAPABILITIES = RunnerCapabilities(
    name="mock",
    version="builtin",
    requires_model=False,
    supports_heartbeat=True,
    supports_checkpoint=True,
    supports_artifacts=True,
    supports_resume=True,
)


class MockRunner:
    def __init__(self) -> None:
        self.started: list[RunSpec] = []
        self.observations: dict[str, RunObservation] = {}
        self.messages: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []
        self.checkpoints: list[CheckpointRef] = []

    def capabilities(self) -> RunnerCapabilities:
        return MOCK_CAPABILITIES

    def start(self, spec: RunSpec) -> RunHandle:
        self.started.append(spec)
        self.observations[spec.run_id] = RunObservation(
            state=RunState.STARTING, last_activity_at=now_iso()
        )
        return RunHandle(run_id=spec.run_id, runtime_id=f"mock://{spec.run_id}")

    def inspect(self, handle: RunHandle) -> RunObservation:
        return self.observations.get(
            handle.run_id, RunObservation(state=RunState.RUNNING, last_activity_at=now_iso())
        )

    def set_observation(self, run_id: str, observation: RunObservation) -> None:
        self.observations[run_id] = observation

    def send(self, handle: RunHandle, message: str) -> None:
        self.messages.append((handle.run_id, message))

    def checkpoint(self, handle: RunHandle) -> CheckpointRef:
        ref = CheckpointRef(
            ref=f"ckpt-{len(self.checkpoints) + 1}", location=f"mock://{handle.run_id}"
        )
        self.checkpoints.append(ref)
        return ref

    def stop(self, handle: RunHandle, reason: str) -> StopResult:
        self.stopped.append((handle.run_id, reason))
        return StopResult(stopped=True)

    def resume(self, checkpoint: CheckpointRef, spec: RunSpec) -> RunHandle:
        self.started.append(spec)
        return RunHandle(run_id=spec.run_id, runtime_id=f"mock://{spec.run_id}")

    def collect_artifacts(self, handle: RunHandle) -> list[ArtifactRef]:
        return []
