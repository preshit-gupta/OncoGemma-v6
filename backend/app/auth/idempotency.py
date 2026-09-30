"""Idempotency-Key header validation, persistence, and replay (SPEC-03 §5.3.3).

Confirm and approve stage transition calls require an Idempotency-Key header.
Keys are retained in the database for 24 hours (configured in configs/safety.yaml).
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Tuple

from fastapi import Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pipeline_config import get_pipeline_config
from app.models.idempotency import IdempotencyKeyRecord


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


def canonical_request_hash(data: Any) -> str:
    """Produces SHA256 hex digest of canonical JSON payload."""
    if data is None:
        raw = b""
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class IdempotencyContext:
    """Manages transactional idempotency lifecycle for a request."""

    def __init__(self, key: str, user_id: str, endpoint: str, request_hash: str, db: Session):
        self.key = key
        self.user_id = user_id
        self.endpoint = endpoint
        self.request_hash = request_hash
        self.db = db
        self.record: IdempotencyKeyRecord | None = None

    def check(self) -> Tuple[bool, dict | None, int | None]:
        """
        Checks if the idempotency key exists and is valid.
        Returns:
            (is_cached, response_body, status_code)
        """
        now = datetime.now(timezone.utc)
        stmt = select(IdempotencyKeyRecord).where(IdempotencyKeyRecord.key == self.key)
        self.record = self.db.scalars(stmt).first()

        if self.record:
            rec_expires = self.record.expires_at
            if rec_expires.tzinfo is None:
                rec_expires = rec_expires.replace(tzinfo=timezone.utc)
            if rec_expires > now:
                if self.record.request_hash != self.request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Idempotency-Key already used with different payload"
                    )
                if self.record.status == "completed":
                    return True, self.record.response_body, self.record.status_code
                if self.record.status == "in_flight":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A request with this Idempotency-Key is currently in progress"
                    )
            else:
                # Key expired, delete old record
                self.db.delete(self.record)
                self.db.commit()
                self.record = None

        ttl_hours = get_pipeline_config().safety.idempotency.ttl_hours
        expires_at = now + timedelta(hours=ttl_hours)
        self.record = IdempotencyKeyRecord(
            key=self.key,
            user_id=self.user_id,
            endpoint=self.endpoint,
            request_hash=self.request_hash,
            status="in_flight",
            created_at=now,
            expires_at=expires_at,
        )
        self.db.add(self.record)
        self.db.commit()
        return False, None, None

    def complete(self, response_body: Any, status_code: int = 200) -> None:
        """Marks the idempotency record as completed with the given response."""
        if self.record:
            self.record.status = "completed"
            self.record.status_code = status_code
            self.record.response_body = response_body if isinstance(response_body, dict) else (
                json.loads(response_body) if isinstance(response_body, str) else None
            )
            self.db.commit()

    def fail(self) -> None:
        """Removes in-flight marker on unhandled error to permit retries."""
        if self.record:
            try:
                self.db.delete(self.record)
                self.db.commit()
            except Exception:
                self.db.rollback()
