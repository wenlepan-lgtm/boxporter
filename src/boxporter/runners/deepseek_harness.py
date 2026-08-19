"""DeepSeek Harness runner (experimental, plan §17.1 priority 3).

DeepSeek Harness is still a Developer Preview upstream; the version is
pinned to a fixed commit (docs/operations/runner-versions.md, ADR-010).
The adapter is a templated CommandRunner: the CLI flags stay configurable
so upstream flag changes only touch the profile, never Core.
"""

from __future__ import annotations

from boxporter.core.errors import BoxPorterError
from boxporter.runners.base import RunnerCapabilities
from boxporter.runners.command import CommandRunner

PINNED_DSH_COMMIT = "47f943859bef60e4160492346772ded9b24f765a"

DEFAULT_COMMAND = [
    "deepseek-harness",
    "run",
    "--task", "{task}",
    "--workspace", "{workspace}",
    "--run-id", "{run_id}",
    "--role", "{role}",
]


class DeepSeekHarnessRunner(CommandRunner):
    """Experimental adapter for the DeepSeek Harness CLI."""

    def __init__(
        self,
        command: list[str] | None = None,
        version: str = f"commit-{PINNED_DSH_COMMIT[:12]}",
    ):
        argv = command or DEFAULT_COMMAND
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise BoxPorterError("deepseek-harness runner requires a non-empty command")
        super().__init__(argv, name="deepseek-harness", version=version)

    def capabilities(self) -> RunnerCapabilities:
        base = super().capabilities()
        return RunnerCapabilities(
            name=base.name,
            version=base.version,
            requires_model=True,
        )


class DeepSeekHarnessUnavailable(BoxPorterError):
    """Raised when the harness CLI is not installed on this host."""
