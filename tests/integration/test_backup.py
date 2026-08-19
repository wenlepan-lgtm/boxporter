"""Backup creation and restore-drill verification (ADR-014)."""

from __future__ import annotations

import json
from pathlib import Path

from boxporter.application.commands import CreateTask, ReadyTask
from boxporter.operations.backup import BackupService, verify_backup
from boxporter.storage.store import Store


def test_backup_round_trip(
    store: Store, make_spec: object, tmp_path: Path
) -> None:
    assert store.execute(CreateTask(spec=make_spec("bk-task"), actor_type="user")).ok
    assert store.execute(ReadyTask(task_id="bk-task", actor_type="user")).ok

    evidence_root = tmp_path / "artifacts"
    evidence_root.mkdir()
    (evidence_root / "note.txt").write_text("evidence", encoding="utf-8")

    backup_root = tmp_path / "backups"
    service = BackupService(Path(store.db.path), evidence_root=evidence_root)
    report = service.create(backup_root)
    backup_dir = Path(report.backup_dir)
    assert (backup_dir / "boxporter.sqlite").is_file()
    assert (backup_dir / "backup-manifest.json").is_file()
    assert (backup_dir / "evidence" / "note.txt").is_file()

    problems = verify_backup(backup_dir)
    assert problems == []

    # Tampering is detected by the drill.
    (backup_dir / "evidence" / "note.txt").write_text("tampered", encoding="utf-8")
    problems = verify_backup(backup_dir)
    assert any("hash mismatch" in item for item in problems)


def test_backup_contains_live_data(
    store: Store, make_spec: object, tmp_path: Path
) -> None:
    assert store.execute(CreateTask(spec=make_spec("bk-task-2"), actor_type="user")).ok
    backup_root = tmp_path / "backups"
    report = BackupService(Path(store.db.path)).create(backup_root)
    backup_dir = Path(report.backup_dir)
    manifest = json.loads((backup_dir / "backup-manifest.json").read_text())
    assert manifest["schema"] == "BOXPORTER_BACKUP_V1"
    assert "boxporter.sqlite" in manifest["files"]
