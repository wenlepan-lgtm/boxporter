"""Token/cost budgets (plan §11.4): task budgets, daily caps, overrun
detection. All checks are deterministic SQL reads — zero model calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from boxporter.core.clock import now_iso
from boxporter.core.ids import new_id
from boxporter.core.schemas import Task
from boxporter.storage.metering import UsageRecord
from boxporter.storage.store import Store


@dataclass(frozen=True)
class BudgetCheck:
    allowed: bool
    reason: str
    used: int = 0
    limit: int = 0


class BudgetService:
    def __init__(self, store: Store):
        self.store = store

    def record_usage(
        self,
        run_id: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float = 0.0,
        tool_calls: int = 0,
    ) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.usage.insert(
                conn,
                UsageRecord(
                    id=new_id("use"),
                    run_id=run_id,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost=cost,
                    tool_calls=tool_calls,
                    recorded_at=now_iso(),
                ),
            )

    def can_start_run(self, task: Task, daily_token_budget: int) -> BudgetCheck:
        """Both the task token budget and the daily cap must allow starting
        a new run."""
        conn = self.store.db.conn
        task_used = self.store.usage.total_tokens_for_task(conn, task.id)
        if task_used >= task.spec.token_budget:
            return BudgetCheck(
                False, "task-token-budget-exhausted", task_used, task.spec.token_budget
            )
        day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        daily_used = self.store.usage.total_tokens_since(conn, day_start)
        if daily_used >= daily_token_budget:
            return BudgetCheck(
                False, "daily-token-budget-exhausted", daily_used, daily_token_budget
            )
        return BudgetCheck(True, "ok", daily_used, daily_token_budget)

    def task_over_budget(self, task: Task) -> BudgetCheck:
        conn = self.store.db.conn
        used = self.store.usage.total_tokens_for_task(conn, task.id)
        over = used >= task.spec.token_budget
        return BudgetCheck(
            not over,
            "task-token-budget-exhausted" if over else "ok",
            used,
            task.spec.token_budget,
        )

    def daily_usage(self) -> tuple[int, int]:
        """(used, limit) for today."""
        conn = self.store.db.conn
        day_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        used = self.store.usage.total_tokens_since(conn, day_start)
        raw = self.store.settings.get(conn, "policy")
        limit = int(raw.get("daily_token_budget", 2000000)) if isinstance(raw, dict) else 2000000
        return used, limit
