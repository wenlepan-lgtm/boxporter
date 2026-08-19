"""Online backup and offline verification (ADR-014).

The SQLite snapshot uses the backup API (consistent even under WAL).
PASSED evidence packages are copied with a fresh hash manifest so restore
drills can verify them without the database.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from boxporter.core.clock import now_iso
from boxporter.core.ids import new_id
from boxporter.core.submission import sha256_file


@dataclass(frozen=True)
class BackupReport:
    backup_dir: str
    files: tuple[str, ...]


class BackupService:
    def __init__(self, store_path: Path, evidence_root: Path | None = None):
        self.store_path = store_path
        self.evidence_root = evidence_root

    def create(self, backup_root: Path) -> BackupReport:
        stamp = now_iso().replace(":", "").replace("-", "")
        backup_dir = backup_root / f"backup-{stamp}-{new_id('bk')[-8:]}"
        staging = backup_root / f".{backup_dir.name}.tmp"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            self._snapshot_db(staging / "boxporter.sqlite")
            evidence_dest = staging / "evidence"
            if self.evidence_root is not None and Path(self.evidence_root).is_dir():
                shutil.copytree(self.evidence_root, evidence_dest)
            self._write_manifest(staging)
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(backup_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        files = tuple(
            str(path.relative_to(backup_dir))
            for path in sorted(backup_dir.rglob("*"))
            if path.is_file()
        )
        return BackupReport(backup_dir=str(backup_dir), files=files)

    def _snapshot_db(self, target: Path) -> None:
        source = sqlite3.connect(self.store_path)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    def _write_manifest(self, staging: Path) -> None:
        files: dict[str, str] = {}
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(staging))] = sha256_file(path)
        (staging / "backup-manifest.json").write_text(
            json.dumps(
                {
                    "schema": "BOXPORTER_BACKUP_V1",
                    "created_at": now_iso(),
                    "files": files,
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def verify_backup(backup_dir: Path) -> list[str]:
    """Restore-drill verification: DB opens with current migrations,
    manifest hashes recompute, evidence packages pass offline checks."""
    problems: list[str] = []
    manifest_path = backup_dir / "backup-manifest.json"
    if not manifest_path.is_file():
        return [f"missing backup-manifest.json: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"unreadable backup manifest: {exc}"]
    files = manifest.get("files")
    if not isinstance(files, dict):
        problems.append("manifest files section missing")
        files = {}
    for name, expected in files.items():
        path = backup_dir / name
        if not path.is_file():
            problems.append(f"missing file: {name}")
            continue
        if sha256_file(path) != expected:
            problems.append(f"hash mismatch: {name}")

    db_path = backup_dir / "boxporter.sqlite"
    if not db_path.is_file():
        problems.append("missing boxporter.sqlite")
    else:
        try:
            conn = sqlite3.connect(db_path)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
                ).fetchone()
                if version < 5:
                    problems.append(f"stale schema version in backup: {version}")
                if conn.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0] != "ok":
                    problems.append("sqlite integrity check failed")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            problems.append(f"database unreadable: {exc}")

    evidence_root = backup_dir / "evidence" / "passed"
    if evidence_root.is_dir():
        for manifest_file in evidence_root.rglob("manifest.json"):
            from boxporter.application.verifier import verify_package

            package_problems = verify_package(manifest_file.parent)
            if package_problems:
                problems.append(
                    f"evidence package failed: {manifest_file.parent.name}:"
                    f" {package_problems[0]}"
                )
    return problems
