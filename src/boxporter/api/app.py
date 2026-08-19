"""FastAPI application: JSON API + SSE event stream + static console.

The browser is only a control surface (ADR-012): every mutation goes
through Store.execute (state machine + idempotency + audit events), and
closing the page never changes run lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from boxporter.application.commands import (
    BeginNextAttempt,
    BlockTask,
    CancelTask,
    CreateTask,
    FailRun,
    FinalizeTaskDone,
    MarkRunRunning,
    ReadyTask,
    ReviewTask,
    UnblockTask,
)
from boxporter.application.queries import (
    events_since,
    latest_seq,
    project_boxes,
    task_detail,
)
from boxporter.core.clock import now_iso
from boxporter.core.errors import BoxPorterError, NotFoundError
from boxporter.core.schemas import TaskSpec
from boxporter.core.state import TaskState, check_task_transition
from boxporter.storage.store import Store

from .auth import SESSION_COOKIE, AuthContext, AuthManager
from .errors import map_error

MAX_EVENTS_PER_PAGE = 500


def _box_of(state: str) -> str:
    from boxporter.core.boxes import box_for
    from boxporter.core.state import TaskState

    return box_for(TaskState(state)).value


class LoginRequest(BaseModel):
    username: str
    password: str
    device_label: str = "web"


class ReauthRequest(BaseModel):
    password: str


class TaskCreateRequest(BaseModel):
    spec: dict[str, Any]


class ReviewRequest(BaseModel):
    reviewer_run_id: str
    result: str
    required_changes: list[str] = []
    review_dir: str | None = None
    note: str = ""


class ModeRequest(BaseModel):
    mode: str


class ReasonRequest(BaseModel):
    reason: str = ""
    note: str = ""


def create_app(store: Store, *, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="BoxPorter", version="0.2.0")
    auth = AuthManager(store)

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next: Any) -> Response:
        request.state.trace_id = uuid.uuid4().hex[:12]
        response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next: Any) -> Response:
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path.startswith("/api/")
            and request.url.path != "/api/auth/login"
            and not request.headers.get("x-boxporter-client")
        ):
            return JSONResponse(
                status_code=400, content={"detail": "missing client header"}
            )
        response: Response = await call_next(request)
        return response

    def audit(request: Request, operation: str, **payload: object) -> None:
        session_id = getattr(getattr(request.state, "auth", None), "session", None)
        client_ip = request.client.host if request.client else "unknown"
        trace_id = getattr(request.state, "trace_id", None)
        conn = store.db.conn
        with store.db.transaction():
            store.events.append(
                conn,
                aggregate_type="web",
                aggregate_id="console",
                event_type="REMOTE_OPERATION",
                actor_type="user",
                actor_id=session_id.id if session_id is not None else None,
                correlation_id=trace_id,
                payload={
                    "operation": operation,
                    "path": request.url.path,
                    "ip": client_ip,
                    "trace_id": trace_id,
                    **payload,
                },
            )

    # -- auth -------------------------------------------------------------

    @app.post("/api/auth/login")
    async def login(request: Request, body: LoginRequest, response: Response) -> dict[str, object]:
        if body.username.strip() != "admin":
            raise HTTPException(status_code=401, detail="wrong username")
        session_id = auth.login(body.password, body.device_label)
        response.set_cookie(
            SESSION_COOKIE,
            auth.cookie_value(session_id),
            httponly=True,
            samesite="strict",
            path="/api",
            max_age=30 * 24 * 3600,
        )
        return {"ok": True, "session_id": session_id}

    @app.post("/api/auth/logout")
    async def logout(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> JSONResponse:
        auth.logout(context.session.id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/api")
        return response

    @app.post("/api/auth/reauthenticate")
    async def reauthenticate(
        request: Request,
        body: ReauthRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.reauthenticate(context.session.id, body.password)
        return {"ok": True}

    @app.get("/api/auth/sessions")
    async def list_sessions(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        sessions = store.web_sessions.list_active(store.db.conn)
        return {
            "sessions": [
                {
                    "id": item.id,
                    "device_label": item.device_label,
                    "created_at": item.created_at,
                    "last_seen_at": item.last_seen_at,
                    "expires_at": item.expires_at,
                    "current": item.id == context.session.id,
                }
                for item in sessions
            ]
        }

    @app.post("/api/auth/sessions/{session_id}/revoke")
    async def revoke_session(
        request: Request,
        session_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        with store.db.transaction():
            store.web_sessions.revoke(store.db.conn, session_id)
        audit(request, "revoke_session", target=session_id)
        return {"ok": True}

    # -- system -----------------------------------------------------------

    @app.get("/api/system/health")
    async def health() -> dict[str, object]:
        import shutil
        import time

        def component(name: str, status: str, detail: object = None) -> dict[str, object]:
            value: dict[str, object] = {"name": name, "status": status}
            if detail is not None:
                value["detail"] = detail
            return value

        components: dict[str, object] = {}

        try:
            store.db.conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:  # noqa: BLE001 - health probe
            db_ok = False
        components["control_plane"] = component(
            "Control Plane",
            "up" if db_ok else "down",
            {"latest_seq": latest_seq(store)},
        )

        integrity = "ok"
        try:
            if store.db.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                integrity = "degraded"
        except Exception:  # noqa: BLE001 - health probe
            integrity = "down"
        components["database"] = component("Database", integrity)

        runners = store.settings.get(store.db.conn, "runner_capabilities", [])
        if isinstance(runners, list) and runners:
            runner_names = [str(item.get("name")) for item in runners if isinstance(item, dict)]
            openhands_registered = any(name == "openhands" for name in runner_names)
            components["openhands"] = component(
                "OpenHands",
                "registered" if openhands_registered else "not-registered",
            )
            components["runner_registry"] = component("Runner Registry", "up", runner_names)
        else:
            components["openhands"] = component("OpenHands", "not-registered")
            components["runner_registry"] = component("Runner Registry", "empty")

        try:
            usage = shutil.disk_usage(str(store.db.path.parent))
            free_gb = usage.free // (1024 ** 3)
            components["disk"] = component(
                "Disk", "ok" if free_gb > 5 else "warn", {"free_gb": free_gb}
            )
        except OSError:
            components["disk"] = component("Disk", "unknown")

        backup_dir = store.db.path.parent.parent / "backups"
        if backup_dir.is_dir() and any(backup_dir.iterdir()):
            latest = max(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime)
            age_hours = (time.time() - latest.stat().st_mtime) / 3600
            components["backup"] = component(
                "Backup", "ok" if age_hours < 48 else "warn",
                {"age_hours": round(age_hours, 1)},
            )
        else:
            components["backup"] = component("Backup", "warn", "no backups yet")

        components["remote_access"] = component(
            "Remote Access",
            "local-only",
            "web bound to 127.0.0.1; use Tailscale or SSH tunnel",
        )

        warnings = [item["name"] for item in components.values()
                    if isinstance(item, dict) and item.get("status") in {"warn", "empty", "not-registered", "unknown"}]
        return {
            "ok": db_ok,
            "latest_seq": latest_seq(store),
            "components": components,
            "warnings": warnings,
        }

    @app.get("/api/system/runners")
    async def runners(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        return {
            "runners": store.settings.get(
                store.db.conn, "runner_capabilities", []
            )
        }

    # -- projects ---------------------------------------------------------

    @app.get("/api/projects")
    async def list_projects(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        rows = store.db.conn.execute(
            "SELECT id, name, workspace_root, status FROM projects"
        ).fetchall()
        return {"projects": [dict(row) for row in rows]}

    @app.get("/api/projects/{project_id}/dashboard")
    async def dashboard(
        request: Request,
        project_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        boxes = project_boxes(store, project_id)
        return {
            "project_id": project_id,
            "boxes": {
                box.value: [item.__dict__ for item in items]
                for box, items in boxes.items()
            },
            "counts": {
                box.value: len(items) for box, items in boxes.items()
            },
        }

    # -- tasks ------------------------------------------------------------

    @app.get("/api/tasks")
    async def list_tasks(
        request: Request,
        project_id: str | None = None,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        query = "SELECT id, project_id, title, state, priority, risk_level,"
        " current_attempt, created_at FROM tasks"
        params: tuple[Any, ...] = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        query += " ORDER BY priority DESC, created_at"
        rows = store.db.conn.execute(query, params).fetchall()
        return {"tasks": [dict(row) for row in rows]}

    @app.post("/api/tasks")
    async def create_task(
        request: Request,
        body: TaskCreateRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        spec = TaskSpec.from_dict(body.spec)
        result = store.execute(
            CreateTask(spec=spec, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "create_task", task_id=spec.task_id)
        return {
            "ok": result.ok,
            "message": result.message,
            "data": result.data,
            "trace_id": getattr(request.state, "trace_id", None),
        }

    @app.post("/api/tasks/import")
    async def import_task(
        request: Request,
        file: UploadFile | None = File(default=None),
        spec_json: str | None = Form(default=None),
        project_id: str | None = Form(default=None),
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        """Create a task from an uploaded JSON file or pasted JSON text
        (remediation §3.1.1). Both paths share the same strict schema
        validation and idempotency semantics as POST /api/tasks."""
        trace_id = getattr(request.state, "trace_id", None)
        if file is not None:
            raw = await file.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "INVALID_ENCODING",
                        "message": "spec 文件必须是 UTF-8 编码",
                        "hint": "用 UTF-8 重新保存 .json 文件",
                        "trace_id": trace_id,
                    },
                )
        elif spec_json and spec_json.strip():
            text = spec_json
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EMPTY_INPUT",
                    "message": "需要提供 spec 文件或 JSON 文本",
                    "hint": "选择本地 .json 文件，或粘贴 BOXPORTER_TASK_V2 JSON",
                    "trace_id": trace_id,
                },
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_JSON",
                    "message": f"JSON 解析失败: {exc.msg}",
                    "field": f"line {exc.lineno} column {exc.colno}",
                    "hint": "检查 JSON 语法（引号/逗号/括号配对）",
                    "trace_id": trace_id,
                },
            )
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_SPEC",
                    "message": "spec 顶层必须是 JSON 对象",
                    "hint": "参照 BOXPORTER_TASK_V2 结构",
                    "trace_id": trace_id,
                },
            )
        if project_id and "project_id" not in value:
            value["project_id"] = project_id
        spec = TaskSpec.from_dict(value)
        result = store.execute(
            CreateTask(spec=spec, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "import_task", task_id=spec.task_id)
        return {
            "ok": result.ok,
            "message": result.message,
            "data": result.data,
            "trace_id": trace_id,
        }

    @app.get("/api/tasks/{task_id}/readiness")
    async def readiness(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        """Startup-condition check: machine-readable gaps that keep the
        task from READY (workspace, dependencies, state machine)."""
        try:
            detail = task_detail(store, task_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        task = detail.task
        gaps: list[dict[str, object]] = []
        transition_ok = True
        try:
            check_task_transition(task.state, TaskState.READY)
        except BoxPorterError as exc:
            transition_ok = False
            gaps.append(
                {"field": "state", "message": str(exc), "hint": "当前状态不可转 READY"}
            )
        if transition_ok:
            for gap in ReadyTask._readiness_gaps(store, task):
                if gap.startswith("workspace does not exist"):
                    gaps.append(
                        {
                            "field": "workspace",
                            "message": gap,
                            "hint": "在磁盘创建该目录后重试",
                        }
                    )
                elif gap == "dependencies not satisfied":
                    gaps.append(
                        {
                            "field": "dependencies",
                            "message": gap,
                            "hint": "依赖任务须 PASSED 并封箱",
                        }
                    )
                else:
                    gaps.append({"field": None, "message": gap, "hint": None})
        return {
            "task_id": task_id,
            "state": task.state.value,
            "ready": len(gaps) == 0,
            "gaps": gaps,
        }

    @app.get("/api/tasks/{task_id}")
    async def task_details(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        try:
            detail = task_detail(store, task_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        submission = None
        reviews: list[dict[str, Any]] = []
        if detail.task.current_attempt:
            attempt = store.attempts.get_by_task_number(
                store.db.conn, task_id, detail.task.current_attempt
            )
            found = store.submissions.get_for_attempt(store.db.conn, attempt.id)
            if found is not None:
                submission = {
                    "id": found.id,
                    "submission_sha256": found.submission_sha256,
                    "head_commit": found.head_commit,
                    "frozen_at": found.frozen_at,
                    "invalidated": found.invalidated_at is not None,
                }
                reviews = [
                    review.__dict__
                    for review in store.reviews.get_for_submission(
                        store.db.conn, found.id
                    )
                ]
        return {
            "task": {
                "id": detail.task.id,
                "project_id": detail.task.project_id,
                "title": detail.task.title,
                "objective": detail.task.objective,
                "state": detail.task.state.value,
                "box": _box_of(detail.task.state.value),
                "priority": detail.task.priority,
                "risk_level": detail.task.risk_level,
                "current_attempt": detail.task.current_attempt,
                "max_attempts": detail.task.max_attempts,
                "acceptance_criteria": list(detail.task.spec.acceptance_criteria),
            },
            "attempts": [attempt.__dict__ for attempt in detail.attempts],
            "runs": [
                {
                    "id": run.id,
                    "role": run.role,
                    "runner": run.runner,
                    "state": run.state.value,
                    "identity": run.identity,
                    "session_id": run.session_id,
                    "worktree": run.worktree,
                    "stop_reason": run.stop_reason,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                }
                for run in detail.runs
            ],
            "submission": submission,
            "reviews": reviews,
            "events": [
                {
                    "seq": event.seq,
                    "event_type": event.event_type,
                    "occurred_at": event.occurred_at,
                    "payload": event.payload,
                }
                for event in detail.events
            ],
        }

    def task_action(task_id: str, command: object, operation: str) -> dict[str, object]:
        del task_id, operation  # reserved; commands always go through store.execute
        result = store.execute(command, operation_id=None)  # type: ignore[arg-type]
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.post("/api/tasks/{task_id}/ready")
    async def ready(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        result = store.execute(
            ReadyTask(task_id=task_id, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "ready", task_id=task_id)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        result = store.execute(
            CancelTask(task_id=task_id, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "cancel", task_id=task_id)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/tasks/{task_id}/block")
    async def block(
        request: Request,
        task_id: str,
        body: ReasonRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        result = store.execute(
            BlockTask(
                task_id=task_id,
                reason=body.reason,
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "block", task_id=task_id, reason=body.reason)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/tasks/{task_id}/unblock")
    async def unblock(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        result = store.execute(
            UnblockTask(task_id=task_id, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "unblock", task_id=task_id)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/tasks/{task_id}/retry")
    async def retry(
        request: Request,
        task_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        result = store.execute(
            BeginNextAttempt(task_id=task_id, actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "retry", task_id=task_id)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/tasks/{task_id}/review")
    async def review(
        request: Request,
        task_id: str,
        body: ReviewRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        result = store.execute(
            ReviewTask(
                task_id=task_id,
                reviewer_run_id=body.reviewer_run_id,
                result=body.result,
                required_changes=tuple(body.required_changes),
                review_dir=body.review_dir,
                note=body.note,
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "review", task_id=task_id, result=body.result)
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.post("/api/tasks/{task_id}/finalize")
    async def finalize(
        request: Request,
        task_id: str,
        body: ReasonRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        evidence_root = str(body.note) or None
        result = store.execute(
            FinalizeTaskDone(
                task_id=task_id,
                evidence_root=evidence_root,
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "finalize", task_id=task_id)
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(
        request: Request,
        run_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        result = store.execute(
            FailRun(run_id=run_id, kind="crash", stop_reason="stopped by user",
                    actor_type="user", actor_id=context.session.id),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "stop_run", run_id=run_id)
        return {"ok": result.ok, "message": result.message}

    # -- runs (plan §15.3: GET /runs/{id}, GET /runs/{id}/events, resume) --

    @app.get("/api/runs/{run_id}")
    async def run_details(
        request: Request,
        run_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.lease import LeaseManager

        try:
            run = store.runs.get(store.db.conn, run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        attempt = store.attempts.get(store.db.conn, run.attempt_id)
        lease = LeaseManager(store).get(run_id)
        return {
            "run": {
                "id": run.id,
                "role": run.role,
                "runner": run.runner,
                "provider": run.provider,
                "model": run.model,
                "identity": run.identity,
                "session_id": run.session_id,
                "state": run.state.value,
                "worktree": run.worktree,
                "prompt_sha": run.prompt_sha,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "stop_reason": run.stop_reason,
            },
            "task_id": attempt.task_id,
            "attempt": attempt.number,
            "lease": (
                {
                    "fencing_token": lease.fencing_token,
                    "owner_instance": lease.owner_instance,
                    "pid": lease.pid,
                    "heartbeat_at": lease.heartbeat_at,
                    "expires_at": lease.expires_at,
                }
                if lease is not None
                else None
            ),
        }

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        request: Request,
        run_id: str,
        after_cursor: int = 0,
        limit: int = 200,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        records = store.events.for_aggregate(
            store.db.conn, "run", run_id, after_seq=after_cursor
        )[:limit]
        return {
            "events": [record.__dict__ for record in records],
            "next_cursor": records[-1].seq if records else after_cursor,
            "latest_seq": latest_seq(store),
        }

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(
        request: Request,
        run_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        """Resume is bound by the state machine and ADR-015: only paused
        intermediate states can return to RUNNING; crashed/timed-out runs
        are rejected with a pointer to the retry path (new Attempt)."""
        auth.require_reauth(context)
        try:
            run = store.runs.get(store.db.conn, run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if run.state.value in {"STALLED", "WAITING_APPROVAL"}:
            result = store.execute(
                MarkRunRunning(run_id=run_id, actor_type="user",
                               actor_id=context.session.id),
                operation_id=request.headers.get("Idempotency-Key"),
            )
            audit(request, "resume_run", run_id=run_id, state=run.state.value)
            return {"ok": result.ok, "message": result.message,
                    "data": result.data}
        attempt = store.attempts.get(store.db.conn, run.attempt_id)
        if run.state.value in {"CRASHED", "TIMED_OUT", "CANCELED"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RESUME_UNSUPPORTED",
                    "field": "run.state",
                    "message": f"run {run_id} 处于 {run.state.value}，"
                               "不支持原地恢复（ADR-015）",
                    "hint": "对任务执行 retry 走新 Attempt："
                            f"POST /api/tasks/{attempt.task_id}/retry",
                    "trace_id": getattr(request.state, "trace_id", None),
                },
            )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RESUME_UNSUPPORTED",
                "field": "run.state",
                "message": f"run {run_id} 处于 {run.state.value}，无需恢复",
                "hint": "运行仍在推进；如需干预请使用 stop",
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )

    # -- blockers (plan §16.7) ---------------------------------------------

    @app.get("/api/blockers")
    async def blockers(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        rows = store.db.conn.execute(
            "SELECT b.*, t.title AS task_title, t.state AS task_state FROM blockers b"
            " LEFT JOIN tasks t ON t.id = b.task_id"
            " WHERE b.resolved_at IS NULL ORDER BY b.created_at DESC"
        ).fetchall()
        return {
            "blockers": [
                {
                    "id": str(row["id"]),
                    "task_id": str(row["task_id"]),
                    "task_title": row["task_title"],
                    "task_state": row["task_state"],
                    "reason": str(row["reason"]),
                    "probe_command": json.loads(str(row["probe_command_json"])),
                    "probe_interval_seconds": int(row["probe_interval_seconds"]),
                    "next_probe_at": row["next_probe_at"],
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]
        }

    # -- settings ---------------------------------------------------------

    @app.get("/api/settings/mode")
    async def get_mode(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.policy import PolicyService

        return {"mode": PolicyService(store).read().mode}

    @app.post("/api/settings/mode")
    async def set_mode(
        request: Request,
        body: ModeRequest,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        if body.mode not in {"SUPERVISED", "AWAY", "PAUSED"}:
            raise HTTPException(status_code=400, detail="invalid mode")
        from boxporter.core.policy import PolicyService

        PolicyService(store).set_mode(body.mode)
        audit(request, "set_mode", mode=body.mode)
        return {"ok": True, "mode": body.mode}

    @app.get("/api/settings/policy")
    async def get_policy(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.policy import PolicyService

        snapshot = PolicyService(store).read()
        return {
            "mode": snapshot.mode,
            "allowed_risk_levels": sorted(snapshot.allowed_risk_levels),
            "max_concurrent": snapshot.max_concurrent,
            "auto_review": snapshot.auto_review,
            "daily_token_budget": snapshot.daily_token_budget,
            "max_recoveries_per_attempt": snapshot.max_recoveries_per_attempt,
        }

    @app.post("/api/settings/policy")
    async def set_policy(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        from boxporter.core.policy import PolicyService

        PolicyService(store).write(body)
        audit(request, "set_policy")
        return {"ok": True}

    # -- reports and notifications -----------------------------------------

    @app.get("/api/reports/activity")
    async def reports(
        request: Request,
        frm: str = "2020-01-01T00:00:00Z",
        to: str = "2099-01-01T00:00:00Z",
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.report import activity_report

        return {"report": activity_report(store, frm, to)}

    @app.get("/api/notifications")
    async def notifications(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        return {"notifications": store.notifications.list_since(store.db.conn)}

    # -- approvals (ADR-009) ----------------------------------------------

    @app.get("/api/approvals")
    async def approvals(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        return {"approvals": [item.__dict__ for item in store.approvals.list_all(store.db.conn)]}

    @app.post("/api/approvals")
    async def request_approval(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.application.commands import RequestApproval

        max_uses_raw = body.get("max_uses", 1)
        ttl_raw = body.get("ttl_seconds", 3600)
        result = store.execute(
            RequestApproval(
                task_id=str(body.get("task_id", "")),
                action=str(body.get("action", "")),
                target=str(body.get("target", "")),
                risk_level=str(body.get("risk_level", "high")),
                max_uses=int(max_uses_raw) if isinstance(max_uses_raw, (int, str)) else 1,
                ttl_seconds=int(ttl_raw) if isinstance(ttl_raw, (int, str)) else 3600,
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "request_approval")
        return {"ok": result.ok, "message": result.message, "data": result.data}

    @app.post("/api/approvals/{approval_id}/approve")
    async def approve(
        request: Request,
        approval_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        from boxporter.application.commands import DecideApproval

        result = store.execute(
            DecideApproval(
                approval_id=approval_id,
                decision="approve",
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "approve", approval_id=approval_id)
        return {"ok": result.ok, "message": result.message}

    @app.post("/api/approvals/{approval_id}/reject")
    async def reject(
        request: Request,
        approval_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        from boxporter.application.commands import DecideApproval

        result = store.execute(
            DecideApproval(
                approval_id=approval_id,
                decision="reject",
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "reject", approval_id=approval_id)
        return {"ok": result.ok, "message": result.message}

    # -- goals -------------------------------------------------------------

    @app.get("/api/goals")
    async def goals(
        request: Request,
        project_id: str | None = None,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        query = "SELECT id, project_id, title, outcome, progress, status FROM goals"
        params: tuple[Any, ...] = ()
        if project_id:
            query += " WHERE project_id = ?"
            params = (project_id,)
        rows = store.db.conn.execute(query, params).fetchall()
        return {"goals": [dict(row) for row in rows]}

    @app.post("/api/goals")
    async def create_goal(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.application.commands import CreateGoal

        criteria_raw = body.get("success_criteria", [])
        criteria = criteria_raw if isinstance(criteria_raw, list) else []
        result = store.execute(
            CreateGoal(
                goal_id=str(body.get("goal_id", "")),
                project_id=str(body.get("project_id", "")),
                title=str(body.get("title", "")),
                outcome=str(body.get("outcome", "")),
                success_criteria=tuple(str(item) for item in criteria),
                actor_type="user",
                actor_id=context.session.id,
            ),
            operation_id=request.headers.get("Idempotency-Key"),
        )
        audit(request, "create_goal", goal_id=body.get("goal_id"))
        return {"ok": result.ok, "message": result.message}

    # -- settings: models and prompts --------------------------------------

    @app.get("/api/settings/models")
    async def get_models(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        return {"models": store.settings.get(store.db.conn, "model_profiles", [])}

    @app.post("/api/settings/models")
    async def set_models(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        profiles = body.get("models")
        if not isinstance(profiles, list):
            raise HTTPException(status_code=400, detail="models must be a list")
        with store.db.transaction():
            store.settings.set(store.db.conn, "model_profiles", profiles)
        audit(request, "set_models")
        return {"ok": True}

    @app.get("/api/settings/prompts")
    async def get_prompts(
        request: Request,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.prompts import PromptService

        service = PromptService(store)
        return {
            "prompts": {
                role: {"version": service.get(role)[0], "content": service.get(role)[1]}
                for role in ("executor", "reviewer", "planner", "supervisor")
            }
        }

    @app.post("/api/settings/prompts")
    async def set_prompt(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        auth.require_reauth(context)
        from boxporter.core.prompts import PromptService

        role = str(body.get("role", ""))
        content = str(body.get("content", ""))
        if not role or not content:
            raise HTTPException(status_code=400, detail="role and content required")
        version = PromptService(store).set(role, content)
        audit(request, "set_prompt", role=role, version=version)
        return {"ok": True, "version": version}

    # -- project memory (ADR-007/§12.3) ------------------------------------

    @app.get("/api/memory")
    async def memory(
        request: Request,
        project_id: str,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        return {
            "items": [
                item.__dict__
                for item in store.memory.list_for_project(store.db.conn, project_id)
            ]
        }

    @app.post("/api/memory")
    async def add_memory(
        request: Request,
        body: dict[str, object],
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        from boxporter.core.ids import new_id
        from boxporter.storage.metering import MemoryItem

        project_id = str(body.get("project_id", ""))
        content = str(body.get("content", ""))
        if not project_id or not content:
            raise HTTPException(status_code=400, detail="project_id and content required")
        item = MemoryItem(
            id=new_id("mem"),
            project_id=project_id,
            kind=str(body.get("kind", "user-note")),
            content=content,
            source="user-confirmed",
            source_ref=None,
            expires_at=None,
            created_at=now_iso(),
        )
        with store.db.transaction():
            store.memory.insert(store.db.conn, item)
        audit(request, "add_memory", project_id=project_id)
        return {"ok": True, "id": item.id}

    # -- events -----------------------------------------------------------

    @app.get("/api/events")
    async def events(
        request: Request,
        after_cursor: int = 0,
        context: AuthContext = Depends(auth.require_session),
    ) -> dict[str, object]:
        records = events_since(store, after_cursor, MAX_EVENTS_PER_PAGE)
        return {
            "events": [record.__dict__ for record in records],
            "latest_seq": latest_seq(store),
        }

    @app.get("/api/events/stream")
    async def events_stream(
        request: Request,
        after_cursor: int = 0,
    ) -> StreamingResponse:
        auth.resolve_session(request, request.cookies.get(SESSION_COOKIE))
        last_event_id = request.headers.get("last-event-id")
        cursor = after_cursor
        if last_event_id and last_event_id.isdigit():
            cursor = max(cursor, int(last_event_id))

        async def generator() -> Any:
            last_sent = cursor
            while True:
                if await request.is_disconnected():
                    break
                records = events_since(store, last_sent, MAX_EVENTS_PER_PAGE)
                for record in records:
                    yield f"id: {record.seq}\n"
                    yield "event: boxporter\n"
                    yield f"data: {json.dumps(record.__dict__, ensure_ascii=True)}\n\n"
                    last_sent = record.seq
                await asyncio.sleep(1.0)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # -- static console ---------------------------------------------------

    if web_dir is not None and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="console")

    @app.exception_handler(BoxPorterError)
    async def boxporter_error(request: Request, exc: BoxPorterError) -> JSONResponse:
        # Failed remote operations are audited too (ADR-012: 100% audit).
        client_ip = request.client.host if request.client else "unknown"
        trace_id = getattr(request.state, "trace_id", None)
        conn = store.db.conn
        with store.db.transaction():
            store.events.append(
                conn,
                aggregate_type="web",
                aggregate_id="console",
                event_type="REMOTE_OPERATION",
                actor_type="user",
                correlation_id=trace_id,
                payload={
                    "operation": "failed",
                    "path": request.url.path,
                    "ip": client_ip,
                    "error": str(exc),
                    "trace_id": trace_id,
                },
            )
        error = map_error(exc, str(trace_id))
        return JSONResponse(status_code=409, content={"ok": False, "error": error.to_dict()})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        detail = exc.detail
        if isinstance(detail, dict) and "message" in detail:
            body = {
                "ok": False,
                "code": detail.get("code", "HTTP_ERROR"),
                "field": detail.get("field"),
                "message": str(detail.get("message")),
                "hint": detail.get("hint"),
                "trace_id": detail.get("trace_id") or trace_id,
            }
        else:
            message = str(detail) if isinstance(detail, str) else json.dumps(detail)
            body = {
                "ok": False,
                "code": "HTTP_ERROR",
                "message": message,
                "detail": detail,
                "hint": None,
                "trace_id": trace_id,
            }
            if exc.status_code == status.HTTP_401_UNAUTHORIZED and "reauthentication" in message:
                body["code"] = "REAUTH_REQUIRED"
                body["hint"] = "高风险操作需要重新输入密码认证（10 分钟有效）"
        headers = dict(exc.headers) if exc.headers else {}
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)

    return app
