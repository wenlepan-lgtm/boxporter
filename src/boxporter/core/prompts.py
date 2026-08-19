"""Role prompt templates with versioning (plan §7.3).

Prompts live in the settings table, are versioned per role, and the
sha256 of the active prompt is recorded on every run for traceability.
"""

from __future__ import annotations

import hashlib

from boxporter.core.schemas import canonical_json
from boxporter.storage.store import Store

ROLES = ("executor", "reviewer", "planner", "supervisor")

DEFAULT_PROMPTS: dict[str, dict[str, object]] = {
    "executor": {
        "version": 1,
        "content": (
            "You are the EXECUTOR for this task. Work inside the provided"
            " workspace. Implement the objective, run verification commands"
            " yourself, and when done write three files under the reports"
            " directory: result.md (compact conclusions), verify.md (real"
            " commands with exit codes), executor.md (facts and remaining"
            " risks). You cannot mark your own work as PASS."
        ),
    },
    "reviewer": {
        "version": 1,
        "content": (
            "You are the independent REVIEWER. The submission is frozen:"
            " you may read code and run tests, but must not modify the"
            " submission. Write review.md and review_evidence.json with"
            " test_exit_code and production_risk, then conclude exactly"
            " PASS, REVISE or BLOCKED per acceptance criteria."
        ),
    },
    "planner": {
        "version": 1,
        "content": (
            "You are the PLANNER. Decompose the goal into small verifiable"
            " tasks with dependencies, risk levels and acceptance criteria."
            " You do not execute code and do not judge PASS."
        ),
    },
    "supervisor": {
        "version": 1,
        "content": (
            "You are the SUPERVISOR. Only for ambiguous diagnostics: read"
            " machine state (events, leases, budgets) and propose a safe,"
            " verifiable next action. Never bypass permissions or budgets."
        ),
    },
}


class PromptService:
    def __init__(self, store: Store):
        self.store = store

    def get(self, role: str) -> tuple[int, str]:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        raw = self.store.settings.get(self.store.db.conn, "prompts")
        if isinstance(raw, dict) and isinstance(raw.get(role), dict):
            entry = raw[role]
            version_raw = entry.get("version", 1)
            version = int(version_raw) if isinstance(version_raw, (int, str)) else 1
            return version, str(entry.get("content", ""))
        default = DEFAULT_PROMPTS[role]
        return int(str(default["version"])), str(default["content"])

    def set(self, role: str, content: str, version: int | None = None) -> int:
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        current_version, _ = self.get(role)
        next_version = version if version is not None else current_version + 1
        raw = self.store.settings.get(self.store.db.conn, "prompts")
        prompts = dict(raw) if isinstance(raw, dict) else {}
        prompts[role] = {"version": next_version, "content": content}
        with self.store.db.transaction():
            self.store.settings.set(self.store.db.conn, "prompts", prompts)
        return next_version

    def sha(self, role: str) -> str:
        version, content = self.get(role)
        payload = {"role": role, "version": version, "content": content}
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def render(self, role: str, context_pack: str) -> str:
        _, content = self.get(role)
        return f"{content}\n\n{context_pack}".strip()
