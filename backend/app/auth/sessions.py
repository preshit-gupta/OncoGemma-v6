"""Server-side sign-in sessions (SPEC-03 §3.3).

The ``og_session`` cookie holds an HS256 JWT ``{sid, uid, role, iat, exp}``. The token only names
the session: every request checks the ``sessions`` row and the user's current status, so logout,
a role change or a disabled account take effect at once on this instance and, through the cache
TTL (configs/auth.yaml ``session.cache_ttl_s``), within that many seconds on every other one. The
role a request acts with is always the user's role in the database, never the token's claim.
"""
import os
import secrets
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic

import jwt
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.errors import AuthConfigError, session_expired
from app.core.pipeline_config import get_pipeline_config
from app.models.user import AuthSession, User

SESSION_COOKIE = "og_session"
CSRF_COOKIE = "og_csrf"
CSRF_HEADER = "X-CSRF-Token"
SIGNING_KEY_ENV = "SESSION_SIGNING_KEY"
JWT_ALGORITHM = "HS256"
MIN_SIGNING_KEY_BYTES = 32  # HS256 needs a 256-bit key
CSRF_TOKEN_BYTES = 32
SECONDS_PER_MINUTE = 60


def signing_key() -> bytes:
    """The session signing key, from the environment only (Secret Manager ``og-session-signing-key``)."""
    key = os.environ.get(SIGNING_KEY_ENV, "")
    if not key:
        raise AuthConfigError(
            f"{SIGNING_KEY_ENV} is not set. Deploy with --set-secrets={SIGNING_KEY_ENV}=og-session-signing-key:latest."
        )
    if key != key.strip():
        raise AuthConfigError(
            f"{SIGNING_KEY_ENV} has leading or trailing whitespace, usually a newline stored in the secret."
        )
    if len(key.encode("utf-8")) < MIN_SIGNING_KEY_BYTES:
        raise AuthConfigError(f"{SIGNING_KEY_ENV} must be at least {MIN_SIGNING_KEY_BYTES} bytes")
    return key.encode("utf-8")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def session_max_age_s() -> int:
    """Cookie lifetime: the session's absolute lifetime."""
    return get_pipeline_config().auth.session.absolute_lifetime_min * SECONDS_PER_MINUTE


def utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes for timezone-aware columns; every stored time is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class SessionUser:
    """What one session lookup established: the session's bounds and its user's current state."""

    session_id: str
    user_id: str
    email: str
    display_name: str | None
    role: str
    expires_at: datetime
    last_seen_at: datetime


class _SessionCache:
    """A bounded, thread-safe LRU of session lookups, each trusted for ``cache_ttl_s``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, SessionUser]] = OrderedDict()

    def get(self, session_id: str, ttl_s: float) -> SessionUser | None:
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            fetched_at, found = entry
            if monotonic() - fetched_at > ttl_s:
                del self._entries[session_id]
                return None
            self._entries.move_to_end(session_id)
            return found

    def put(self, found: SessionUser, max_entries: int) -> None:
        with self._lock:
            self._entries[found.session_id] = (monotonic(), found)
            self._entries.move_to_end(found.session_id)
            while len(self._entries) > max_entries:
                self._entries.popitem(last=False)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def drop_user(self, user_id: str) -> None:
        with self._lock:
            for session_id in [s for s, (_, found) in self._entries.items() if found.user_id == user_id]:
                del self._entries[session_id]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


session_cache = _SessionCache()


def create_session(
    db: Session, user: User, *, ip: str | None, user_agent: str | None, now: datetime
) -> tuple[AuthSession, str]:
    """Add a session row for ``user`` and return it with its signed token. The caller commits."""
    lifetime = timedelta(minutes=get_pipeline_config().auth.session.absolute_lifetime_min)
    row = AuthSession(
        id=uuid.uuid4(), user_id=user.id, created_at=now, expires_at=now + lifetime, last_seen_at=now,
        ip=ip, user_agent=user_agent,
    )
    db.add(row)
    claims = {
        "sid": str(row.id),
        "uid": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int(row.expires_at.timestamp()),
    }
    return row, jwt.encode(claims, signing_key(), algorithm=JWT_ALGORITHM)


def decode_session_token(token: str) -> dict:
    try:
        return jwt.decode(
            token, signing_key(), algorithms=[JWT_ALGORITHM], options={"require": ["sid", "uid", "iat", "exp"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise session_expired("token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise session_expired("bad_token") from exc


def _load(db: Session, session_id: str, now: datetime) -> SessionUser:
    """Look the session and its user up, refuse it if it is no longer valid, and record the activity."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise session_expired("bad_session_id") from exc
    row = db.get(AuthSession, sid)
    if row is None:
        raise session_expired("unknown_session")
    if row.revoked_at is not None:
        raise session_expired("revoked")
    if utc(row.expires_at) <= now:
        raise session_expired("expired")
    idle = timedelta(minutes=get_pipeline_config().auth.session.idle_timeout_min)
    if now - utc(row.last_seen_at) > idle:
        raise session_expired("idle")
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise session_expired("user_not_active")
    row.last_seen_at = now
    db.commit()
    return SessionUser(
        session_id=str(row.id), user_id=str(user.id), email=user.email, display_name=user.display_name,
        role=user.role, expires_at=utc(row.expires_at), last_seen_at=now,
    )


def resolve_session(db: Session, token: str, now: datetime) -> SessionUser:
    """The session a cookie names, if it is still valid; otherwise ``401 session_expired``."""
    claims = decode_session_token(token)
    settings = get_pipeline_config().auth.session
    found = session_cache.get(claims["sid"], settings.cache_ttl_s)
    if found is None:
        found = _load(db, claims["sid"], now)
        session_cache.put(found, settings.cache_max_entries)
    if found.user_id != claims["uid"]:
        raise session_expired("user_mismatch")
    if found.expires_at <= now:
        raise session_expired("expired")
    return found


def revoke_session(db: Session, session_id: str, now: datetime) -> None:
    """Revoke one session. The caller commits."""
    session_cache.drop(session_id)
    db.execute(
        update(AuthSession)
        .where(AuthSession.id == uuid.UUID(session_id), AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_user_sessions(db: Session, user_id: uuid.UUID | str, now: datetime) -> None:
    """Revoke every open session of a user (logout everywhere, role change, disable). The caller commits."""
    session_cache.drop_user(str(user_id))
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == uuid.UUID(str(user_id)), AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
