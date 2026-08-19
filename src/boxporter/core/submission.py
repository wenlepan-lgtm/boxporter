"""Submission Manifest V2 and artifact hashing (ADR-005).

The manifest freezes: task spec, code identity (git), report/evidence file
hashes and the executor run identity. ``submission_sha256`` is computed
over the canonical JSON of the manifest without the hash field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from boxporter.core.errors import BoxPorterError
from boxporter.core.schemas import canonical_json

SUBMISSION_SCHEMA = "BOXPORTER_SUBMISSION_V2"

REPORT_FILES = ("result.md", "verify.md", "executor.md")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SubmissionManifest:
    task_id: str
    attempt: int
    base_commit: str | None
    head_commit: str
    git_tree_sha: str
    git_diff_sha256: str | None
    task_sha256: str
    report_hashes: dict[str, str]
    artifact_manifest_sha256: str
    executor_run_id: str
    executor_session_ref: str
    executor_worktree: str
    created_at: str

    @property
    def submission_sha256(self) -> str:
        payload = self.to_dict()
        payload.pop("submission_sha256", None)
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SUBMISSION_SCHEMA,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "git_tree_sha": self.git_tree_sha,
            "git_diff_sha256": self.git_diff_sha256,
            "task_sha256": self.task_sha256,
            "result_sha256": self.report_hashes.get("result.md"),
            "verify_sha256": self.report_hashes.get("verify.md"),
            "executor_sha256": self.report_hashes.get("executor.md"),
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "executor_run_id": self.executor_run_id,
            "executor_session_ref": self.executor_session_ref,
            "executor_worktree": self.executor_worktree,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SubmissionManifest:
        try:
            report_hashes: dict[str, str] = {}
            for key in REPORT_FILES:
                stored = value.get(f"{key.removesuffix('.md')}_sha256")
                if stored is not None:
                    report_hashes[key] = str(stored)

            def optional_str(key: str) -> str | None:
                stored = value.get(key)
                return str(stored) if stored is not None else None

            return cls(
                task_id=str(value["task_id"]),
                attempt=int(str(value["attempt"])),
                base_commit=optional_str("base_commit"),
                head_commit=str(value["head_commit"]),
                git_tree_sha=str(value["git_tree_sha"]),
                git_diff_sha256=optional_str("git_diff_sha256"),
                task_sha256=str(value["task_sha256"]),
                report_hashes=report_hashes,
                artifact_manifest_sha256=str(value["artifact_manifest_sha256"]),
                executor_run_id=str(value["executor_run_id"]),
                executor_session_ref=str(value["executor_session_ref"]),
                executor_worktree=str(value["executor_worktree"]),
                created_at=str(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BoxPorterError(f"invalid submission manifest: {exc}") from exc

    def verify_reports(self, report_dir: Path) -> list[str]:
        """Re-hash report files; returns the list of mismatches (empty = ok)."""
        problems: list[str] = []
        for name, expected in self.report_hashes.items():
            path = report_dir / name
            if not path.is_file():
                problems.append(f"missing report file: {name}")
                continue
            if sha256_file(path) != expected:
                problems.append(f"hash mismatch: {name}")
        return problems


def build_artifact_manifest(paths: dict[str, Path]) -> str:
    """Hash a set of artifacts and return the canonical manifest JSON."""
    manifest: dict[str, object] = {
        "schema": "BOXPORTER_ARTIFACT_MANIFEST_V1",
        "files": {
            name: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
            for name, path in sorted(paths.items())
        },
    }
    return json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2)


def artifact_manifest_sha256(manifest_json: str) -> str:
    return hashlib.sha256(manifest_json.encode()).hexdigest()
