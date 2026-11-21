"""
Access control and rate limiting for a deployed backend.

- Password: when LOGICFORGE_ACCESS_PASSWORD is set, every request except the
  health check and /auth/status must send "Authorization: Bearer <password>".
  Unset (local development) means no password is required.
- Rate limits: per client IP, a sliding one-hour window for AI requests
  (they cost API money) and for simulation / schematic requests (they use
  server CPU). Counts live in memory, so run a single backend process.
"""

import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config

PUBLIC_PATHS = {"/", "/auth/status", "/docs", "/openapi.json"}

AI_PATHS = {"/generate", "/generate-testbench", "/debug"}
COMPUTE_PATHS = {"/simulate", "/schematic"}
WINDOW_SECONDS = 3600


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client: str, bucket: str, limit: int) -> tuple[bool, int]:
        """Record a hit; return (allowed, seconds until the next free slot)."""
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(client, bucket)]
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= limit:
                return False, int(WINDOW_SECONDS - (now - hits[0])) + 1
            hits.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # Hosts such as Render put the real client first in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def password_ok(request: Request) -> bool:
    expected = config.ACCESS_PASSWORD
    if not expected:
        return True
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    return hmac.compare_digest(supplied.encode(), expected.encode())


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        if not password_ok(request):
            return JSONResponse(
                {"detail": "A valid access password is required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        bucket, limit = None, 0
        if path in AI_PATHS:
            bucket, limit = "ai", config.AI_REQUESTS_PER_HOUR
        elif path in COMPUTE_PATHS:
            bucket, limit = "compute", config.COMPUTE_REQUESTS_PER_HOUR
        if bucket:
            allowed, retry_after = limiter.allow(client_ip(request), bucket, limit)
            if not allowed:
                minutes = max(1, retry_after // 60)
                kind = "AI" if bucket == "ai" else "simulation"
                return JSONResponse(
                    {"detail": f"Hourly {kind} limit reached. Try again in about {minutes} minute(s)."},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
