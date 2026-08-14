from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

TASK_SCHEMA = "BOXPORTER_TASK_V1"
REPORT_SCHEMA = "BOXPORTER_AGENT_REPORT_V1"
VALID_STATES = {
    "PENDING",
    "READY",
    "WORKING",
    "REVIEW_PENDING",
    "REVISE",
    "PASS",
    "BLOCKED",
    "WAITING_USER",
}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BoxPorterError(RuntimeError):
    pass


def now_iso() -> str:
    return _local_now().isoformat(timespec="seconds")


def _local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _safe_scalar(value: Any) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise BoxPorterError("metadata values must be single-line scalars")
    return text


def render_document(metadata: dict[str, Any], body: str) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_safe_scalar(value)}" for key, value in metadata.items())
    lines.extend(["---", "", body.rstrip(), ""])
    return "\n".join(lines)


def parse_document(path: Path) -> tuple[dict[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BoxPorterError(f"missing document: {path}") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise BoxPorterError(f"invalid front matter: {path}")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise BoxPorterError(f"unclosed front matter: {path}") from exc
    metadata: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip():
            continue
        if ":" not in line:
            raise BoxPorterError(f"invalid metadata at {path}:{number}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key in metadata:
            raise BoxPorterError(f"invalid or duplicate key at {path}:{number}")
        metadata[key] = value.strip()
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return metadata, body


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def submission_sha(result_path: Path, verify_path: Path) -> str:
    if not result_path.is_file() or not verify_path.is_file():
        raise BoxPorterError("result.md and verify.md are both required")
    result_digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    verify_digest = hashlib.sha256(verify_path.read_bytes()).hexdigest()
    canonical = f"{result_digest}\n{verify_digest}\n".encode()
    return hashlib.sha256(canonical).hexdigest()


def normalize_required_changes(value: str | Iterable[str]) -> list[str]:
    raw_items = value.split(",") if isinstance(value, str) else list(value)
    changes: list[str] = []
    for raw in raw_items:
        item = str(raw).strip()
        if not item or item.lower() == "none":
            continue
        if "\n" in item or "\r" in item:
            raise BoxPorterError("required changes must be single-line values")
        if item not in changes:
            changes.append(item)
    return changes


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def boxes(self) -> Path:
        return self.root / "boxes"

    @property
    def pending(self) -> Path:
        return self.boxes / "pending"

    @property
    def active(self) -> Path:
        return self.boxes / "active" / "current.md"

    @property
    def passed(self) -> Path:
        return self.boxes / "passed"

    @property
    def blocked(self) -> Path:
        return self.boxes / "blocked"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def result(self) -> Path:
        return self.reports / "result.md"

    @property
    def verify(self) -> Path:
        return self.reports / "verify.md"

    @property
    def executor_report(self) -> Path:
        return self.reports / "executor.md"

    @property
    def reviewer_report(self) -> Path:
        return self.reports / "reviewer.md"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def log(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.root / ".coordinator.lock"


class DirectoryLock:
    def __init__(self, path: Path):
        self.path = path
        self.owned = False

    def __enter__(self) -> DirectoryLock:  # noqa: PYI034 - Python 3.10 has no typing.Self
        try:
            self.path.mkdir()
        except FileExistsError:
            pid_path = self.path / "pid"
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                shutil.rmtree(self.path, ignore_errors=True)
                self.path.mkdir()
            except PermissionError as exc:
                raise BoxPorterError(f"coordinator lock owned by pid {pid}") from exc
            else:
                raise BoxPorterError(f"coordinator already running as pid {pid}")
        atomic_write(self.path / "pid", f"{os.getpid()}\n")
        self.owned = True
        return self

    def __exit__(self, *_: object) -> None:
        if self.owned:
            shutil.rmtree(self.path, ignore_errors=True)


class BoxPorter:
    def __init__(self, root: str | Path = ".boxporter", workspace: str | Path | None = None):
        self.layout = Layout(Path(root).expanduser().resolve())
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve()

    def init(self, force: bool = False) -> None:
        if self.layout.root.exists() and any(self.layout.root.iterdir()) and not force:
            raise BoxPorterError(f"non-empty root already exists: {self.layout.root}")
        for directory in (
            self.layout.pending,
            self.layout.active.parent,
            self.layout.passed,
            self.layout.blocked,
            self.layout.reports,
            self.layout.root / "logs",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        config = {
            "poll_seconds": 1200,
            "stale_seconds": 2400,
            "retry_seconds": 3600,
            "max_revisions": 2,
            "max_progress_extensions": 2,
            "executor_command": [],
            "reviewer_command": [],
        }
        if not self.layout.config.exists():
            atomic_json(self.layout.config, config)
        if not self.layout.state.exists():
            atomic_json(self.layout.state, self._default_state())
        self._event("initialized", root=str(self.layout.root))

    def add(
        self,
        task_id: str,
        title: str,
        body: str,
        author: str = "human",
        depends_on: Iterable[str] = (),
    ) -> Path:
        self._require_initialized()
        self._validate_task_id(task_id)
        if self._find_task(task_id):
            raise BoxPorterError(f"task already exists: {task_id}")
        created = now_iso()
        metadata = {
            "schema": TASK_SCHEMA,
            "task_id": task_id,
            "title": title,
            "author": author,
            "state": "PENDING",
            "handoff_to": "executor",
            "created_at": created,
            "updated_at": created,
            "attempt": 0,
        }
        dependencies = list(dict.fromkeys(str(item).strip() for item in depends_on if str(item).strip()))
        for dependency in dependencies:
            self._validate_task_id(dependency)
        if dependencies:
            metadata["depends_on"] = ",".join(dependencies)
        path = self.layout.pending / f"{task_id}.md"
        atomic_write(path, render_document(metadata, body))
        self._event("task_added", task_id=task_id)
        return path

    def promote(self) -> Path | None:
        self._require_initialized()
        if self.layout.active.exists():
            raise BoxPorterError("an active task already exists")
        candidates = sorted(
            self.layout.pending.glob("*.md"), key=lambda path: (path.stat().st_mtime_ns, path.name)
        )
        passed_ids = self._passed_task_ids()
        eligible: list[Path] = []
        for candidate in candidates:
            metadata, _ = parse_document(candidate)
            self._validate_task(metadata)
            dependencies = {
                item.strip() for item in metadata.get("depends_on", "").split(",") if item.strip()
            }
            if dependencies.issubset(passed_ids):
                eligible.append(candidate)
        if not eligible:
            return None
        source = eligible[0]
        metadata, body = parse_document(source)
        self._validate_task(metadata)
        metadata.update(state="READY", handoff_to="executor", updated_at=now_iso())
        atomic_write(source, render_document(metadata, body))
        os.replace(source, self.layout.active)
        _fsync_directory(source.parent)
        _fsync_directory(self.layout.active.parent)
        self._clear_reports()
        self._event("task_promoted", task_id=metadata["task_id"])
        return self.layout.active

    def transition(self, state: str, handoff_to: str | None = None) -> dict[str, str]:
        state = state.upper()
        if state not in VALID_STATES:
            raise BoxPorterError(f"invalid state: {state}")
        metadata, body = self.active_task()
        metadata["state"] = state
        if handoff_to is not None:
            metadata["handoff_to"] = handoff_to
        metadata["updated_at"] = now_iso()
        if state in {"READY", "REVISE"}:
            metadata["attempt"] = str(int(metadata.get("attempt", "0")) + 1)
        atomic_write(self.layout.active, render_document(metadata, body))
        self._event("task_transition", task_id=metadata["task_id"], state=state)
        return metadata

    def active_task(self) -> tuple[dict[str, str], str]:
        metadata, body = parse_document(self.layout.active)
        self._validate_task(metadata)
        return metadata, body

    def submit(self, author: str, content: str = "Implementation and verification submitted.") -> str:
        metadata, _ = self.active_task()
        if metadata["state"] not in {"WORKING", "READY", "REVISE"}:
            raise BoxPorterError(f"cannot submit from state {metadata['state']}")
        digest = submission_sha(self.layout.result, self.layout.verify)
        report = {
            "schema": REPORT_SCHEMA,
            "report_id": f"EXECUTOR-{_local_now().strftime('%Y%m%dT%H%M%S')}-{metadata['task_id']}",
            "time": now_iso(),
            "title": f"Executor submission for {metadata['task_id']}",
            "author": author,
            "role": "executor",
            "result": "REVIEW_PENDING",
            "task": metadata["task_id"],
            "submission_sha256": digest,
        }
        atomic_write(self.layout.executor_report, render_document(report, content))
        metadata.update(
            state="REVIEW_PENDING",
            handoff_to="reviewer",
            updated_at=now_iso(),
            submission_sha256=digest,
        )
        _, body = self.active_task()
        atomic_write(self.layout.active, render_document(metadata, body))
        self._event("submission_created", task_id=metadata["task_id"], submission=digest)
        return digest

    def review(
        self,
        result: str,
        author: str,
        content: str,
        required_changes: str | Iterable[str] = (),
    ) -> Path | None:
        result = result.upper()
        if result not in {"PASS", "REVISE", "INVALID"}:
            raise BoxPorterError("review result must be PASS, REVISE, or INVALID")
        changes = normalize_required_changes(required_changes)
        if result != "PASS" and not changes:
            raise BoxPorterError("REVISE and INVALID reviews require concrete required changes")
        metadata, body = self.active_task()
        if metadata["state"] != "REVIEW_PENDING":
            raise BoxPorterError(f"cannot review from state {metadata['state']}")
        digest = submission_sha(self.layout.result, self.layout.verify)
        if digest != metadata.get("submission_sha256"):
            raise BoxPorterError("submission changed after review request")
        executor_meta, _ = parse_document(self.layout.executor_report)
        self._validate_report(executor_meta, "executor", metadata["task_id"], digest)
        report = {
            "schema": REPORT_SCHEMA,
            "report_id": f"REVIEWER-{_local_now().strftime('%Y%m%dT%H%M%S')}-{metadata['task_id']}",
            "time": now_iso(),
            "title": f"Independent review for {metadata['task_id']}",
            "author": author,
            "role": "reviewer",
            "result": result,
            "task": metadata["task_id"],
            "submission_sha256": digest,
            "required_changes": json.dumps(changes, ensure_ascii=False, separators=(",", ":")),
        }
        policy = None if result == "PASS" else self._record_revision(metadata["task_id"], digest, changes)
        atomic_write(self.layout.reviewer_report, render_document(report, content))
        self._event("review_recorded", task_id=metadata["task_id"], result=result)
        if result == "PASS":
            metadata.update(state="PASS", handoff_to="none", updated_at=now_iso())
            atomic_write(self.layout.active, render_document(metadata, body))
            return self.archive_passed()
        if policy is None:  # pragma: no cover - guarded by the PASS branch above
            raise BoxPorterError("missing review policy")
        metadata.update(
            state="REVISE" if policy["continue"] else "WAITING_USER",
            handoff_to="executor" if policy["continue"] else "human",
            updated_at=now_iso(),
            review_policy=policy["reason"],
        )
        atomic_write(self.layout.active, render_document(metadata, body))
        return None

    def archive_passed(self) -> Path:
        metadata, body = self.active_task()
        if metadata["state"] != "PASS":
            raise BoxPorterError("only PASS tasks can be archived")
        existing = self._existing_archive(
            metadata["task_id"], metadata.get("submission_sha256", "")
        )
        if existing is not None:
            self.layout.active.unlink(missing_ok=True)
            _fsync_directory(self.layout.active.parent)
            self._event("task_archive_recovered", task_id=metadata["task_id"], archive=str(existing))
            return existing
        timestamp = _local_now().strftime("%Y%m%dT%H%M%S")
        destination = self.layout.passed / f"{timestamp}-{metadata['task_id']}"
        staging = self.layout.passed / f".{timestamp}-{metadata['task_id']}.{os.getpid()}.tmp"
        if destination.exists() or staging.exists():
            raise BoxPorterError(f"archive already exists: {destination}")
        atomic_write(self.layout.active, render_document(metadata, body))
        staging.mkdir()
        try:
            evidence = {
                "task.md": self.layout.active,
                "result.md": self.layout.result,
                "verify.md": self.layout.verify,
                "executor.md": self.layout.executor_report,
                "reviewer.md": self.layout.reviewer_report,
            }
            manifest: dict[str, str] = {}
            for name, source in evidence.items():
                if not source.is_file():
                    raise BoxPorterError(f"missing archive evidence: {source}")
                data = source.read_bytes()
                target = staging / name
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                manifest[name] = hashlib.sha256(data).hexdigest()
            atomic_json(
                staging / "manifest.json",
                {
                    "schema": "BOXPORTER_ARCHIVE_V1",
                    "task_id": metadata["task_id"],
                    "submission_sha256": metadata.get("submission_sha256", ""),
                    "archived_at": now_iso(),
                    "files": manifest,
                },
            )
            _fsync_directory(staging)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.layout.active.unlink()
        _fsync_directory(destination.parent)
        _fsync_directory(self.layout.active.parent)
        self._event("task_passed", task_id=metadata["task_id"], archive=str(destination))
        return destination

    def block(self, reason: str) -> Path:
        metadata, body = self.active_task()
        metadata.update(state="BLOCKED", handoff_to="human", updated_at=now_iso(), blocker=reason)
        destination = self.layout.blocked / f"{metadata['task_id']}.md"
        atomic_write(self.layout.active, render_document(metadata, body))
        os.replace(self.layout.active, destination)
        _fsync_directory(destination.parent)
        _fsync_directory(self.layout.active.parent)
        self._event("task_blocked", task_id=metadata["task_id"], reason=reason)
        return destination

    def status(self) -> dict[str, Any]:
        self._require_initialized()
        data: dict[str, Any] = {
            "root": str(self.layout.root),
            "pending_count": len(list(self.layout.pending.glob("*.md"))),
            "passed_count": len([path for path in self.layout.passed.iterdir() if path.is_dir() and not path.name.startswith(".")]),
            "blocked_count": len(list(self.layout.blocked.glob("*.md"))),
            "active": None,
        }
        if self.layout.active.exists():
            metadata, _ = self.active_task()
            data["active"] = metadata
        return data

    def tick(self) -> dict[str, Any]:
        self._require_initialized()
        with DirectoryLock(self.layout.lock):
            if not self.layout.active.exists():
                promoted = self.promote()
                if promoted is None:
                    return {"action": "idle", "model_call": False}
            metadata, _ = self.active_task()
            state = metadata["state"]
            config = self._load_config()
            if state in {"READY", "REVISE"}:
                return self._trigger_once("executor", metadata, config)
            if state == "WORKING":
                age = max(0, int(time.time() - self.layout.active.stat().st_mtime))
                if age >= int(config["stale_seconds"]):
                    return self._trigger_once("executor", metadata, config, suffix=f"stale-{age}")
                return {"action": "working_healthy", "model_call": False, "age_seconds": age}
            if state == "REVIEW_PENDING":
                digest = submission_sha(self.layout.result, self.layout.verify)
                if digest != metadata.get("submission_sha256"):
                    raise BoxPorterError("review-pending submission hash mismatch")
                executor_meta, _ = parse_document(self.layout.executor_report)
                self._validate_report(executor_meta, "executor", metadata["task_id"], digest)
                return self._trigger_once("reviewer", metadata, config, suffix=digest)
            if state == "PASS":
                archive = self.archive_passed()
                return {"action": "archived", "model_call": False, "path": str(archive)}
            return {"action": "terminal", "model_call": False, "state": state}

    def doctor(self) -> list[str]:
        self._require_initialized()
        messages = ["layout: ok", "config: ok"]
        config = self._load_config()
        for role in ("executor", "reviewer"):
            command = config[f"{role}_command"]
            if not command:
                messages.append(f"{role}_command: manual")
                continue
            executable = command[0]
            if os.path.sep not in executable and shutil.which(executable) is None:
                raise BoxPorterError(f"{role} executable not found: {executable}")
            messages.append(f"{role}_command: ok")
        if self.layout.active.exists():
            self.active_task()
            messages.append("active_task: valid")
        return messages

    def _trigger_once(
        self,
        role: str,
        metadata: dict[str, str],
        config: dict[str, Any],
        suffix: str = "",
    ) -> dict[str, Any]:
        state = self._load_state()
        key = f"{role}:{metadata['task_id']}:{metadata['state']}:{suffix}"
        now_epoch = int(time.time())
        retry_seconds = int(config["retry_seconds"])
        if state.get("last_trigger_key") == key and now_epoch - int(state.get("last_trigger_at", 0)) < retry_seconds:
            return {"action": "deduplicated", "model_call": False, "trigger_key": key}
        command = config[f"{role}_command"]
        if not command:
            self._event("manual_handoff_required", role=role, task_id=metadata["task_id"])
            return {"action": "manual_handoff_required", "model_call": False, "role": role}
        substitutions = {
            "{root}": str(self.layout.root),
            "{workspace}": str(self.workspace),
            "{task}": str(self.layout.active),
            "{executor_report}": str(self.layout.executor_report),
            "{reviewer_report}": str(self.layout.reviewer_report),
        }
        argv = [substitutions.get(item, item) for item in command]
        env = os.environ.copy()
        env.update(
            BOXPORTER_ROOT=str(self.layout.root),
            BOXPORTER_WORKSPACE=str(self.workspace),
            BOXPORTER_TASK=str(self.layout.active),
            BOXPORTER_ROLE=role,
        )
        log_path = self.layout.root / "logs" / f"{role}.log"
        with log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        state.update(last_trigger_key=key, last_trigger_at=now_epoch, last_pid=process.pid)
        atomic_json(self.layout.state, state)
        self._event("agent_triggered", role=role, task_id=metadata["task_id"], pid=process.pid)
        return {"action": "triggered", "model_call": True, "role": role, "pid": process.pid}

    def _load_config(self) -> dict[str, Any]:
        try:
            raw_config = json.loads(self.layout.config.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise BoxPorterError(f"invalid config: {self.layout.config}") from exc
        if not isinstance(raw_config, dict):
            raise BoxPorterError(f"invalid config: {self.layout.config}")
        config = cast(dict[str, Any], raw_config)
        # Backward-compatible defaults for roots created by BoxPorter 0.1.
        config.setdefault("max_revisions", 2)
        config.setdefault("max_progress_extensions", 2)
        required_ints = (
            "poll_seconds",
            "stale_seconds",
            "retry_seconds",
            "max_revisions",
            "max_progress_extensions",
        )
        for key in required_ints:
            if not isinstance(config.get(key), int) or config[key] <= 0:
                raise BoxPorterError(f"config {key} must be a positive integer")
        for key in ("executor_command", "reviewer_command"):
            command = config.get(key)
            if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
                raise BoxPorterError(f"config {key} must be a string array")
        return config

    def _load_state(self) -> dict[str, Any]:
        try:
            raw_value = json.loads(self.layout.state.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return self._default_state()
        if not isinstance(raw_value, dict):
            return self._default_state()
        return cast(dict[str, Any], raw_value)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "last_trigger_key": "",
            "last_trigger_at": 0,
            "last_pid": 0,
            "review_history": {},
        }

    def _record_revision(
        self, task_id: str, digest: str, required_changes: list[str]
    ) -> dict[str, Any]:
        config = self._load_config()
        state = self._load_state()
        raw_history = state.setdefault("review_history", {})
        if not isinstance(raw_history, dict):
            raw_history = {}
            state["review_history"] = raw_history
        history = raw_history.setdefault(task_id, [])
        if not isinstance(history, list):
            history = []
            raw_history[task_id] = history
        replayed = bool(history and history[-1].get("submission_sha256") == digest)
        if replayed and history[-1].get("required_changes") != required_changes:
            raise BoxPorterError("conflicting review replay for the same submission")
        if not replayed:
            history.append(
                {
                    "submission_sha256": digest,
                    "required_changes": required_changes,
                    "reviewed_at": now_iso(),
                }
            )
            atomic_json(self.layout.state, state)

        attempts = len(history)
        base_limit = int(config["max_revisions"])
        absolute_limit = base_limit + int(config["max_progress_extensions"])
        if attempts < base_limit:
            return {
                "continue": True,
                "reason": "within_base_limit",
                "attempts": attempts,
                "replayed": replayed,
            }
        previous = history[-2].get("required_changes", []) if len(history) >= 2 else []
        progressing = len(required_changes) < len(previous)
        if progressing and attempts < absolute_limit:
            return {
                "continue": True,
                "reason": "required_change_count_reduced",
                "attempts": attempts,
                "replayed": replayed,
            }
        reason = "absolute_review_limit" if attempts >= absolute_limit else "review_not_converging"
        return {
            "continue": False,
            "reason": reason,
            "attempts": attempts,
            "replayed": replayed,
        }

    def _event(self, event: str, **fields: Any) -> None:
        record = {"time": now_iso(), "event": event, **fields}
        self.layout.root.mkdir(parents=True, exist_ok=True)
        with self.layout.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _require_initialized(self) -> None:
        if not self.layout.config.is_file():
            raise BoxPorterError(f"not initialized: {self.layout.root}")

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not TASK_ID_RE.fullmatch(task_id):
            raise BoxPorterError("task id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")

    def _validate_task(self, metadata: dict[str, str]) -> None:
        if metadata.get("schema") != TASK_SCHEMA:
            raise BoxPorterError("unsupported task schema")
        self._validate_task_id(metadata.get("task_id", ""))
        if metadata.get("state") not in VALID_STATES:
            raise BoxPorterError("invalid task state")

    @staticmethod
    def _validate_report(metadata: dict[str, str], role: str, task_id: str, digest: str) -> None:
        expected = {
            "schema": REPORT_SCHEMA,
            "role": role,
            "task": task_id,
            "submission_sha256": digest,
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise BoxPorterError(f"invalid report field {key}")
        if not metadata.get("author") or not metadata.get("report_id") or not metadata.get("time"):
            raise BoxPorterError("incomplete agent report")

    def _find_task(self, task_id: str) -> Path | None:
        candidates: Iterable[Path] = (
            [self.layout.active] if self.layout.active.exists() else []
        )
        candidates = list(candidates) + list(self.layout.pending.glob("*.md"))
        candidates += list(self.layout.passed.glob("*/task.md")) + list(self.layout.blocked.glob("*.md"))
        for path in candidates:
            try:
                metadata, _ = parse_document(path)
            except BoxPorterError:
                continue
            if metadata.get("task_id") == task_id:
                return path
        return None

    def _passed_task_ids(self) -> set[str]:
        task_ids: set[str] = set()
        for path in self.layout.passed.glob("*/task.md"):
            try:
                metadata, _ = parse_document(path)
            except BoxPorterError:
                continue
            if metadata.get("task_id"):
                task_ids.add(metadata["task_id"])
        return task_ids

    def _clear_reports(self) -> None:
        for path in (
            self.layout.result,
            self.layout.verify,
            self.layout.executor_report,
            self.layout.reviewer_report,
        ):
            path.unlink(missing_ok=True)

    def _existing_archive(self, task_id: str, digest: str) -> Path | None:
        for archive in self.layout.passed.iterdir():
            if not archive.is_dir() or archive.name.startswith("."):
                continue
            manifest_path = archive / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if manifest.get("task_id") == task_id and manifest.get("submission_sha256") == digest:
                return archive
        return None
