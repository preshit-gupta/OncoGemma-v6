"""In-app rate limiting middleware (SPEC-03 §5.3.4).

Enforces:
- 10 req/min per IP on POST /api/v1/auth/session.
- 60 req/min per user on mutating routes (POST, PUT, PATCH, DELETE).
Thresholds are loaded from configs/safety.yaml into PipelineConfig.
"""
import json
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.pipeline_config import get_pipeline_config

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXEMPT_PATHS = frozenset({
    "/health",
    "/api/health",
    "/healthz",
    "/api/healthz",
    "/api/v1/health",
    "/api/v1/healthz",
    "/api/v1/internal/execute-stage",
})


class RateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[str, Deque[float]] = defaultdict(deque)

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = self._buckets[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= max_requests:
                earliest = timestamps[0]
                retry_after = max(1, int(earliest + window_seconds - now))
                return False, retry_after
            timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


limiter = RateLimiter()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "127.0.0.1"


def get_user_identifier(request: Request) -> str:
    # 1. Test header in test environments
    test_user = request.headers.get("X-Test-User-Id")
    if test_user:
        return f"user:{test_user}"

    # 2. og_session cookie
    cookie = request.cookies.get("og_session")
    if cookie:
        # Fast extraction of sub/uid without heavy verification for rate limit accounting
        try:
            import jwt
            unverified = jwt.decode(cookie, options={"verify_signature": False})
            uid = unverified.get("uid") or unverified.get("sub")
            if uid:
                return f"user:{uid}"
        except Exception:
            pass
        return f"session:{cookie[:16]}"

    return f"ip:{get_client_ip(request)}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in EXEMPT_PATHS:
            return await call_next(request)

        try:
            safety_cfg = get_pipeline_config().safety
            rate_cfg = safety_cfg.rate_limits
        except Exception:
            # If config is not loaded yet during startup, proceed
            return await call_next(request)

        # 1. Sign-in rate limit: 10/min per IP
        if request.method == "POST" and path == "/api/v1/auth/session":
            ip_key = f"auth_ip:{get_client_ip(request)}"
            allowed, retry_after = limiter.is_allowed(
                ip_key,
                max_requests=rate_cfg.auth_session_per_ip_per_min,
                window_seconds=rate_cfg.window_seconds,
            )
            if not allowed:
                return Response(
                    content=json.dumps({"detail": "Rate limit exceeded for sign-in"}),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": str(retry_after)},
                )

        # 2. Mutating requests rate limit: 60/min per user
        elif request.method in MUTATING_METHODS:
            user_key = f"mutating:{get_user_identifier(request)}"
            allowed, retry_after = limiter.is_allowed(
                user_key,
                max_requests=rate_cfg.mutating_per_user_per_min,
                window_seconds=rate_cfg.window_seconds,
            )
            if not allowed:
                return Response(
                    content=json.dumps({"detail": "Rate limit exceeded for mutating requests"}),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
