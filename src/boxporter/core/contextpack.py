"""Task Context Pack (ADR-007): compact, verifiable handoff payloads.

A Context Pack carries only what the next role needs: objective,
acceptance criteria, constraints, verified facts, prior review feedback
and failed approaches — never full chat history.
"""

from __future__ import annotations

import json
from typing import Any

from boxporter.core.schemas import Task, canonical_json
from boxporter.core.state import AttemptState
from boxporter.storage.store import Store

CONTEXT_SCHEMA = "BOXPORTER_CONTEXT_V1"

DEFAULT_FORBIDDEN_ACTIONS = [
    "push or deploy to production",
    "delete large data ranges",
    "send external messages or place paid orders",
    "change router, firewall or system security settings",
    "use unauthorized sudo",
    "expand token or cost budgets",
    "bypass tests, review or evidence gates",
    "overwrite modifications on a dirty worktree",
]


def build_context_pack(store: Store, task: Task, role: str) -> str:
    """Build the role-specific Context Pack for the current attempt."""
    conn = store.db.conn
    attempts = store.attempts.list_for_task(conn, task.id)
    prior_feedback: list[dict[str, Any]] = []
    failed_fingerprints: list[str] = []
    for attempt in attempts:
        if attempt.state == AttemptState.REVISED and attempt.number < task.current_attempt:
            submission = store.submissions.get_for_attempt(conn, attempt.id)
            reviews = (
                store.reviews.get_for_submission(conn, submission.id)
                if submission is not None
                else []
            )
            for review in reviews:
                prior_feedback.append(
                    {
                        "attempt": attempt.number,
                        "result": review.result,
                        "report_ref": review.report_ref,
                    }
                )
        if attempt.error_fingerprint:
            failed_fingerprints.append(attempt.error_fingerprint)

    memory_rows = store.memory.list_for_project(conn, task.project_id)
    project_facts = [
        {"kind": item.kind, "content": item.content, "source": item.source}
        for item in memory_rows
    ]

    pack: dict[str, Any] = {
        "schema": CONTEXT_SCHEMA,
        "task_ref": f"task://{task.id}",
        "role": role,
        "objective": task.objective,
        "acceptance_criteria": list(task.spec.acceptance_criteria),
        "constraints": list(task.spec.constraints),
        "workspace": {
            "path": task.spec.workspace,
            "base_commit": task.spec.base_commit,
        },
        "project_facts": project_facts,
        "prior_review_feedback": prior_feedback,
        "failed_approaches": sorted(set(failed_fingerprints)),
        "forbidden_actions": list(DEFAULT_FORBIDDEN_ACTIONS),
        "budget": {
            "token_budget": task.spec.token_budget,
            "timeout_seconds": task.spec.timeout_seconds,
            "max_attempts": task.spec.max_attempts,
            "current_attempt": task.current_attempt,
        },
    }
    return json.dumps(pack, ensure_ascii=False, indent=2)


def context_pack_sha256(pack: str) -> str:
    import hashlib

    value = json.loads(pack)
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
