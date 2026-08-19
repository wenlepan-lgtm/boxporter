"""Full protocol simulation with frozen submissions and isolated reviews.

Covers: create → ready → executor worktree run → freeze submission →
independent reviewer → PASS (gate + seal) / REVISE / BLOCKED → retries →
cancel, plus tampering and isolation violations.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from boxporter.application.commands import (
    BeginNextAttempt,
    BlockTask,
    CancelTask,
    CreateTask,
    FailRun,
    FinalizeTaskDone,
    ReadyTask,
    ReviewTask,
    UnblockTask,
)
from boxporter.application.queries import project_boxes, task_detail
from boxporter.core.boxes import Box
from boxporter.core.errors import BoxPorterError
from boxporter.core.gitworktree import git
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import RunState, TaskState
from boxporter.storage.store import Store
from tests.phase3 import (
    init_repo,
    make_report_dir,
    make_review_dir,
    run_executor,
    run_reviewer,
    submit_run,
)


@pytest.fixture
def git_env(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    base = init_repo(repo)
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    return repo, worktrees_root, base


def create_task(
    store: Store, spec: TaskSpec, actor: str = "user", op: str | None = None
) -> None:
    result = store.execute(CreateTask(spec=spec, actor_type=actor), operation_id=op)
    assert result.ok, result.message


def ready(store: Store, task_id: str) -> None:
    result = store.execute(ReadyTask(task_id=task_id, actor_type="user"))
    assert result.ok, result.message


def succeed_reviewer(store: Store, run_id: str) -> None:
    run = store.runs.get(store.db.conn, run_id)
    assert run.state == RunState.RUNNING
    store.runs.update_state(store.db.conn, run_id, RunState.SUCCEEDED)


def drive_to_review_pending(
    store: Store,
    task_id: str,
    repo: Path,
    worktrees_root: Path,
    tmp_path: Path,
) -> str:
    executor_run, wt = run_executor(
        store, task_id, repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(tmp_path / task_id)
    submit_run(store, executor_run, wt, report_dir)
    return executor_run


def test_happy_path_pass_and_seal(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-1"))
    ready(store, "task-1")
    drive_to_review_pending(store, "task-1", repo, worktrees_root, tmp_path)

    reviewer_run, _reviewer_wt = run_reviewer(
        store, "task-1", repo=repo, worktrees_root=worktrees_root
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-1")
    result = store.execute(
        ReviewTask(
            task_id="task-1",
            reviewer_run_id=reviewer_run,
            result="PASS",
            review_dir=str(review_dir),
            actor_type="daemon",
        )
    )
    assert result.ok, result.message
    assert store.tasks.get(store.db.conn, "task-1").state == TaskState.PASS

    evidence_root = tmp_path / "evidence"
    sealed = store.execute(
        FinalizeTaskDone(
            task_id="task-1",
            evidence_root=str(evidence_root),
            actor_type="daemon",
        )
    )
    assert sealed.ok, sealed.message
    package = Path(str(sealed.data["package"]))
    assert (package / "manifest.json").is_file()
    assert (package / "submission-manifest.json").is_file()
    assert (package / "task.md").is_file()
    assert (package / "result.md").is_file()
    assert (package / "verify.md").is_file()

    from boxporter.application.verifier import verify_package

    assert verify_package(package) == []

    assert store.tasks.get(store.db.conn, "task-1").state == TaskState.DONE
    boxes = project_boxes(store, project)
    assert boxes[Box.PASSED][0].task_id == "task-1"
    assert boxes[Box.PENDING] == [] and boxes[Box.ACTIVE] == []
    assert boxes[Box.BLOCKED] == []


def test_review_without_submission_rejected(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    create_task(store, make_spec("task-1"))
    ready(store, "task-1")
    from boxporter.application.commands import StartExecutorRun

    result = store.execute(
        StartExecutorRun(
            task_id="task-1", runner="mock", identity="a", session_id="s",
            actor_type="daemon",
        )
    )
    run_id = str(result.data["run_id"])
    store.runs.update_state(store.db.conn, run_id, RunState.RUNNING)
    store.runs.update_state(store.db.conn, run_id, RunState.SUCCEEDED)
    # REVIEW_PENDING is only reachable through a frozen submission; without
    # one the task stays WORKING and review is refused.
    assert store.tasks.get(store.db.conn, "task-1").state == TaskState.WORKING
    with pytest.raises(BoxPorterError, match="cannot review from state"):
        store.execute(
            ReviewTask(
                task_id="task-1", reviewer_run_id=run_id, result="PASS",
                actor_type="daemon",
            )
        )


def test_revise_then_pass(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-2"))
    ready(store, "task-2")

    drive_to_review_pending(store, "task-2", repo, worktrees_root, tmp_path)
    reviewer_1, _ = run_reviewer(
        store, "task-2", repo=repo, worktrees_root=worktrees_root,
        session="rev-b1",
    )
    succeed_reviewer(store, reviewer_1)
    result = store.execute(
        ReviewTask(
            task_id="task-2",
            reviewer_run_id=reviewer_1,
            result="REVISE",
            required_changes=("add regression test",),
            actor_type="daemon",
        )
    )
    assert result.ok
    assert store.tasks.get(store.db.conn, "task-2").state == TaskState.REVISE

    assert store.execute(BeginNextAttempt(task_id="task-2", actor_type="daemon")).ok
    task = store.tasks.get(store.db.conn, "task-2")
    assert task.state == TaskState.READY
    assert task.current_attempt == 2

    drive_to_review_pending(store, "task-2", repo, worktrees_root, tmp_path / "a2")
    reviewer_2, _ = run_reviewer(
        store, "task-2", repo=repo, worktrees_root=worktrees_root,
        session="rev-b2",
    )
    succeed_reviewer(store, reviewer_2)
    review_dir = make_review_dir(tmp_path / "task-2-a2")
    assert store.execute(
        ReviewTask(
            task_id="task-2",
            reviewer_run_id=reviewer_2,
            result="PASS",
            review_dir=str(review_dir),
            actor_type="daemon",
        )
    ).ok
    assert store.execute(
        FinalizeTaskDone(
            task_id="task-2",
            evidence_root=str(tmp_path / "evidence"),
            actor_type="daemon",
        )
    ).ok
    detail = task_detail(store, "task-2")
    assert len(detail.attempts) == 2
    assert detail.attempts[0].state.value == "REVISED"
    assert detail.attempts[1].state.value == "PASSED"


def test_blocked_and_unblocked(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    create_task(store, make_spec("task-3"))
    ready(store, "task-3")
    from boxporter.application.commands import MarkRunRunning, StartExecutorRun

    result = store.execute(
        StartExecutorRun(
            task_id="task-3", runner="mock", identity="a", session_id="s",
            actor_type="daemon",
        )
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    blocked = store.execute(
        BlockTask(task_id="task-3", reason="device offline", actor_type="executor")
    )
    assert blocked.ok
    assert store.tasks.get(store.db.conn, "task-3").state == TaskState.BLOCKED
    assert store.runs.get(store.db.conn, run_id).state.value == "CANCELED"

    assert store.execute(UnblockTask(task_id="task-3", actor_type="user")).ok
    assert store.tasks.get(store.db.conn, "task-3").state == TaskState.READY


def test_crash_then_retry(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    create_task(store, make_spec("task-4"))
    ready(store, "task-4")
    from boxporter.application.commands import MarkRunRunning, StartExecutorRun

    result = store.execute(
        StartExecutorRun(
            task_id="task-4", runner="mock", identity="a", session_id="s",
            actor_type="daemon",
        )
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    failed = store.execute(
        FailRun(run_id=run_id, kind="crash", stop_reason="segfault", actor_type="daemon")
    )
    assert failed.ok
    assert store.tasks.get(store.db.conn, "task-4").state == TaskState.FAILED

    assert store.execute(BeginNextAttempt(task_id="task-4", actor_type="daemon")).ok
    assert store.tasks.get(store.db.conn, "task-4").state == TaskState.READY
    assert store.tasks.get(store.db.conn, "task-4").current_attempt == 2


def test_tampering_after_freeze_blocks_pass(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-5"))
    ready(store, "task-5")
    executor_run, wt = run_executor(
        store, "task-5", repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(tmp_path / "task-5")
    submit_run(store, executor_run, wt, report_dir)

    # The executor mutates the worktree after the freeze.
    (wt / "app.py").write_text("value = 999\n", encoding="utf-8")
    git("add", "app.py", cwd=wt)
    git("commit", "-q", "-m", "sneaky change", cwd=wt)

    reviewer_run, _ = run_reviewer(
        store, "task-5", repo=repo, worktrees_root=worktrees_root
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-5")
    with pytest.raises(BoxPorterError, match="acceptance gate failed"):
        store.execute(
            ReviewTask(
                task_id="task-5",
                reviewer_run_id=reviewer_run,
                result="PASS",
                review_dir=str(review_dir),
                actor_type="daemon",
            )
        )
    assert store.tasks.get(store.db.conn, "task-5").state == TaskState.REVIEW_PENDING


def test_evidence_tampering_blocks_pass(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-6"))
    ready(store, "task-6")
    executor_run, wt = run_executor(
        store, "task-6", repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(tmp_path / "task-6")
    submit_run(store, executor_run, wt, report_dir)

    (report_dir / "result.md").write_text("# Result\n\ntampered\n", encoding="utf-8")

    reviewer_run, _ = run_reviewer(
        store, "task-6", repo=repo, worktrees_root=worktrees_root
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-6")
    with pytest.raises(BoxPorterError, match="acceptance gate failed"):
        store.execute(
            ReviewTask(
                task_id="task-6",
                reviewer_run_id=reviewer_run,
                result="PASS",
                review_dir=str(review_dir),
                actor_type="daemon",
            )
        )


def test_reviewer_same_worktree_rejected(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-7"))
    ready(store, "task-7")
    executor_run, wt = run_executor(
        store, "task-7", repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(tmp_path / "task-7")
    submit_run(store, executor_run, wt, report_dir)

    reviewer_run, _ = run_reviewer(
        store, "task-7", repo=repo, worktrees_root=worktrees_root, worktree=wt
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-7")
    with pytest.raises(BoxPorterError, match="acceptance gate failed"):
        store.execute(
            ReviewTask(
                task_id="task-7",
                reviewer_run_id=reviewer_run,
                result="PASS",
                review_dir=str(review_dir),
                actor_type="daemon",
            )
        )


def test_high_risk_requires_risk_statement(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-8", risk_level="high"))
    ready(store, "task-8")
    executor_run, wt = run_executor(
        store, "task-8", repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(tmp_path / "task-8")
    submit_run(store, executor_run, wt, report_dir)

    reviewer_run, _ = run_reviewer(
        store, "task-8", repo=repo, worktrees_root=worktrees_root
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-8")
    # No test evidence + no production risk statement -> gate fails.
    review_dir.joinpath("review_evidence.json").write_text(
        json.dumps({}), encoding="utf-8"
    )
    with pytest.raises(BoxPorterError, match="acceptance gate failed"):
        store.execute(
            ReviewTask(
                task_id="task-8",
                reviewer_run_id=reviewer_run,
                result="PASS",
                review_dir=str(review_dir),
                actor_type="daemon",
            )
        )


def test_secret_in_evidence_blocks_seal(
    store: Store,
    project: str,
    make_spec: Callable[..., TaskSpec],
    git_env: tuple[Path, Path, str],
    tmp_path: Path,
) -> None:
    repo, worktrees_root, _ = git_env
    create_task(store, make_spec("task-9"))
    ready(store, "task-9")
    executor_run, wt = run_executor(
        store, "task-9", repo=repo, worktrees_root=worktrees_root
    )
    report_dir = make_report_dir(
        tmp_path / "task-9",
        result="api_key = sk-abcdefghijklmnopqrstuvwxyz123456 leaked",
    )
    submit_run(store, executor_run, wt, report_dir)

    reviewer_run, _ = run_reviewer(
        store, "task-9", repo=repo, worktrees_root=worktrees_root
    )
    succeed_reviewer(store, reviewer_run)
    review_dir = make_review_dir(tmp_path / "task-9")
    assert store.execute(
        ReviewTask(
            task_id="task-9",
            reviewer_run_id=reviewer_run,
            result="PASS",
            review_dir=str(review_dir),
            actor_type="daemon",
        )
    ).ok

    with pytest.raises(BoxPorterError, match="secret scan blocked sealing"):
        store.execute(
            FinalizeTaskDone(
                task_id="task-9",
                evidence_root=str(tmp_path / "evidence"),
                actor_type="daemon",
            )
        )
    assert store.tasks.get(store.db.conn, "task-9").state == TaskState.PASS


def test_cancel_task(
    store: Store, project: str, make_spec: Callable[..., TaskSpec]
) -> None:
    create_task(store, make_spec("task-10"))
    ready(store, "task-10")
    from boxporter.application.commands import MarkRunRunning, StartExecutorRun

    result = store.execute(
        StartExecutorRun(
            task_id="task-10", runner="mock", identity="a", session_id="s",
            actor_type="daemon",
        )
    )
    run_id = str(result.data["run_id"])
    store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon"))
    assert store.execute(CancelTask(task_id="task-10", actor_type="user")).ok
    assert store.tasks.get(store.db.conn, "task-10").state == TaskState.CANCELED
    assert store.runs.get(store.db.conn, run_id).state.value == "CANCELED"
    assert project_boxes(store, project)[Box.ARCHIVED][0].task_id == "task-10"
