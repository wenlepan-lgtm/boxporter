"""Git worktree lifecycle and content identity (ADR-005).

Content identity for reviews is computed from git itself:
``head_commit``, ``git_tree_sha`` (HEAD^{tree}) and a binary-safe diff
hash against the task's base commit. Anything that changes any of these
invalidates the frozen submission.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from boxporter.core.errors import BoxPorterError


@dataclass(frozen=True)
class WorktreeIdentity:
    worktree: str
    head_commit: str
    tree_sha: str
    base_commit: str | None
    diff_sha256: str | None
    changed_files: tuple[str, ...]


def git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise BoxPorterError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


class WorktreeManager:
    def __init__(self, worktrees_root: Path):
        self.worktrees_root = worktrees_root

    def add_worktree(
        self, repo: Path, name: str, base_commit: str | None = None
    ) -> Path:
        target = self.worktrees_root / name
        if target.exists():
            raise BoxPorterError(f"worktree already exists: {target}")
        args = ["worktree", "add", "--detach", str(target)]
        if base_commit is not None:
            args.append(base_commit)
        git(*args, cwd=repo)
        return target

    def remove_worktree(self, worktree: Path, repo: Path) -> None:
        git("worktree", "remove", "--force", str(worktree), cwd=repo, check=False)
        git("worktree", "prune", cwd=repo, check=False)

    def identity(
        self, worktree: Path, base_commit: str | None = None
    ) -> WorktreeIdentity:
        head = git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        tree = git("rev-parse", "HEAD^{tree}", cwd=worktree).stdout.strip()
        diff_sha: str | None = None
        changed: list[str] = []
        if base_commit is not None:
            diff = git(
                "diff", "--binary", base_commit, head, cwd=worktree
            ).stdout.encode()
            diff_sha = hashlib.sha256(diff).hexdigest()
            changed_raw = git(
                "diff", "--name-status", base_commit, head, cwd=worktree
            ).stdout.splitlines()
            changed = [line for line in changed_raw if line.strip()]
        return WorktreeIdentity(
            worktree=str(worktree),
            head_commit=head,
            tree_sha=tree,
            base_commit=base_commit,
            diff_sha256=diff_sha,
            changed_files=tuple(changed),
        )

    def is_clean(self, worktree: Path) -> bool:
        status = git("status", "--porcelain", cwd=worktree).stdout.strip()
        return status == ""

    def tree_unchanged(self, worktree: Path, tree_sha: str) -> bool:
        current = git("rev-parse", "HEAD^{tree}", cwd=worktree).stdout.strip()
        return current == tree_sha
