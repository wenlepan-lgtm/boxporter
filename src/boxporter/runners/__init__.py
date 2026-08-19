"""Runner factory: single entry point for CLI and daemon (fix-guide P0-A).

Environment-driven configuration; never silently runs on an empty
registry — callers get a clear error explaining how to configure a
runner.

Configuration:
- BOXPORTER_RUNNER: mock | openhands | dsh | command | auto (default)
- BOXPORTER_OPENHANDS_MODEL / BOXPORTER_OPENHANDS_API_KEY / BOXPORTER_OPENHANDS_HOST
- BOXPORTER_DSH_COMMAND: JSON array overriding the DSH command template
- BOXPORTER_EXECUTOR_COMMAND: JSON array for the command runner
- BOXPORTER_ALLOW_MOCK: "1" permits the mock runner (demo/dry-run mode)
"""

from __future__ import annotations

import json
import os

from boxporter.core.errors import BoxPorterError

from .base import RunnerRegistry
from .command import CommandRunner
from .deepseek_harness import DeepSeekHarnessRunner
from .mock import MockRunner
from .openhands import OpenHandsAdapter, OpenHandsConfig


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def _env_json_list(name: str) -> list[str] | None:
    value = _env(name)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BoxPorterError(f"{name} must be a JSON array: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise BoxPorterError(f"{name} must be a JSON array of strings")
    return parsed


def build_registry(require_runner: bool = True) -> RunnerRegistry:
    """Build the runner registry from the environment.

    With ``require_runner`` (default) an empty registry is an error, so
    daemon/tick never silently idle on an unconfigured machine.
    """
    registry = RunnerRegistry()
    mode = (_env("BOXPORTER_RUNNER") or "auto").lower()
    openhands_key = _env("BOXPORTER_OPENHANDS_API_KEY")
    openhands_model = _env("BOXPORTER_OPENHANDS_MODEL")
    openhands_host = _env("BOXPORTER_OPENHANDS_HOST")
    dsh_command = _env_json_list("BOXPORTER_DSH_COMMAND")
    command = _env_json_list("BOXPORTER_EXECUTOR_COMMAND")

    def add_openhands() -> None:
        registry.register(
            OpenHandsAdapter(
                OpenHandsConfig(
                    model=openhands_model or "gpt-5.5",
                    api_key=openhands_key,
                    host=openhands_host,
                )
            )
        )

    if mode == "mock":
        if _env("BOXPORTER_ALLOW_MOCK") != "1" and require_runner:
            raise BoxPorterError(
                "BOXPORTER_RUNNER=mock requires BOXPORTER_ALLOW_MOCK=1"
                " (mock runner is for demo/dry-run only)"
            )
        registry.register(MockRunner())
    elif mode == "openhands":
        if openhands_key is None and require_runner:
            raise BoxPorterError(
                "BOXPORTER_RUNNER=openhands requires BOXPORTER_OPENHANDS_API_KEY"
                " (never store keys in config files)"
            )
        add_openhands()
    elif mode == "dsh":
        registry.register(DeepSeekHarnessRunner(command=dsh_command))
    elif mode == "command":
        if not command:
            raise BoxPorterError(
                "BOXPORTER_RUNNER=command requires BOXPORTER_EXECUTOR_COMMAND"
                " (JSON array)"
            )
        registry.register(CommandRunner(command))
    elif mode == "auto":
        if openhands_key is not None:
            add_openhands()
        if dsh_command is not None:
            registry.register(DeepSeekHarnessRunner(command=dsh_command))
        if command is not None:
            registry.register(CommandRunner(command))
        if not registry.names() and _env("BOXPORTER_ALLOW_MOCK") == "1":
            registry.register(MockRunner())
    else:
        raise BoxPorterError(
            f"unknown BOXPORTER_RUNNER: {mode}"
            " (mock | openhands | dsh | command | auto)"
        )

    if require_runner and not registry.names():
        raise BoxPorterError(
            "no runner configured. Set one of:\n"
            "  BOXPORTER_OPENHANDS_API_KEY + BOXPORTER_OPENHANDS_MODEL (OpenHands)\n"
            "  BOXPORTER_DSH_COMMAND='[\"deepseek-harness\", ...]' (DeepSeek Harness)\n"
            "  BOXPORTER_EXECUTOR_COMMAND='[\"cmd\", ...]' (command runner)\n"
            "  BOXPORTER_RUNNER=mock BOXPORTER_ALLOW_MOCK=1 (demo/dry-run)"
        )
    return registry
