"""Runner adapter contract (ADR-001, ADR-010).

Adapters translate between BoxPorter's Run model and a concrete Agent
Runtime. They must return machine-readable observations, never just
terminal text. Any capability gap must raise ``RunnerUnsupported`` rather
than silently weakening isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from boxporter.core.errors import BoxPorterError
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState


class RunnerUnsupported(BoxPorterError):
    """The runner cannot satisfy a requested capability."""


@dataclass(frozen=True)
class RunnerCapabilities:
    name: str
    version: str
    requires_model: bool
    supports_heartbeat: bool = False
    supports_checkpoint: bool = False
    supports_artifacts: bool = False
    supports_resume: bool = False


@dataclass(frozen=True)
class RunSpec:
    """Everything an adapter needs to start a session for one run."""

    run_id: str
    task_id: str
    attempt: int
    role: str
    workspace: str
    task: TaskSpec
    session_id: str
    runner_profile: str
    context_prompt: str = ""  # rendered role prompt + Context Pack


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    runtime_id: str
    pid: int | None = None


@dataclass(frozen=True)
class RunObservation:
    state: RunState
    last_activity_at: str
    pid: int | None = None
    exit_code: int | None = None
    usage_tokens: int = 0
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointRef:
    ref: str
    location: str
    sha256: str | None = None


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    uri: str
    sha256: str | None = None
    size_bytes: int | None = None


class RunnerAdapter(Protocol):
    def capabilities(self) -> RunnerCapabilities: ...

    def start(self, spec: RunSpec) -> RunHandle: ...

    def inspect(self, handle: RunHandle) -> RunObservation: ...

    def send(self, handle: RunHandle, message: str) -> None: ...

    def checkpoint(self, handle: RunHandle) -> CheckpointRef: ...

    def stop(self, handle: RunHandle, reason: str) -> StopResult: ...

    def resume(self, checkpoint: CheckpointRef, spec: RunSpec) -> RunHandle: ...

    def collect_artifacts(self, handle: RunHandle) -> list[ArtifactRef]: ...


class RunnerRegistry:
    def __init__(self) -> None:
        self._runners: dict[str, RunnerAdapter] = {}

    def register(self, runner: RunnerAdapter) -> None:
        self._runners[runner.capabilities().name] = runner

    def get(self, name: str) -> RunnerAdapter:
        if name not in self._runners:
            raise RunnerUnsupported(f"runner not registered: {name}")
        return self._runners[name]

    def names(self) -> list[str]:
        return sorted(self._runners)
