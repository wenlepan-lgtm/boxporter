"""Contract tests for the DeepSeek Harness adapter (ADR-010).

No harness install required: the adapter is a templated command runner;
these tests pin the substitution contract so upstream CLI flag changes
only touch the profile.
"""

from __future__ import annotations

from boxporter.core.schemas import TaskSpec
from boxporter.runners.base import RunSpec
from boxporter.runners.deepseek_harness import (
    DEFAULT_COMMAND,
    PINNED_DSH_COMMIT,
    DeepSeekHarnessRunner,
)


def make_spec() -> RunSpec:
    task = TaskSpec(
        task_id="dsh-task",
        project_id="demo",
        title="DSH",
        objective="Do the thing.",
        priority=50,
        risk_level="low",
        workspace="/tmp/dsh-ws",
        acceptance_criteria=("works",),
    )
    return RunSpec(
        run_id="run-1",
        task_id="dsh-task",
        attempt=1,
        role="executor",
        workspace="/tmp/dsh-ws",
        task=task,
        session_id="session-1",
        runner_profile="boxporter-executor",
    )


def test_capabilities_pinned() -> None:
    runner = DeepSeekHarnessRunner()
    capabilities = runner.capabilities()
    assert capabilities.name == "deepseek-harness"
    assert capabilities.requires_model is True
    assert capabilities.version == f"commit-{PINNED_DSH_COMMIT[:12]}"


def test_command_template_substitution() -> None:
    spec = make_spec()
    argv = [
        DeepSeekHarnessRunner._substitute(item, spec) for item in DEFAULT_COMMAND
    ]
    assert "dsh-task" in argv
    assert "/tmp/dsh-ws" in argv
    assert "run-1" in argv
    assert "executor" in argv
    assert "{task}" not in argv and "{workspace}" not in argv


def test_custom_command_profile() -> None:
    runner = DeepSeekHarnessRunner(
        command=["dsh", "--mode", "experimental", "--task", "{task}"]
    )
    assert runner.command == ["dsh", "--mode", "experimental", "--task", "{task}"]


def test_checkpoint_resume_unsupported() -> None:
    import pytest

    from boxporter.core.errors import BoxPorterError
    from boxporter.runners.base import CheckpointRef, RunHandle

    runner = DeepSeekHarnessRunner()
    handle = RunHandle(run_id="run-1", runtime_id="fake")
    with pytest.raises(BoxPorterError, match="checkpoint"):
        runner.checkpoint(handle)
    with pytest.raises(BoxPorterError, match="resume"):
        runner.resume(CheckpointRef(ref="x", location="y"), make_spec())
