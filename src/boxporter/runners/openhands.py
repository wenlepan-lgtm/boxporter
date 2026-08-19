"""OpenHands runner adapter (ADR-001, ADR-010, ADR-011).

The adapter wraps the OpenHands Software Agent SDK (pinned version, see
docs/operations/runner-versions.md). All SDK imports are lazy and
optional: BoxPorter Core never imports OpenHands.

A ``RuntimeBridge`` separates the adapter contract from the SDK so
contract tests run against a fake bridge without a real agent server
(ADR-010). The default ``SDKBridge`` speaks the real SDK API.
"""

from __future__ import annotations

import importlib.metadata
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from boxporter.core.clock import now_iso
from boxporter.core.errors import BoxPorterError
from boxporter.core.state import RunState

from .base import (
    ArtifactRef,
    CheckpointRef,
    RunHandle,
    RunnerCapabilities,
    RunnerUnsupported,
    RunObservation,
    RunSpec,
    StopResult,
)

PINNED_SDK_VERSION = "1.42.1"


def sdk_version() -> str:
    try:
        return importlib.metadata.version("openhands-sdk")
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


@dataclass(frozen=True)
class OpenHandsConfig:
    model: str = "gpt-5.5"
    api_key: str | None = None
    host: str | None = None  # None -> local SDK; else Agent Server URL
    cli_mode: bool = True
    max_iterations: int = 500


@dataclass(frozen=True)
class BridgeObservation:
    state: RunState
    last_activity_at: str
    events: int = 0
    detail: dict[str, object] = field(default_factory=dict)


class RuntimeBridge(Protocol):
    def start(self, spec: RunSpec, config: OpenHandsConfig) -> object: ...

    def inspect(self, session: object) -> BridgeObservation: ...

    def send(self, session: object, message: str) -> None: ...

    def stop(self, session: object, reason: str) -> None: ...

    def close(self, session: object) -> None: ...


class SDKBridge:
    """Real bridge over openhands-sdk (lazy import, optional dependency)."""

    def start(self, spec: RunSpec, config: OpenHandsConfig) -> object:
        try:
            from openhands.sdk import LLM, Conversation, Workspace
            from openhands.tools.preset.default import get_default_agent
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RunnerUnsupported(
                "openhands extra not installed: uv sync --extra openhands"
            ) from exc

        llm = LLM(model=config.model, api_key=config.api_key)
        agent = get_default_agent(llm=llm, cli_mode=config.cli_mode)
        workspace = Workspace(
            host=cast(Any, config.host),
            working_dir=spec.workspace,
            api_key=config.api_key if config.host else None,
        )
        events: list[object] = []
        conversation: Any = Conversation(
            agent=agent,
            workspace=workspace,
            callbacks=[events.append],
            max_iteration_per_run=config.max_iterations,
        )
        conversation.send_message(self._prompt(spec))
        thread = threading.Thread(
            target=conversation.run, name=f"openhands-{spec.run_id}", daemon=True
        )
        session: dict[str, object] = {
            "conversation": conversation,
            "thread": thread,
            "events": events,
            "started_at": now_iso(),
            "finished": False,
            "error": None,
        }
        thread.start()
        return session

    def inspect(self, session: object) -> BridgeObservation:
        assert isinstance(session, dict)
        thread = session["thread"]
        assert isinstance(thread, threading.Thread)
        events = session["events"]
        assert isinstance(events, list)
        if thread.is_alive():
            return BridgeObservation(
                state=RunState.RUNNING,
                last_activity_at=now_iso(),
                events=len(events),
            )
        error = session.get("error")
        return BridgeObservation(
            state=RunState.CRASHED if error is not None else RunState.SUCCEEDED,
            last_activity_at=now_iso(),
            events=len(events),
            detail={"error": str(error)} if error is not None else {},
        )

    def send(self, session: object, message: str) -> None:
        assert isinstance(session, dict)
        conversation: Any = session["conversation"]
        assert conversation is not None
        conversation.send_message(message)

    def stop(self, session: object, reason: str) -> None:
        assert isinstance(session, dict)
        conversation: Any = session["conversation"]
        assert conversation is not None
        try:
            conversation.interrupt(reason)
        except AttributeError:
            conversation.pause()
        self.close(session)

    def close(self, session: object) -> None:
        assert isinstance(session, dict)
        conversation: Any = session["conversation"]
        thread = session["thread"]
        if conversation is not None:
            try:
                conversation.close()
            except Exception:  # noqa: BLE001, S110 - best effort cleanup
                pass
        if isinstance(thread, threading.Thread) and thread.is_alive():
            thread.join(timeout=5)
        session["finished"] = True

    @staticmethod
    def _prompt(spec: RunSpec) -> str:
        if spec.context_prompt:
            return spec.context_prompt
        role_block = {
            "executor": (
                "You are the EXECUTOR for this task. Work inside the provided"
                " workspace. Implement the objective, run verification commands"
                " yourself, and when done write three files under the reports"
                " directory: result.md (compact conclusions), verify.md (real"
                " commands with exit codes), executor.md (facts and remaining"
                " risks). You cannot mark your own work as PASS."
            ),
            "reviewer": (
                "You are the independent REVIEWER. The submission is frozen:"
                " you may read code and run tests, but must not modify the"
                " submission. Write review.md and review_evidence.json with"
                " test_exit_code and production_risk, then conclude exactly"
                " PASS, REVISE or BLOCKED per acceptance criteria."
            ),
        }[spec.role]
        criteria = "\n".join(f"- {item}" for item in spec.task.acceptance_criteria)
        return (
            f"{role_block}\n\n"
            f"Task: {spec.task.title}\n"
            f"Objective: {spec.task.objective}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"Workspace: {spec.workspace}\n"
            f"Task id: {spec.task_id}, attempt {spec.attempt}, session"
            f" {spec.session_id}.\n"
        )


class OpenHandsAdapter:
    """RunnerAdapter over the OpenHands SDK."""

    def __init__(
        self,
        config: OpenHandsConfig | None = None,
        bridge: RuntimeBridge | None = None,
    ):
        self.config = config or OpenHandsConfig()
        self.bridge: RuntimeBridge = bridge or SDKBridge()
        self._sessions: dict[str, object] = {}

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            name="openhands",
            version=f"sdk-{sdk_version()}" if sdk_version() != "unavailable" else "sdk-unavailable",
            requires_model=True,
            supports_heartbeat=True,
            supports_checkpoint=False,
            supports_artifacts=True,
            supports_resume=False,
        )

    def start(self, spec: RunSpec) -> RunHandle:
        session = self.bridge.start(spec, self.config)
        self._sessions[spec.run_id] = session
        return RunHandle(run_id=spec.run_id, runtime_id=f"openhands://{spec.session_id}", pid=None)

    def inspect(self, handle: RunHandle) -> RunObservation:
        observation = self.bridge.inspect(self._session_for(handle))
        detail = dict(observation.detail)
        detail["events"] = observation.events
        return RunObservation(
            state=observation.state,
            last_activity_at=observation.last_activity_at,
            usage_tokens=0,
            detail=detail,
        )

    def send(self, handle: RunHandle, message: str) -> None:
        self.bridge.send(self._session_for(handle), message)

    def checkpoint(self, handle: RunHandle) -> CheckpointRef:
        raise RunnerUnsupported("openhands adapter does not support checkpoints")

    def stop(self, handle: RunHandle, reason: str) -> StopResult:
        self.bridge.stop(self._session_for(handle), reason)
        self._sessions.pop(handle.run_id, None)
        return StopResult(stopped=True, detail={"reason": reason})

    def resume(self, checkpoint: CheckpointRef, spec: RunSpec) -> RunHandle:
        raise RunnerUnsupported("openhands adapter does not support resume")

    def collect_artifacts(self, handle: RunHandle) -> list[ArtifactRef]:
        return []

    def _session_for(self, handle: RunHandle) -> object:
        session = self._sessions.get(handle.run_id)
        if session is None:
            raise BoxPorterError(f"no openhands session for run {handle.run_id}")
        return session
