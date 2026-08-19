"""24x7 operating policy (ADR-009): modes, risk admission, budgets.

The policy lives in the settings table (written by the Web console) and
is re-read by the daemon on every tick, so remote mode changes apply
without restarting the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from boxporter.storage.store import Store

DEFAULT_POLICY: dict[str, object] = {
    "mode": "SUPERVISED",
    "allowed_risk_levels": ["low", "medium"],
    "max_concurrent": 1,
    "auto_review": True,
    "daily_token_budget": 2000000,
    "max_recoveries_per_attempt": 2,
    "notify_on_block": True,
    "notify_on_budget": True,
}

MODES = frozenset({"SUPERVISED", "AWAY", "PAUSED"})


@dataclass(frozen=True)
class PolicySnapshot:
    mode: str
    allowed_risk_levels: frozenset[str]
    max_concurrent: int
    auto_review: bool
    daily_token_budget: int
    max_recoveries_per_attempt: int
    notify_on_block: bool
    notify_on_budget: bool
    raw: dict[str, object] = field(default_factory=dict)


class PolicyService:
    def __init__(self, store: Store):
        self.store = store

    def read(self) -> PolicySnapshot:
        raw = self.store.settings.get(self.store.db.conn, "policy")
        merged = dict(DEFAULT_POLICY)
        if isinstance(raw, dict):
            merged.update({str(k): v for k, v in raw.items()})

        def as_int(key: str, fallback: int) -> int:
            value = merged[key]
            return int(value) if isinstance(value, (int, str)) else fallback

        mode = str(merged["mode"])
        if mode not in MODES:
            mode = "SUPERVISED"
        risk_raw = merged["allowed_risk_levels"]
        levels = risk_raw if isinstance(risk_raw, (list, tuple, set, frozenset)) else []
        return PolicySnapshot(
            mode=mode,
            allowed_risk_levels=frozenset(str(item) for item in levels),
            max_concurrent=as_int("max_concurrent", 1),
            auto_review=bool(merged["auto_review"]),
            daily_token_budget=as_int("daily_token_budget", 2000000),
            max_recoveries_per_attempt=as_int("max_recoveries_per_attempt", 2),
            notify_on_block=bool(merged["notify_on_block"]),
            notify_on_budget=bool(merged["notify_on_budget"]),
            raw=merged,
        )

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        raw = dict(self.read().raw)
        raw["mode"] = mode
        with self.store.db.transaction():
            self.store.settings.set(self.store.db.conn, "policy", raw)

    def write(self, snapshot: dict[str, object]) -> None:
        mode = str(snapshot.get("mode", "SUPERVISED"))
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        merged = dict(DEFAULT_POLICY)
        merged.update(snapshot)
        with self.store.db.transaction():
            self.store.settings.set(self.store.db.conn, "policy", merged)
