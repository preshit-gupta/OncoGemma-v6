"""In-app rate limiting middleware (SPEC-03 §5.3.4).

Enforces, per API instance:
- ``auth_session_per_ip_per_min`` on POST /api/v1/auth/session, per client IP.
- ``mutating_per_user_per_min`` on mutating routes (POST, PUT, PATCH, DELETE), per signed-in
  user; requests without a validly signed session cookie count against their client IP.

Identities come only from values a client cannot forge: the session cookie's HS256 signature
is verified, and the client IP is read ``trusted_proxy_hops`` entries from the right of
X-Forwarded-For (entries further left are client-supplied). Thresholds are in configs/safety.yaml.
"""
import json
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth.errors import AuthError
from app.auth.sessions import SESSION_COOKIE, decode_session_token
from app.core.pipeline_config import get_pipeline_config

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SIGN_IN_PATH = "/api/v1/auth/session"
# Cloud Tasks callbacks are authenticated by OIDC and paced by the queue, not by users.
EXEMPT_PATHS = frozenset({"/api/v1/internal/execute-stage"})


class RateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[str, Deque[float]] = defaultdict(deque)

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = self._buckets[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= max_requests:
                retry_after = max(1, int(timestamps[0] + window_seconds - now))
                return False, retry_after
            timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()


def get_client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """The client IP as recorded by our own proxies, never a client-supplied X-Forwarded-For entry."""
    forwarded = [part.strip() for part in request.headers.get("X-Forwarded-For", "").split(",") if part.strip()]
    if len(forwarded) >= trusted_proxy_hops:
        return forwarded[-trusted_proxy_hops]
    # Fewer entries than our proxy chain adds: the request did not come through it.
    return request.client.host if request.client else "unknown"


def get_rate_limit_identity(request: Request, trusted_proxy_hops: int) -> str:
    """``user:<id>`` for a validly signed session cookie, otherwise ``ip:<client ip>``."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            return f"user:{decode_session_token(token)['uid']}"
        except AuthError:
            # Forged or expired cookie: the route itself rejects it; meter it by IP.
            pass
    return f"ip:{get_client_ip(request, trusted_proxy_hops)}"


def _too_many(detail: str, retry_after: int) -> Response:
    return Response(
        content=json.dumps({"detail": detail}),
        status_code=429,
        media_type="application/json",
        headers={"Retry-After": str(retry_after)},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method not in MUTATING_METHODS or path in EXEMPT_PATHS:
            return await call_next(request)

        rate_cfg = get_pipeline_config().safety.rate_limits

        if request.method == "POST" and path == SIGN_IN_PATH:
            key = f"auth_ip:{get_client_ip(request, rate_cfg.trusted_proxy_hops)}"
            allowed, retry_after = limiter.is_allowed(
                key, rate_cfg.auth_session_per_ip_per_min, rate_cfg.window_seconds
            )
            if not allowed:
                return _too_many("Rate limit exceeded for sign-in", retry_after)
        else:
            key = f"mutating:{get_rate_limit_identity(request, rate_cfg.trusted_proxy_hops)}"
            allowed, retry_after = limiter.is_allowed(
                key, rate_cfg.mutating_per_user_per_min, rate_cfg.window_seconds
            )
            if not allowed:
                return _too_many("Rate limit exceeded for mutating requests", retry_after)

        return await call_next(request)
