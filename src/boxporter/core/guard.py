"""Execution guard: high-risk actions must consume a scoped approval
before they may execute (fix-guide P1-E, plan §19.3).

Approvals are bound to the exact action string, a task, max uses and an
expiry; the guard consumes one use per execution attempt. A rejected or
missing approval blocks the action and records an audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from boxporter.core.clock import now_iso, parse_iso_utc
from boxporter.storage.events import ActorType, EventType
from boxporter.storage.store import Store

HIGH_RISK_ACTIONS = frozenset(
    {
        "external-write",
        "system-admin",
        "production",
        "sudo",
        "execute-high-risk-task",
    }
)


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str
    approval_id: str | None = None


class RunnerExecutionGuard:
    def __init__(self, store: Store):
        self.store = store

    def consume(
        self,
        *,
        task_id: str,
        action: str,
        actor: str = ActorType.DAEMON,
        now: datetime | None = None,
    ) -> GuardResult:
        conn = self.store.db.conn
        current = now or datetime.now(timezone.utc)
        candidates = self.store.approvals.list_for_task(conn, task_id)
        for approval in candidates:
            if approval.status != "approved" or approval.action != action:
                continue
            if parse_iso_utc(approval.expires_at) <= current:
                continue
            if approval.used_count >= approval.max_uses:
                continue
            consumed = self.store.approvals.consume(
                conn, approval.id, by=actor, at=now_iso()
            )
            if consumed:
                self._event(
                    EventType.APPROVAL_CONSUMED,
                    task_id=task_id,
                    payload={
                        "approval_id": approval.id,
                        "action": action,
                        "target": approval.target,
                        "allowed": True,
                    },
                )
                return GuardResult(
                    True, "approval consumed", approval_id=approval.id
                )
        self._event(
            EventType.APPROVAL_REJECTED,
            task_id=task_id,
            payload={"action": action, "allowed": False},
        )
        return GuardResult(
            False,
            f"no valid approval for action {action!r} on task {task_id}",
        )

    def _event(self, event_type: str, task_id: str, payload: dict[str, object]) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="task",
                aggregate_id=task_id,
                event_type=event_type,
                actor_type=ActorType.DAEMON,
                payload=payload,
            )
