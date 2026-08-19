"""Structured activity reports over arbitrary time windows (plan §10.7).

Generated from events and metering — zero model calls. An optional
natural-language summary hook may be added later without changing this
deterministic core.
"""

from __future__ import annotations

from boxporter.application.queries import events_since
from boxporter.core.state import TaskState
from boxporter.storage.store import Store


def activity_report(store: Store, from_iso: str, to_iso: str) -> str:
    conn = store.db.conn
    events = events_since(store, 0, limit=100000)
    window_events = [
        event for event in events if from_iso <= event.occurred_at <= to_iso
    ]

    done: list[str] = []
    passing: list[str] = []
    revising: list[str] = []
    blocked: list[str] = []
    failed: list[str] = []
    for event in window_events:
        task_id = str(event.payload.get("task_id") or "")
        if event.aggregate_type == "task":
            task_id = event.aggregate_id
        if event.event_type == "EVIDENCE_SEALED":
            done.append(task_id)
        elif event.event_type == "TASK_PASS" and task_id not in passing:
            passing.append(task_id)
        elif event.event_type == "TASK_REVISE" and task_id not in revising:
            revising.append(task_id)
        elif event.event_type == "TASK_BLOCKED" and task_id not in blocked:
            blocked.append(task_id)
        elif event.event_type == "TASK_FAILED" and task_id not in failed:
            failed.append(task_id)

    rows = conn.execute(
        "SELECT id, title, state, current_attempt FROM tasks ORDER BY priority DESC"
    ).fetchall()
    executing = [
        str(row["id"])
        for row in rows
        if str(row["state"]) in {TaskState.WORKING.value, TaskState.REVIEW_PENDING.value}
    ]

    usage = conn.execute(
        "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS total FROM usage"
        " WHERE recorded_at >= ? AND recorded_at <= ?",
        (from_iso, to_iso),
    ).fetchone()
    total_tokens = int(usage["total"])

    lines = [
        "# BoxPorter 活动报告\n",
        f"统计窗口：{from_iso} ~ {to_iso}\n",
        "## 已通过",
        *([f"- {item}" for item in sorted(set(done + passing))] or ["- （无）"]),
        "",
        "## 执行中",
        *([f"- {item}" for item in executing] or ["- （无）"]),
        "",
        "## 返修中",
        *([f"- {item}" for item in sorted(set(revising))] or ["- （无）"]),
        "",
        "## 阻塞",
        *([f"- {item}" for item in sorted(set(blocked))] or ["- （无）"]),
        "",
        "## 失败",
        *([f"- {item}" for item in sorted(set(failed))] or ["- （无）"]),
        "",
        "## 资源",
        f"- 窗口内 Token 消耗：{total_tokens}",
        "",
    ]
    return "\n".join(lines)
