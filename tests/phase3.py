"""Shared helpers for Phase 3+ integration tests (git + submissions)."""

from __future__ import annotations

import itertools
from pathlib import Path

from boxporter.application.commands import (
    MarkRunRunning,
    StartExecutorRun,
    StartReviewerRun,
    SubmitExecutorRun,
)
from boxporter.core.gitworktree import git
from boxporter.storage.store import Store

_worktree_counter = itertools.count(1)


def init_repo(repo: Path) -> str:
    """Create a git repo with one committed file; return the base commit."""
    repo.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=repo)
    git("config", "user.email", "porter@example.com", cwd=repo)
    git("config", "user.name", "Porter", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "app.py", cwd=repo)
    git("commit", "-q", "-m", "baseline", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def make_report_dir(parent: Path, *, result: str = "implemented", verify: str = "tests pass") -> Path:
    report_dir = parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "result.md").write_text(f"# Result\n\n{result}\n", encoding="utf-8")
    (report_dir / "verify.md").write_text(f"# Verify\n\n{verify}\n", encoding="utf-8")
    (report_dir / "executor.md").write_text("# Executor report\n\nDone.\n", encoding="utf-8")
    return report_dir


def make_review_dir(parent: Path, *, exit_code: int = 0, production_risk: str = "low") -> Path:
    review_dir = parent / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "review.md").write_text("# Review\n\nPASS\n", encoding="utf-8")
    import json

    (review_dir / "review_evidence.json").write_text(
        json.dumps({"test_exit_code": exit_code, "production_risk": production_risk}),
        encoding="utf-8",
    )
    return review_dir


def run_executor(
    store: Store,
    task_id: str,
    *,
    repo: Path,
    worktrees_root: Path,
    name: str = "executor",
    identity: str = "executor-model-a",
    session: str = "session-exec-a",
    worktree: Path | None = None,
) -> tuple[str, Path]:
    """Start an executor run with its own worktree and return (run_id, worktree)."""
    from boxporter.core.gitworktree import WorktreeManager

    manager = WorktreeManager(worktrees_root)
    wt = worktree or manager.add_worktree(repo, f"{name}-{next(_worktree_counter)}")
    result = store.execute(
        StartExecutorRun(
            task_id=task_id,
            runner="mock",
            identity=identity,
            session_id=session,
            worktree=str(wt),
            actor_type="daemon",
        )
    )
    assert result.ok, result.message
    run_id = str(result.data["run_id"])
    assert store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon")).ok
    return run_id, wt


def run_reviewer(
    store: Store,
    task_id: str,
    *,
    repo: Path,
    worktrees_root: Path,
    name: str = "reviewer",
    identity: str = "reviewer-model-b",
    session: str = "session-rev-b",
    worktree: Path | None = None,
) -> tuple[str, Path]:
    from boxporter.core.gitworktree import WorktreeManager

    manager = WorktreeManager(worktrees_root)
    wt = worktree or manager.add_worktree(repo, f"{name}-{next(_worktree_counter)}")
    result = store.execute(
        StartReviewerRun(
            task_id=task_id,
            runner="mock",
            identity=identity,
            session_id=session,
            worktree=str(wt),
            actor_type="daemon",
        )
    )
    assert result.ok, result.message
    run_id = str(result.data["run_id"])
    assert store.execute(MarkRunRunning(run_id=run_id, actor_type="daemon")).ok
    return run_id, wt


def submit_run(
    store: Store, run_id: str, worktree: Path, report_dir: Path
) -> dict[str, object]:
    result = store.execute(
        SubmitExecutorRun(
            run_id=run_id,
            report_dir=str(report_dir),
            worktree=str(worktree),
            actor_type="daemon",
        )
    )
    assert result.ok, result.message
    return result.data
