from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import BoxPorter, BoxPorterError


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="boxporter", description="Human-readable task boxes for coding agents")
    top.add_argument("--root", default=".boxporter", help="BoxPorter state directory")
    top.add_argument("--workspace", default=".", help="Workspace used by agent commands")
    commands = top.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create an empty task-box layout")
    init.add_argument("--force", action="store_true")

    add = commands.add_parser("add", help="Add a task to the pending box")
    add.add_argument("--id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--author", default="human")
    add.add_argument("--depends-on", action="append", default=[])
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--body")
    source.add_argument("--file", type=Path)

    commands.add_parser("promote", help="Move the oldest pending task into the active box")
    commands.add_parser("status", help="Show a compact task-box snapshot").add_argument("--json", action="store_true")

    transition = commands.add_parser("transition", help="Change the active task state")
    transition.add_argument("state")
    transition.add_argument("--handoff-to")

    submit = commands.add_parser("submit", help="Seal result.md and verify.md for independent review")
    submit.add_argument("--author", required=True)
    submit.add_argument("--content", default="Implementation and verification submitted.")

    review = commands.add_parser("review", help="Record an independent review")
    review.add_argument("--result", choices=("PASS", "REVISE", "INVALID"), required=True)
    review.add_argument("--author", required=True)
    review.add_argument("--content", required=True)
    review.add_argument("--required-change", action="append", default=[])
    review.add_argument("--required-changes", default="", help="Legacy comma-separated form")

    block = commands.add_parser("block", help="Move the active task to the blocked box")
    block.add_argument("--reason", required=True)

    commands.add_parser("tick", help="Run one zero-cost coordination decision")
    commands.add_parser("doctor", help="Validate layout, config, commands, and active task")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    porter = BoxPorter(args.root, args.workspace)
    try:
        if args.command == "init":
            porter.init(force=args.force)
            print(f"initialized: {porter.layout.root}")
        elif args.command == "add":
            body = args.body if args.body is not None else args.file.read_text(encoding="utf-8")
            print(porter.add(args.id, args.title, body, args.author, args.depends_on))
        elif args.command == "promote":
            print(porter.promote() or "no pending task")
        elif args.command == "status":
            status = porter.status()
            if args.json:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                active = status["active"]
                print(f"active: {active['task_id'] + ' [' + active['state'] + ']' if active else 'none'}")
                print(f"pending: {status['pending_count']}")
                print(f"passed: {status['passed_count']}")
                print(f"blocked: {status['blocked_count']}")
        elif args.command == "transition":
            print(json.dumps(porter.transition(args.state, args.handoff_to), ensure_ascii=False))
        elif args.command == "submit":
            print(porter.submit(args.author, args.content))
        elif args.command == "review":
            changes = args.required_change or args.required_changes
            archive = porter.review(args.result, args.author, args.content, changes)
            if archive:
                print(archive)
            else:
                print(porter.active_task()[0]["state"].lower())
        elif args.command == "block":
            print(porter.block(args.reason))
        elif args.command == "tick":
            print(json.dumps(porter.tick(), ensure_ascii=False))
        elif args.command == "doctor":
            print("\n".join(porter.doctor()))
    except (BoxPorterError, OSError, ValueError) as exc:
        print(f"boxporter: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
