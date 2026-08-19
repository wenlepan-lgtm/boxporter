"""Web console authentication: password hash, device sessions, reauth
(ADR-012). Cookies are signed (HMAC-SHA256); state-changing requests must
send a custom header for CSRF hardening. High-risk operations require a
fresh re-authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Cookie, HTTPException, Request, status

from boxporter.core.clock import now_iso, parse_iso_utc
from boxporter.core.errors import NotFoundError
from boxporter.core.ids import new_id
from boxporter.storage.store import Store
from boxporter.storage.web import WebSession

SESSION_COOKIE = "boxporter_session"
REAUTH_WINDOW_SECONDS = 600
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1}
CLIENT_HEADER = "X-BoxPorter-Client"


def hash_password(password: str) -> dict[str, object]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_PARAMS["n"], r=SCRYPT_PARAMS["r"],
        p=SCRYPT_PARAMS["p"], dklen=32,
    )
    return {
        "algo": "scrypt",
        "salt": salt.hex(),
        "n": SCRYPT_PARAMS["n"],
        "r": SCRYPT_PARAMS["r"],
        "p": SCRYPT_PARAMS["p"],
        "digest": digest.hex(),
    }


def verify_password(password: str, stored: dict[str, object]) -> bool:
    try:
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(str(stored["salt"])),
            n=int(str(stored["n"])),
            r=int(str(stored["r"])),
            p=int(str(stored["p"])),
            dklen=32,
        )
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), str(stored["digest"]))


@dataclass(frozen=True)
class AuthContext:
    session: WebSession


class AuthManager:
    def __init__(self, store: Store):
        self.store = store

    def ensure_secret(self) -> str:
        secret = self.store.settings.get(self.store.db.conn, "web_secret")
        if not secret:
            secret = secrets.token_hex(32)
            with self.store.db.transaction():
                self.store.settings.set(self.store.db.conn, "web_secret", secret)
        return str(secret)

    def cookie_value(self, session_id: str) -> str:
        signature = hmac.new(
            self.ensure_secret().encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        return f"{session_id}.{signature}"

    def parse_cookie(self, value: str | None) -> str | None:
        if value is None or "." not in value:
            return None
        session_id, signature = value.split(".", 1)
        expected = hmac.new(
            self.ensure_secret().encode(), session_id.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        return session_id

    def login(self, password: str, device_label: str) -> str:
        stored = self.store.settings.get(self.store.db.conn, "admin_password_hash")
        if not isinstance(stored, dict):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="password not initialized: run boxporter-v2 web-set-password",
            )
        if not verify_password(password, stored):
            self._audit("AUTH_FAILED", detail="wrong password")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong password")
        session_id = new_id("ws")
        now = now_iso()
        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        session = WebSession(
            id=session_id,
            device_label=device_label,
            created_at=now,
            last_seen_at=now,
            expires_at=expires,
            reauth_until=None,
            revoked=False,
        )
        with self.store.db.transaction():
            self.store.web_sessions.insert(self.store.db.conn, session)
        self._audit("AUTH_LOGIN", device_label=device_label, session_id=session_id)
        return session_id

    def logout(self, session_id: str) -> None:
        try:
            with self.store.db.transaction():
                self.store.web_sessions.revoke(self.store.db.conn, session_id)
        except NotFoundError:
            pass

    def reauthenticate(self, session_id: str, password: str) -> None:
        stored = self.store.settings.get(self.store.db.conn, "admin_password_hash")
        if not isinstance(stored, dict) or not verify_password(password, stored):
            self._audit("AUTH_REAUTH_FAILED", session_id=session_id)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong password")
        until = (
            datetime.now(timezone.utc) + timedelta(seconds=REAUTH_WINDOW_SECONDS)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.store.db.transaction():
            self.store.web_sessions.set_reauth(self.store.db.conn, session_id, until)
        self._audit("AUTH_REAUTH", session_id=session_id)

    def require_session(
        self,
        request: Request,
        boxporter_session: Annotated[str | None, Cookie()] = None,
    ) -> AuthContext:
        return self.resolve_session(request, boxporter_session)

    def resolve_session(self, request: Request, cookie_value: str | None) -> AuthContext:
        """Session resolution usable outside FastAPI dependency injection
        (e.g. SSE endpoints that must parse the cookie manually)."""
        session_id = self.parse_cookie(cookie_value)
        if session_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
        try:
            session = self.store.web_sessions.get(self.store.db.conn, session_id)
        except NotFoundError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session not found")
        if session.revoked or parse_iso_utc(session.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session expired")
        with self.store.db.transaction():
            self.store.web_sessions.touch(self.store.db.conn, session_id)
        request.state.auth = AuthContext(session=session)
        return AuthContext(session=session)

    def require_reauth(self, context: AuthContext) -> None:
        if context.session.reauth_until is None or parse_iso_utc(
            context.session.reauth_until
        ) <= datetime.now(timezone.utc):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="reauthentication required for high-risk operations",
                headers={"X-BoxPorter-Reauth": "required"},
            )

    def _audit(self, event: str, **payload: object) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="web",
                aggregate_id="console",
                event_type=event,
                actor_type="user",
                payload=payload,
            )
