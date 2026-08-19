"""CLI for the V2 control plane (operation and manual driving)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from boxporter.application.commands import (
    BeginNextAttempt,
    BlockTask,
    CancelTask,
    CreateProject,
    CreateTask,
    FailRun,
    FinalizeTaskDone,
    ReadyTask,
    ReviewTask,
    SubmitExecutorRun,
    UnblockTask,
)
from boxporter.application.queries import events_since, latest_seq, project_boxes
from boxporter.core.errors import BoxPorterError
from boxporter.core.scheduler import SchedulingPolicy
from boxporter.core.schemas import TaskSpec
from boxporter.storage.db import Database
from boxporter.storage.store import Store


def build_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="boxporter-v2", description="BoxPorter V2 control plane")
    top.add_argument("--data-dir", default="data", help="BoxPorter data directory")
    commands = top.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize database and project")
    init.add_argument("--project-id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--workspace-root", required=True)

    add = commands.add_parser("add-task", help="Create a task from a JSON spec file")
    add.add_argument("--spec-file", required=True, type=Path)

    ready = commands.add_parser("ready", help="Validate and mark a task READY")
    ready.add_argument("task_id")

    commands.add_parser("tick", help="Run one deterministic scheduling tick")
    tick = commands.add_parser("tick-policy", help="Run one tick with an explicit policy")
    tick.add_argument("--mode", choices=("SUPERVISED", "AWAY", "PAUSED"), required=True)

    status = commands.add_parser("status", help="Four-box snapshot")
    status.add_argument("--project-id", required=True)

    events = commands.add_parser("events", help="Stream events after a cursor")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)

    complete = commands.add_parser("submit-run", help="Freeze a submission and move to review")
    complete.add_argument("run_id")
    complete.add_argument("--worktree", required=True)
    complete.add_argument("--report-dir", required=True)
    fail = commands.add_parser("fail-run", help="Mark a run crashed")
    fail.add_argument("run_id")
    fail.add_argument("--reason", default="manual")

    review = commands.add_parser("review", help="Record a review verdict")
    review.add_argument("task_id")
    review.add_argument("--reviewer-run", required=True)
    review.add_argument("--result", choices=("PASS", "REVISE", "BLOCKED"), required=True)
    review.add_argument("--required-change", action="append", default=[])
    review.add_argument("--review-dir")

    done = commands.add_parser("finalize", help="Seal evidence and move a PASS task to DONE")
    done.add_argument("task_id")
    done.add_argument("--evidence-root", required=True)

    verify = commands.add_parser("verify-package", help="Offline verify a PASSED evidence package")
    verify.add_argument("package_dir", type=Path)

    block = commands.add_parser("block", help="Block a task")
    block.add_argument("task_id")
    block.add_argument("--reason", required=True)
    unblock = commands.add_parser("unblock", help="Unblock a task")
    unblock.add_argument("task_id")

    retry = commands.add_parser("retry", help="Begin the next attempt of a task")
    retry.add_argument("task_id")
    cancel = commands.add_parser("cancel", help="Cancel a task")
    cancel.add_argument("task_id")

    set_pw = commands.add_parser("web-set-password", help="Set the Web console password")
    set_pw.add_argument("--password-env", default="BOXPORTER_ADMIN_PASSWORD")
    serve = commands.add_parser("serve", help="Run the Web console (127.0.0.1)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3088)

    report = commands.add_parser("report", help="Activity report for a time window")
    report.add_argument("--from", dest="from_iso", required=True)
    report.add_argument("--to", dest="to_iso", required=True)

    backup = commands.add_parser("backup", help="Create an online backup")
    backup.add_argument("--backup-root", required=True)
    backup.add_argument("--evidence-root", default=None)
    verify_bk = commands.add_parser("backup-verify", help="Verify a backup (restore drill)")
    verify_bk.add_argument("backup_dir", type=Path)

    daemon = commands.add_parser("daemon", help="Run the 24x7 porter daemon")
    daemon.add_argument("--policy", choices=("SUPERVISED", "AWAY", "PAUSED"), default="SUPERVISED")
    daemon.add_argument("--worktrees-root", default=None)
    daemon.add_argument("--tick-seconds", type=float, default=15.0)

    runners_cmd = commands.add_parser("runners", help="List configured runner capabilities")
    runners_cmd.add_argument("--require", action="store_true",
                             help="Fail when no runner is configured")

    return top


def open_store(data_dir: str) -> tuple[Database, Store]:
    db = Database(Path(data_dir) / "boxporter.sqlite")
    db.open()
    return db, Store(db)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db: Database | None = None
    try:
        db, store = open_store(args.data_dir)
        if args.command == "init":
            result = store.execute(
                CreateProject(
                    project_id=args.project_id,
                    name=args.name,
                    workspace_root=args.workspace_root,
                    actor_type="user",
                )
            )
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "add-task":
            spec = TaskSpec.from_dict(json.loads(args.spec_file.read_text(encoding="utf-8")))
            result = store.execute(CreateTask(spec=spec, actor_type="user"))
            print(json.dumps({"ok": result.ok, "message": result.message, "data": result.data}))
        elif args.command == "ready":
            result = store.execute(ReadyTask(task_id=args.task_id, actor_type="user"))
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "tick":
            policy = SchedulingPolicy()
            from boxporter.core.lease import LeaseManager
            from boxporter.core.recovery import RecoveryEngine
            from boxporter.core.scheduler import Scheduler
            from boxporter.core.watchdog import WatchDog
            from boxporter.runners import build_registry

            runners = build_registry()
            leases = LeaseManager(store)
            watchdog = WatchDog(store, leases)
            scheduler = Scheduler(store, runners, leases, watchdog, RecoveryEngine(store), policy)
            tick_result = scheduler.tick()
            print(
                json.dumps(
                    {
                        "action": tick_result.action,
                        "model_call": tick_result.model_call,
                        "detail": tick_result.detail,
                    }
                )
            )
        elif args.command == "tick-policy":
            policy = SchedulingPolicy(mode=args.mode)
            from boxporter.core.lease import LeaseManager
            from boxporter.core.recovery import RecoveryEngine
            from boxporter.core.scheduler import Scheduler
            from boxporter.core.watchdog import WatchDog
            from boxporter.runners import build_registry

            runners = build_registry()
            leases = LeaseManager(store)
            watchdog = WatchDog(store, leases)
            scheduler = Scheduler(store, runners, leases, watchdog, RecoveryEngine(store), policy)
            tick_result = scheduler.tick()
            print(
                json.dumps(
                    {
                        "action": tick_result.action,
                        "model_call": tick_result.model_call,
                        "detail": tick_result.detail,
                    }
                )
            )
        elif args.command == "status":
            boxes = project_boxes(store, args.project_id)
            summary = {
                box.value: [
                    {"task_id": item.task_id, "state": item.state, "title": item.title}
                    for item in items
                ]
                for box, items in boxes.items()
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "events":
            records = events_since(store, args.after, args.limit)
            print(
                json.dumps(
                    [
                        {
                            "seq": record.seq,
                            "event_type": record.event_type,
                            "aggregate_type": record.aggregate_type,
                            "aggregate_id": record.aggregate_id,
                            "payload": record.payload,
                        }
                        for record in records
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if args.after == 0:
                print(f"# latest seq: {latest_seq(store)}", file=sys.stderr)
        elif args.command == "submit-run":
            result = store.execute(
                SubmitExecutorRun(
                    run_id=args.run_id,
                    report_dir=args.report_dir,
                    worktree=args.worktree,
                    actor_type="daemon",
                )
            )
            print(json.dumps({"ok": result.ok, "message": result.message, "data": result.data}))
        elif args.command == "fail-run":
            result = store.execute(
                FailRun(run_id=args.run_id, kind="crash", stop_reason=args.reason,
                        actor_type="daemon")
            )
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "review":
            result = store.execute(
                ReviewTask(
                    task_id=args.task_id,
                    reviewer_run_id=args.reviewer_run,
                    result=args.result,
                    required_changes=tuple(args.required_change),
                    review_dir=args.review_dir,
                    actor_type="reviewer",
                )
            )
            print(json.dumps({"ok": result.ok, "message": result.message, "data": result.data}))
        elif args.command == "finalize":
            result = store.execute(
                FinalizeTaskDone(
                    task_id=args.task_id,
                    evidence_root=args.evidence_root,
                    actor_type="daemon",
                )
            )
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "verify-package":
            from boxporter.application.verifier import verify_package

            problems = verify_package(args.package_dir)
            if problems:
                print("\n".join(f"- {item}" for item in problems))
                return 1
            print(f"ok: {args.package_dir}")
        elif args.command == "block":
            result = store.execute(
                BlockTask(task_id=args.task_id, reason=args.reason, actor_type="user")
            )
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "unblock":
            result = store.execute(UnblockTask(task_id=args.task_id, actor_type="user"))
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "retry":
            result = store.execute(BeginNextAttempt(task_id=args.task_id, actor_type="user"))
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "cancel":
            result = store.execute(CancelTask(task_id=args.task_id, actor_type="user"))
            print(json.dumps({"ok": result.ok, "message": result.message}))
        elif args.command == "web-set-password":
            import getpass
            import os

            from boxporter.api.auth import hash_password

            password = os.environ.get(args.password_env) or getpass.getpass("Admin password: ")
            if not password:
                print("boxporter-v2: empty password", file=sys.stderr)
                return 1
            with store.db.transaction():
                store.settings.set(store.db.conn, "admin_password_hash", hash_password(password))
            print("ok: password hash stored")
        elif args.command == "serve":
            import uvicorn

            from boxporter.api.app import create_app

            web_dir = Path(__file__).parent / "web"
            app = create_app(store, web_dir=web_dir)
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        elif args.command == "report":
            from boxporter.core.report import activity_report

            print(activity_report(store, args.from_iso, args.to_iso))
        elif args.command == "runners":
            from boxporter.runners import build_registry

            registry = build_registry(require_runner=args.require)
            print(
                json.dumps(
                    [
                        registry.get(name).capabilities().__dict__
                        for name in registry.names()
                    ],
                    indent=2,
                )
            )
        elif args.command == "backup":
            from boxporter.operations.backup import BackupService

            report = BackupService(
                db.path,
                evidence_root=Path(args.evidence_root) if args.evidence_root else None,
            ).create(Path(args.backup_root))
            print(json.dumps({"ok": True, "backup_dir": report.backup_dir,
                              "files": list(report.files)}))
        elif args.command == "backup-verify":
            from boxporter.operations.backup import verify_backup

            problems = verify_backup(args.backup_dir)
            if problems:
                print("\n".join(f"- {item}" for item in problems))
                return 1
            print(f"ok: {args.backup_dir}")
        elif args.command == "daemon":
            from boxporter.core.policy import PolicyService
            from boxporter.daemon import DaemonConfig, PorterDaemon
            from boxporter.runners import build_registry

            registry = build_registry()
            snapshot = PolicyService(store).read()
            policy = SchedulingPolicy(
                mode=snapshot.mode,
                max_concurrent=snapshot.max_concurrent,
                allowed_risk_levels=snapshot.allowed_risk_levels,
                auto_review=snapshot.auto_review,
                max_recoveries_per_attempt=snapshot.max_recoveries_per_attempt,
                daily_token_budget=snapshot.daily_token_budget,
            )
            config = DaemonConfig(
                tick_seconds=args.tick_seconds,
                worktrees_root=Path(args.worktrees_root) if args.worktrees_root else None,
            )
            daemon = PorterDaemon(store, registry, policy, config)
            print(f"porter daemon starting (mode={policy.mode})", file=sys.stderr)
            daemon.run_forever()
        return 0
    except (BoxPorterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"boxporter-v2: {exc}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
