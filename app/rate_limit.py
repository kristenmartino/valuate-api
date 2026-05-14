"""Sliding-window IP rate limiter for /extract.

In-memory and per-process. Across multiple Railway replicas it would
become per-replica, but we run a single replica — for a portfolio demo
this is fine. A production deployment would back this with Redis.

The /extract endpoint is the expensive one: each first-time call invokes
Claude (Track B) and burns Anthropic credits. /value, /comps, and
/company are cheap reads — not rate-limited. /override is auth-gated
instead, which is the right shape for destructive writes.

Default: 10 extracts/hour/IP. Configurable via VALUATE_EXTRACT_RATE_LIMIT
(format: "<count>/<window-in-seconds>", e.g. "5/3600" for 5/hour).
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from time import monotonic
from typing import Deque, Optional

from fastapi import HTTPException, Request


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, accounting for Railway's edge proxy.

    Railway sits behind a proxy that sets X-Forwarded-For. The leftmost
    value in XFF is the original client (Railway's own hops come after).
    Falls back to the direct connection IP if XFF is absent.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class IPRateLimiter:
    """Sliding-window counter per IP.

    Each IP gets a deque of monotonic timestamps. On each request, we drop
    timestamps outside the window and check if the remaining count is
    under the limit. Memory is bounded by (active_IPs × limit_count) —
    fine for a demo's scale.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max = max_requests
        self.window = window_seconds
        self.hits: dict[str, Deque[float]] = defaultdict(deque)

    def check(self, ip: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        retry_after is set only when allowed=False — it's the time until
        the oldest in-window hit ages out, after which one more request
        would be allowed.
        """
        now = monotonic()
        bucket = self.hits[ip]
        # Evict hits older than the window
        while bucket and bucket[0] < now - self.window:
            bucket.popleft()
        if len(bucket) >= self.max:
            retry = int(bucket[0] + self.window - now) + 1
            return False, max(1, retry)
        bucket.append(now)
        return True, 0


def _parse_config() -> tuple[int, float]:
    """Read VALUATE_EXTRACT_RATE_LIMIT env var; default 10/3600."""
    raw = os.environ.get("VALUATE_EXTRACT_RATE_LIMIT", "10/3600")
    try:
        count_str, window_str = raw.split("/", 1)
        return int(count_str), float(window_str)
    except (ValueError, AttributeError):
        return 10, 3600.0


_max, _window = _parse_config()
_extract_limiter = IPRateLimiter(max_requests=_max, window_seconds=_window)


def require_extract_rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 when the per-IP rate is exceeded."""
    ip = _client_ip(request)
    allowed, retry_after = _extract_limiter.check(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many extracts from this IP. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )


def _reset_limiter_for_tests(
    max_requests: Optional[int] = None,
    window_seconds: Optional[float] = None,
) -> None:
    """Reset the global limiter — used by tests only."""
    global _extract_limiter
    m = max_requests if max_requests is not None else _max
    w = window_seconds if window_seconds is not None else _window
    _extract_limiter = IPRateLimiter(max_requests=m, window_seconds=w)
