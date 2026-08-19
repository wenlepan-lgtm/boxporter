"""Read-side queries: four-box projection, task detail, event cursor.

All reads go through the store; UI never infers state from local data
(ADR-002, ADR-013).
"""

from __future__ import annotations

from dataclasses import dataclass

from boxporter.core.boxes import Box, box_for
from boxporter.core.schemas import Attempt, EventRecord, Run, Task
from boxporter.storage.store import Store


@dataclass(frozen=True)
class BoxTaskSummary:
    task_id: str
    title: str
    state: str
    priority: int
    risk_level: str
    current_attempt: int


@dataclass(frozen=True)
class TaskDetail:
    task: Task
    attempts: tuple[Attempt, ...]
    runs: tuple[Run, ...]
    events: tuple[EventRecord, ...]


def project_boxes(store: Store, project_id: str) -> dict[Box, list[BoxTaskSummary]]:
    conn = store.db.conn
    tasks = store.tasks.list_by_project(conn, project_id)
    result: dict[Box, list[BoxTaskSummary]] = {box: [] for box in Box}
    for task in tasks:
        result[box_for(task.state)].append(
            BoxTaskSummary(
                task_id=task.id,
                title=task.title,
                state=task.state.value,
                priority=task.priority,
                risk_level=task.risk_level,
                current_attempt=task.current_attempt,
            )
        )
    return result


def task_detail(store: Store, task_id: str) -> TaskDetail:
    conn = store.db.conn
    task = store.tasks.get(conn, task_id)
    attempts: list[Attempt] = []
    if task.current_attempt:
        for number in range(1, task.current_attempt + 1):
            attempts.append(store.attempts.get_by_task_number(conn, task_id, number))
    runs = store.runs.list_for_task(conn, task_id)
    events = store.events.for_aggregate(conn, "task", task_id)
    return TaskDetail(
        task=task,
        attempts=tuple(attempts),
        runs=tuple(runs),
        events=tuple(events),
    )


def events_since(store: Store, after_seq: int, limit: int = 500) -> list[EventRecord]:
    return store.events.since(store.db.conn, after_seq, limit)


def latest_seq(store: Store) -> int:
    row = store.db.conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM events").fetchone()
    return int(row["seq"])
