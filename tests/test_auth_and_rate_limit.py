"""Tests for the bearer-token auth (on /override) and IP rate limit (on /extract).

Both modules are designed to be testable in isolation: the auth dep reads
the env var on each invocation, and the rate-limit module exposes a
test-only reset hook. We don't spin up a full FastAPI TestClient — the
deps are simple enough to exercise directly.
"""

import os

import pytest
from fastapi import HTTPException

from app.auth import require_override_auth
from app.rate_limit import (
    IPRateLimiter,
    _reset_limiter_for_tests,
    require_extract_rate_limit,
)


# --- auth ---------------------------------------------------------------------


def test_override_auth_is_noop_when_env_token_unset(monkeypatch):
    """Local-dev mode: no env token → no auth check, no exception."""
    monkeypatch.delenv("VALUATE_OVERRIDE_TOKEN", raising=False)
    # Both missing header and any header should pass through
    require_override_auth(authorization=None)
    require_override_auth(authorization="Bearer literally-anything")


def test_override_auth_rejects_missing_authorization(monkeypatch):
    """Token set → missing header returns 401."""
    monkeypatch.setenv("VALUATE_OVERRIDE_TOKEN", "secret-xyz")
    with pytest.raises(HTTPException) as exc_info:
        require_override_auth(authorization=None)
    assert exc_info.value.status_code == 401


def test_override_auth_rejects_wrong_token(monkeypatch):
    """Token set → wrong bearer token returns 401."""
    monkeypatch.setenv("VALUATE_OVERRIDE_TOKEN", "secret-xyz")
    with pytest.raises(HTTPException) as exc_info:
        require_override_auth(authorization="Bearer wrong-token")
    assert exc_info.value.status_code == 401


def test_override_auth_rejects_wrong_scheme(monkeypatch):
    """Token set → wrong scheme (e.g. Basic) returns 401."""
    monkeypatch.setenv("VALUATE_OVERRIDE_TOKEN", "secret-xyz")
    with pytest.raises(HTTPException) as exc_info:
        require_override_auth(authorization="Basic c2VjcmV0LXh5eg==")
    assert exc_info.value.status_code == 401


def test_override_auth_accepts_correct_bearer_token(monkeypatch):
    """Token set + correct header → no exception."""
    monkeypatch.setenv("VALUATE_OVERRIDE_TOKEN", "secret-xyz")
    require_override_auth(authorization="Bearer secret-xyz")  # no raise


# --- rate limiter -------------------------------------------------------------


def test_ip_rate_limiter_allows_under_limit():
    """First N requests within the window should all be allowed."""
    limiter = IPRateLimiter(max_requests=3, window_seconds=60.0)
    for _ in range(3):
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed is True


def test_ip_rate_limiter_blocks_over_limit():
    """The (N+1)th request from the same IP within the window is blocked."""
    limiter = IPRateLimiter(max_requests=2, window_seconds=60.0)
    limiter.check("1.2.3.4")
    limiter.check("1.2.3.4")
    allowed, retry_after = limiter.check("1.2.3.4")
    assert allowed is False
    assert retry_after >= 1  # always at least 1s; ceiling-rounded for clients


def test_ip_rate_limiter_tracks_ips_independently():
    """One IP burning its quota doesn't block another IP."""
    limiter = IPRateLimiter(max_requests=2, window_seconds=60.0)
    limiter.check("1.1.1.1")
    limiter.check("1.1.1.1")
    blocked, _ = limiter.check("1.1.1.1")
    assert blocked is False
    # Different IP starts fresh
    fresh, _ = limiter.check("2.2.2.2")
    assert fresh is True


def test_ip_rate_limiter_evicts_old_hits():
    """Hits outside the window are dropped, freeing slots for new ones."""
    limiter = IPRateLimiter(max_requests=2, window_seconds=0.05)
    limiter.check("1.2.3.4")
    limiter.check("1.2.3.4")
    blocked, _ = limiter.check("1.2.3.4")
    assert blocked is False

    import time

    time.sleep(0.1)  # all hits aged out of the 50ms window
    allowed, _ = limiter.check("1.2.3.4")
    assert allowed is True


def test_extract_rate_limit_dep_raises_429_with_retry_after_header():
    """When the per-IP limit is exceeded, the FastAPI dep should raise 429
    with a Retry-After header set so clients can back off correctly."""
    _reset_limiter_for_tests(max_requests=1, window_seconds=60.0)

    class _FakeRequest:
        def __init__(self, ip: str):
            self.client = type("c", (), {"host": ip})()
            self.headers = {}

    # First call: allowed
    require_extract_rate_limit(_FakeRequest("9.9.9.9"))  # no raise

    # Second call: blocked
    with pytest.raises(HTTPException) as exc_info:
        require_extract_rate_limit(_FakeRequest("9.9.9.9"))
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
    assert int(exc_info.value.headers["Retry-After"]) >= 1

    # Reset after test so we don't poison the module-level state.
    _reset_limiter_for_tests()


def test_extract_rate_limit_dep_uses_x_forwarded_for_when_present():
    """Behind Railway's edge proxy, the real client IP is in X-Forwarded-For.
    The rate limiter should key off that, not the proxy's IP."""
    _reset_limiter_for_tests(max_requests=1, window_seconds=60.0)

    class _FakeRequest:
        def __init__(self, xff: str, direct: str = "10.0.0.1"):
            self.client = type("c", (), {"host": direct})()
            self.headers = {"X-Forwarded-For": xff}

    # Two "different" clients via XFF, even though they share a direct hop
    require_extract_rate_limit(_FakeRequest("203.0.113.1"))  # no raise
    require_extract_rate_limit(_FakeRequest("203.0.113.2"))  # no raise (different IP)

    # Third request from the first IP should now be blocked
    with pytest.raises(HTTPException):
        require_extract_rate_limit(_FakeRequest("203.0.113.1"))

    _reset_limiter_for_tests()
