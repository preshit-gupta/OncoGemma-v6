"""Idempotency-Key header validation, persistence, and replay (SPEC-03 §5.3.3).

Confirm and approve stage transition calls require an Idempotency-Key header.
Keys are scoped to the calling user, bound to the request path and body, and retained
for ``safety.idempotency.ttl_hours``.

A route opts in by declaring ``_idempotency: IdempotencyContext = idempotent("<name>")``
after its ``require(...)`` user parameter (so authorization runs first), on a router built
with ``route_class=IdempotentRoute``. The route body itself does not touch the key:

- the dependency claims the key, or replays a completed response (``IdempotentReplay``),
  or rejects the key reused with a different route/body (422) or while in flight (409);
- ``IdempotentRoute`` stores a 2xx response for replay and releases the claim on any
  error, so a failed call can be retried with the same key.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine

from fastapi import Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth.deps import CurrentUser, current_user
from app.core.db import get_db
from app.core.pipeline_config import get_pipeline_config
from app.models.idempotency import IdempotencyKeyRecord

REPLAY_HEADER = "Idempotent-Replay"
_STATE_ATTR = "idempotency"


class IdempotentReplay(Exception):
    """Raised by the dependency to short-circuit a route with the stored response."""

    def __init__(self, body: Any, status_code: int):
        self.body = body
        self.status_code = status_code


def replay_response(_request: Request, exc: IdempotentReplay) -> JSONResponse:
    return JSONResponse(content=exc.body, status_code=exc.status_code, headers={REPLAY_HEADER: "true"})


def require_idempotency_key(
    idempotency_key: str | None = Header(None, alias="Idempotency-Key")
) -> str:
    """Validates the presence and format of the required Idempotency-Key header."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required"
        )
    clean_key = idempotency_key.strip()
    if len(clean_key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header exceeds maximum length of 255 characters"
        )
    return clean_key


def canonical_request_hash(raw_body: bytes) -> str:
    """SHA-256 of the request body, with JSON bodies canonicalised (key order, whitespace)."""
    if not raw_body:
        return hashlib.sha256(b"").hexdigest()
    try:
        parsed = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return hashlib.sha256(raw_body).hexdigest()
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class IdempotencyContext:
    """Lifecycle of one key: ``begin`` -> route -> ``complete`` or ``release``."""

    def __init__(self, key: str, user_id: str, endpoint: str, request_hash: str, db: Session):
        self.key = key
        self.user_id = user_id
        self.endpoint = endpoint
        self.request_hash = request_hash
        self.db = db

    def _load(self) -> IdempotencyKeyRecord | None:
        return self.db.scalars(
            select(IdempotencyKeyRecord).where(
                IdempotencyKeyRecord.user_id == self.user_id,
                IdempotencyKeyRecord.key == self.key,
            )
        ).first()

    def begin(self) -> None:
        """Claims the key, or raises ``IdempotentReplay`` / 409 / 422 for an existing claim."""
        now = datetime.now(timezone.utc)
        record = self._load()
        if record is not None:
            if _utc(record.expires_at) <= now:
                self.db.delete(record)
                self.db.commit()
            elif record.endpoint != self.endpoint or record.request_hash != self.request_hash:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Idempotency-Key already used with different payload"
                )
            elif record.status == "completed":
                raise IdempotentReplay(record.response_body, record.status_code)
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A request with this Idempotency-Key is currently in progress"
                )

        ttl_hours = get_pipeline_config().safety.idempotency.ttl_hours
        self.db.add(IdempotencyKeyRecord(
            key=self.key,
            user_id=self.user_id,
            endpoint=self.endpoint,
            request_hash=self.request_hash,
            status="in_flight",
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        ))
        try:
            self.db.commit()
        except IntegrityError as exc:
            # A concurrent request with the same key claimed it between our read and insert.
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A request with this Idempotency-Key is currently in progress"
            ) from exc

    def complete(self, response_body: Any, status_code: int) -> None:
        """Stores the route's response for replay."""
        record = self._load()
        if record is None:
            raise RuntimeError(f"idempotency key {self.key!r} vanished while in flight")
        record.status = "completed"
        record.status_code = status_code
        record.response_body = response_body
        self.db.commit()

    def release(self) -> None:
        """Drops the in-flight claim so the caller may retry with the same key."""
        self.db.rollback()
        record = self._load()
        if record is not None and record.status == "in_flight":
            self.db.delete(record)
            self.db.commit()


def idempotent(endpoint: str) -> Any:
    """The dependency for one confirm/approve route; ``endpoint`` names the route in the key."""

    async def dependency(
        request: Request,
        key: str = Depends(require_idempotency_key),
        user: CurrentUser = Depends(current_user),
        db: Session = Depends(get_db),
    ) -> IdempotencyContext:
        request_hash = canonical_request_hash(await request.body())
        # The path carries case and stage IDs, so a key reused on another case never replays.
        ctx = IdempotencyContext(key, str(user.id), f"{endpoint} {request.url.path}", request_hash, db)
        await run_in_threadpool(ctx.begin)
        setattr(request.state, _STATE_ATTR, ctx)
        return ctx

    return Depends(dependency)


class IdempotentRoute(APIRoute):
    """Completes or releases the key claimed by ``idempotent`` once the route has answered."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def idempotent_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except BaseException:
                ctx = getattr(request.state, _STATE_ATTR, None)
                if ctx is not None:
                    await run_in_threadpool(ctx.release)
                raise
            ctx = getattr(request.state, _STATE_ATTR, None)
            if ctx is not None:
                if 200 <= response.status_code < 300:
                    await run_in_threadpool(ctx.complete, json.loads(response.body), response.status_code)
                else:
                    await run_in_threadpool(ctx.release)
            return response

        return idempotent_handler
