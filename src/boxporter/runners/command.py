"""Command runner: spawns an external agent command per run (V0.2 compat).

The command is executed as an argument array (never via shell string
concatenation) with BoxPorter context in the environment. The process
group is detached so a BoxPorter restart does not kill the session, and
the pid is stored on the lease so the watchdog can observe liveness.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from boxporter.core.clock import now_iso
from boxporter.core.errors import BoxPorterError
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

PLACEHOLDERS = ("{root}", "{workspace}", "{task}", "{run_id}", "{role}")


class CommandRunner:
    def __init__(self, command: list[str], name: str = "command", version: str = "builtin"):
        if not command or not all(isinstance(item, str) and item for item in command):
            raise BoxPorterError("command runner requires a non-empty argument array")
        self.command = command
        self._name = name
        self._version = version
        self._spawned: dict[str, subprocess.Popen[bytes]] = {}

    def capabilities(self) -> RunnerCapabilities:
        return RunnerCapabilities(
            name=self._name,
            version=self._version,
            requires_model=True,
        )

    def start(self, spec: RunSpec) -> RunHandle:
        argv = [
            self._substitute(item, spec) for item in self.command
        ]
        env = os.environ.copy()
        env.update(
            BOXPORTER_ROOT=str(Path(spec.workspace).anchor),  # replaced by caller context
            BOXPORTER_RUN_ID=spec.run_id,
            BOXPORTER_TASK_ID=spec.task_id,
            BOXPORTER_ROLE=spec.role,
            BOXPORTER_SESSION_ID=spec.session_id,
            BOXPORTER_WORKSPACE=spec.workspace,
        )
        process = subprocess.Popen(
            argv,
            cwd=spec.workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._spawned[spec.run_id] = process
        return RunHandle(run_id=spec.run_id, runtime_id=f"pid://{process.pid}", pid=process.pid)

    def inspect(self, handle: RunHandle) -> RunObservation:
        process = self._spawned.get(handle.run_id)
        if process is None and handle.pid is not None:
            # Re-attached after a daemon restart: the pid is the only truth
            # we have, so liveness is observed via the OS, not a fake state.
            try:
                os.kill(handle.pid, 0)
            except ProcessLookupError:
                return RunObservation(
                    state=RunState.CRASHED,
                    last_activity_at=now_iso(),
                    pid=handle.pid,
                    detail={"reason": "process no longer alive"},
                )
            except PermissionError:
                pass
            return RunObservation(
                state=RunState.RUNNING,
                last_activity_at=now_iso(),
                pid=handle.pid,
                detail={"reason": "reattached-by-pid"},
            )
        if process is None:
            return RunObservation(
                state=RunState.CRASHED,
                last_activity_at=now_iso(),
                detail={"reason": "process not tracked"},
            )
        exit_code = process.poll()
        if exit_code is None:
            return RunObservation(
                state=RunState.RUNNING,
                last_activity_at=now_iso(),
                pid=process.pid,
            )
        return RunObservation(
            state=RunState.SUCCEEDED if exit_code == 0 else RunState.CRASHED,
            last_activity_at=now_iso(),
            pid=process.pid,
            exit_code=exit_code,
        )

    def send(self, handle: RunHandle, message: str) -> None:
        process = self._spawned.get(handle.run_id)
        if process is None or process.stdin is None:
            raise BoxPorterError("command runner has no writable stdin")
        process.stdin.write(message.encode() + b"\n")
        process.stdin.flush()

    def checkpoint(self, handle: RunHandle) -> CheckpointRef:
        raise BoxPorterError("command runner does not support checkpoints")

    def stop(self, handle: RunHandle, reason: str) -> StopResult:
        process = self._spawned.get(handle.run_id)
        if process is None:
            if handle.pid is not None:
                try:
                    os.kill(handle.pid, signal.SIGTERM)
                    self._spawned.pop(handle.run_id, None)
                    return StopResult(stopped=True, detail={"reason": reason, "state": "terminated-by-pid"})
                except ProcessLookupError:
                    return StopResult(stopped=True, detail={"reason": reason, "state": "already-dead"})
            return StopResult(stopped=True, detail={"reason": reason, "state": "untracked"})
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._spawned.pop(handle.run_id, None)
        return StopResult(stopped=True, detail={"reason": reason})

    def resume(self, checkpoint: CheckpointRef, spec: RunSpec) -> RunHandle:
        raise BoxPorterError("command runner does not support resume")

    def collect_artifacts(self, handle: RunHandle) -> list[ArtifactRef]:
        return []

    @staticmethod
    def _substitute(item: str, spec: RunSpec) -> str:
        return (
            item.replace("{root}", "")
            .replace("{workspace}", spec.workspace)
            .replace("{task}", spec.task_id)
            .replace("{run_id}", spec.run_id)
            .replace("{role}", spec.role)
        )
