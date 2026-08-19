"""Recovery engine: stop-reason classification and bounded recovery decisions.

Deterministic first (ADR-002): map watchdog findings and adapter
observations to recovery actions. A supervisor model is only a future
extension for ambiguous diagnostics, never part of the hot path.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from boxporter.core.clock import parse_iso_utc
from boxporter.core.watchdog import FindingKind, WatchFinding
from boxporter.storage.store import Store


class RecoveryAction(str, Enum):
    NONE = "NONE"
    RETRY_LATER = "RETRY_LATER"
    NOTIFY = "NOTIFY"
    FAIL_RUN = "FAIL_RUN"
    BEGIN_NEXT_ATTEMPT = "BEGIN_NEXT_ATTEMPT"
    BLOCK_TASK = "BLOCK_TASK"
    STOP_AND_NOTIFY = "STOP_AND_NOTIFY"


def error_fingerprint(reason: str) -> str:
    """Stable failure fingerprint: identical root causes map to the same
    fingerprint so repeated identical retries can be detected (plan §9.3)."""
    normalized = re.sub(r"\s+", " ", reason.strip().lower())
    normalized = re.sub(r"[^a-z0-9_ \-.]", "", normalized)
    if len(normalized) > 120:
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        return f"{normalized[:100]}~{digest}"
    return normalized or "unknown"


def backoff_seconds(
    recovery_count: int,
    *,
    base: float = 60.0,
    cap: float = 3600.0,
    jitter: float = 0.2,
) -> float:
    """Exponential backoff with deterministic jitter (plan §9.3)."""
    exponent = max(recovery_count - 1, 0)
    delay: float = min(base * (2.0 ** exponent), cap)
    wave = ((recovery_count * 2654435761) % 100) - 50  # -50..49
    return delay * (1.0 + jitter * wave / 50.0)


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    detail: dict[str, object]


@dataclass(frozen=True)
class RecoveryPolicy:
    max_recoveries_per_attempt: int = 2
    max_repeated_error_fingerprints: int = 3


class RecoveryEngine:
    """Classifies findings and returns the next safe action."""

    def __init__(self, store: Store, policy: RecoveryPolicy | None = None):
        self.store = store
        self.policy = policy or RecoveryPolicy()

    def decide(self, finding: WatchFinding) -> RecoveryDecision:
        conn = self.store.db.conn
        run = self.store.runs.get(conn, finding.run_id)
        attempt = self.store.attempts.get(conn, run.attempt_id)
        task = self.store.tasks.get(conn, attempt.task_id)

        if finding.kind in {FindingKind.LEASE_MISSING, FindingKind.LEASE_EXPIRED}:
            # The run no longer holds valid execution rights: crash it.
            # Recovery (same attempt / new attempt) is decided afterwards
            # from budgets, never as an immediate blind restart.
            return RecoveryDecision(
                RecoveryAction.FAIL_RUN,
                reason=finding.kind.lower(),
                detail={"task_id": task.id, "attempt": attempt.number},
            )
        if finding.kind == FindingKind.PROCESS_DEAD:
            return RecoveryDecision(
                RecoveryAction.FAIL_RUN,
                reason="process-dead",
                detail={"task_id": task.id, "attempt": attempt.number},
            )
        if finding.kind == FindingKind.STALE_HEARTBEAT:
            return RecoveryDecision(
                RecoveryAction.NOTIFY,
                reason="stale-heartbeat",
                detail={"task_id": task.id},
            )
        if finding.kind == FindingKind.NO_PROGRESS:
            return RecoveryDecision(
                RecoveryAction.NOTIFY,
                reason="no-progress",
                detail={"task_id": task.id},
            )
        return RecoveryDecision(RecoveryAction.NONE, reason="unclassified", detail={})

    def after_failure(
        self,
        task_id: str,
        max_recoveries: int | None = None,
        now: datetime | None = None,
    ) -> RecoveryDecision:
        """Decide whether a FAILED task may retry within budgets.

        Checks in order: max attempts → budget exhaustion → recovery budget
        → repeated error fingerprint → backoff window. Deterministic; the
        backoff check consumes no model calls."""
        conn = self.store.db.conn
        current = now or datetime.now(timezone.utc)
        task = self.store.tasks.get(conn, task_id)
        if task.current_attempt >= task.max_attempts:
            return RecoveryDecision(
                RecoveryAction.STOP_AND_NOTIFY,
                reason="max-attempts-reached",
                detail={"attempt": task.current_attempt, "max": task.max_attempts},
            )
        # Budget exhaustion is not retryable by automation: only a human can
        # raise the budget, so retrying would burn the same wall twice.
        budget_row = conn.execute(
            "SELECT 1 FROM runs r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.task_id = ? AND r.stop_reason LIKE '%budget%'",
            (task.id,),
        ).fetchone()
        if budget_row is not None:
            return RecoveryDecision(
                RecoveryAction.STOP_AND_NOTIFY,
                reason="budget-exhausted",
                detail={"task_id": task.id},
            )
        budget = (
            self.policy.max_recoveries_per_attempt
            if max_recoveries is None
            else max_recoveries
        )
        # Task-level cumulative recovery budget: every automatic recovery
        # (FailRun) across all attempts counts against the same cap.
        row = conn.execute(
            "SELECT COALESCE(SUM(recovery_count), 0) AS total FROM attempts"
            " WHERE task_id = ?",
            (task.id,),
        ).fetchone()
        total = int(row["total"])
        if total >= budget:
            return RecoveryDecision(
                RecoveryAction.STOP_AND_NOTIFY,
                reason="recovery-budget-exhausted",
                detail={"total_recoveries": total, "budget": budget},
            )
        # Repeated identical failure fingerprints stop blind retries:
        # a retry must carry a new hypothesis or a changed environment.
        attempt = self.store.attempts.get_by_task_number(conn, task.id, task.current_attempt)
        if attempt.error_fingerprint:
            rows = conn.execute(
                "SELECT error_fingerprint FROM attempts WHERE task_id = ? AND"
                " error_fingerprint IS NOT NULL ORDER BY number DESC LIMIT ?",
                (task.id, self.policy.max_repeated_error_fingerprints),
            ).fetchall()
            fingerprints = [str(item["error_fingerprint"]) for item in rows]
            if (
                len(fingerprints) >= self.policy.max_repeated_error_fingerprints
                and len(set(fingerprints)) == 1
            ):
                return RecoveryDecision(
                    RecoveryAction.STOP_AND_NOTIFY,
                    reason="repeated-error-fingerprint",
                    detail={"fingerprint": fingerprints[0]},
                )
        # Exponential backoff with jitter: before next_retry_at, zero work.
        if attempt.next_retry_at is not None and parse_iso_utc(
            attempt.next_retry_at
        ) > current:
            return RecoveryDecision(
                RecoveryAction.RETRY_LATER,
                reason="backoff",
                detail={"next_retry_at": attempt.next_retry_at},
            )
        return RecoveryDecision(
            RecoveryAction.BEGIN_NEXT_ATTEMPT,
            reason="within-recovery-budget",
            detail={"attempt": task.current_attempt, "max": task.max_attempts},
        )
