"""Acceptance gate: the ordered checks that must all pass before PASS
(plan §13.2). Any single failure blocks the task from PASSED."""

from __future__ import annotations

from pathlib import Path

from boxporter.core.schemas import Submission, Task
from boxporter.core.submission import sha256_file
from boxporter.storage.store import Store

# Required evidence kinds that demand a machine-checkable test exit code.
TEST_EVIDENCE_KINDS = frozenset({"test_commands_with_exit_codes"})


def check_acceptance(
    store: Store,
    task: Task,
    submission: Submission,
    *,
    executor_run_id: str,
    executor_session: str,
    executor_worktree: str | None,
    reviewer_run_id: str,
    reviewer_session: str,
    reviewer_worktree: str | None,
    reviewer_evidence: dict[str, object] | None,
) -> list[str]:
    """Returns the list of gate problems; empty list = gate passed."""
    problems: list[str] = []
    conn = store.db.conn

    # 1. Schema validity is guaranteed by construction (typed dataclasses).
    # 2. Executor / Reviewer isolation (ADR-006).
    if executor_run_id == reviewer_run_id:
        problems.append("executor and reviewer run ids must differ")
    if executor_session == reviewer_session:
        problems.append("executor and reviewer sessions must differ")
    if executor_worktree is None or reviewer_worktree is None:
        problems.append("both executor and reviewer worktrees must be recorded")
    elif Path(executor_worktree).resolve() == Path(reviewer_worktree).resolve():
        problems.append("executor and reviewer worktrees must differ")

    # 3. Frozen manifest hashes recomputable from stored artifacts.
    artifacts = store.artifacts.for_submission(conn, submission.id)
    by_kind = {artifact.kind: artifact for artifact in artifacts}
    for name in ("result.md", "verify.md", "executor.md"):
        artifact = by_kind.get(name)
        if artifact is None:
            problems.append(f"missing artifact: {name}")
            continue
        path = Path(artifact.uri)
        if not path.is_file():
            problems.append(f"artifact file missing: {artifact.uri}")
            continue
        if sha256_file(path) != artifact.sha256:
            problems.append(f"artifact hash mismatch: {name}")

    # 4. Git identity unchanged since freezing (ADR-005).
    if executor_worktree is not None:
        worktree = Path(executor_worktree)
        from boxporter.core.gitworktree import WorktreeManager

        identity = WorktreeManager(worktree.parent.parent / "worktrees").identity(worktree)
        if identity.head_commit != submission.head_commit:
            problems.append("git head changed after freeze")
        if identity.tree_sha != submission.git_tree_sha:
            problems.append("git tree changed after freeze")

    # 5. Required artifacts present (checked above).

    # 6. Machine-checkable test evidence when the task requires it.
    requires_tests = TEST_EVIDENCE_KINDS & set(task.spec.required_evidence)
    if requires_tests:
        if reviewer_evidence is None:
            problems.append("reviewer evidence missing (tests required)")
        else:
            raw_exit_code = reviewer_evidence.get("test_exit_code", -1)
            exit_code = (
                int(raw_exit_code) if isinstance(raw_exit_code, (int, str)) else -1
            )
            if exit_code != 0:
                problems.append("reviewer test exit code is not 0")

    # 7. Reviewer verdict PASS is enforced by the review command itself.

    # 8. High-risk tasks require an explicit production-risk statement.
    if task.risk_level == "high" and (
        reviewer_evidence is None or not reviewer_evidence.get("production_risk")
    ):
        problems.append("high-risk task requires production risk statement")

    return problems
