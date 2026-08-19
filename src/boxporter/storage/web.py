"""Settings and device-session repositories for the Web console."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from boxporter.core.clock import now_iso
from boxporter.core.errors import NotFoundError


class SettingsRepo:
    def get(self, conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(str(row["value_json"]))

    def set(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
            " updated_at = excluded.updated_at",
            (
                key,
                json.dumps(value, ensure_ascii=True, separators=(",", ":")),
                now_iso(),
            ),
        )


@dataclass(frozen=True)
class WebSession:
    id: str
    device_label: str
    created_at: str
    last_seen_at: str
    expires_at: str
    reauth_until: str | None
    revoked: bool


class WebSessionsRepo:
    def insert(self, conn: sqlite3.Connection, session: WebSession) -> None:
        conn.execute(
            "INSERT INTO web_sessions (id, device_label, created_at, last_seen_at,"
            " expires_at, reauth_until, revoked) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.id,
                session.device_label,
                session.created_at,
                session.last_seen_at,
                session.expires_at,
                session.reauth_until,
                1 if session.revoked else 0,
            ),
        )

    def get(self, conn: sqlite3.Connection, session_id: str) -> WebSession:
        row = conn.execute(
            "SELECT * FROM web_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"web session not found: {session_id}")
        return self._row_to_session(row)

    def list_active(self, conn: sqlite3.Connection) -> list[WebSession]:
        rows = conn.execute(
            "SELECT * FROM web_sessions WHERE revoked = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def touch(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            "UPDATE web_sessions SET last_seen_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )

    def revoke(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            "UPDATE web_sessions SET revoked = 1 WHERE id = ?", (session_id,)
        )

    def set_reauth(self, conn: sqlite3.Connection, session_id: str, until: str) -> None:
        conn.execute(
            "UPDATE web_sessions SET reauth_until = ? WHERE id = ?",
            (until, session_id),
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> WebSession:
        return WebSession(
            id=str(row["id"]),
            device_label=str(row["device_label"]),
            created_at=str(row["created_at"]),
            last_seen_at=str(row["last_seen_at"]),
            expires_at=str(row["expires_at"]),
            reauth_until=row["reauth_until"],
            revoked=bool(row["revoked"]),
        )
