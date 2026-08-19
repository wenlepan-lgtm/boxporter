"""Deduplicated notifications for blocks, budgets and recovery stops."""

from __future__ import annotations

from boxporter.storage.store import Store


class Notifier:
    def __init__(self, store: Store):
        self.store = store

    def block(self, task_id: str, reason: str) -> None:
        with self.store.db.transaction():
            self.store.notifications.create(
                self.store.db.conn,
                kind="block",
                dedup_key=f"block:{task_id}",
                payload={"task_id": task_id, "reason": reason},
            )

    def budget(self, task_id: str, used: int, limit: int) -> None:
        with self.store.db.transaction():
            self.store.notifications.create(
                self.store.db.conn,
                kind="budget",
                dedup_key=f"budget:{task_id}",
                payload={"task_id": task_id, "used": used, "limit": limit},
            )

    def daily_budget(self, used: int, limit: int) -> None:
        with self.store.db.transaction():
            self.store.notifications.create(
                self.store.db.conn,
                kind="daily-budget",
                dedup_key=f"daily-budget:{used // max(limit, 1)}",
                payload={"used": used, "limit": limit},
            )

    def recovery_stop(self, task_id: str, reason: str) -> None:
        with self.store.db.transaction():
            self.store.notifications.create(
                self.store.db.conn,
                kind="recovery-stop",
                dedup_key=f"recovery-stop:{task_id}",
                payload={"task_id": task_id, "reason": reason},
            )
