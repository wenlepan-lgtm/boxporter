"""Offline verification of sealed PASSED evidence packages.

Works without the BoxPorter database: every file hash in manifest.json is
recomputed, and the submission hash is rebuilt from the stored manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from boxporter.core.schemas import canonical_json
from boxporter.core.submission import sha256_file

ARCHIVE_SCHEMA = "BOXPORTER_ARCHIVE_V2"


def verify_package(package_dir: Path) -> list[str]:
    problems: list[str] = []
    package = Path(package_dir)
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing manifest.json: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"unreadable manifest.json: {exc}"]
    if manifest.get("schema") != ARCHIVE_SCHEMA:
        problems.append(f"unsupported archive schema: {manifest.get('schema')}")

    files = manifest.get("files")
    if not isinstance(files, dict):
        return problems + ["manifest files section missing"]
    for name, expected in files.items():
        path = package / name
        if not path.is_file():
            problems.append(f"missing file: {name}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            problems.append(f"hash mismatch: {name}")

    submission_path = package / "submission-manifest.json"
    if submission_path.is_file():
        try:
            submission = json.loads(submission_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"unreadable submission-manifest.json: {exc}")
        else:
            payload = dict(submission)
            stored_sha = payload.pop("submission_sha256", None)
            if not isinstance(stored_sha, str):
                problems.append("submission-manifest.json has no submission_sha256")
            else:
                rebuilt = hashlib.sha256(
                    canonical_json(payload).encode()
                ).hexdigest()
                if rebuilt != stored_sha:
                    problems.append("submission_sha256 does not match manifest content")
                if manifest.get("submission_sha256") != stored_sha:
                    problems.append("archive submission_sha256 differs from manifest")
    else:
        problems.append("missing submission-manifest.json")

    commit_path = package / "commit.json"
    if commit_path.is_file():
        try:
            commit = json.loads(commit_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"unreadable commit.json: {exc}")
        else:
            if not commit.get("git_tree_sha") or not commit.get("head_commit"):
                problems.append("commit.json missing git identity fields")
    else:
        problems.append("missing commit.json")

    return problems
