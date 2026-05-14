"""Bearer-token auth for destructive endpoints.

This is a portfolio-demo auth scheme: a single shared token, set as
VALUATE_OVERRIDE_TOKEN in the env. When set, /override requires
`Authorization: Bearer <token>`; when unset (local dev), the endpoint
runs without auth.

The threat model is "random scraping / accidental corruption," not
"sophisticated adversary." A production deployment would put both
endpoints behind real per-user auth; the case study acknowledges this.

The frontend's Next.js middleware injects the same token as a header on
proxied requests (Vercel env var, never exposed to the browser).
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Header, HTTPException


def require_override_auth(authorization: Optional[str] = Header(None)) -> None:
    """FastAPI dependency: require `Authorization: Bearer <env-token>` on /override.

    If VALUATE_OVERRIDE_TOKEN is unset, this is a no-op (local dev). Otherwise
    a missing or mismatched header returns 401.
    """
    expected = os.environ.get("VALUATE_OVERRIDE_TOKEN")
    if not expected:
        return  # Auth disabled — local dev or explicit "no token" mode

    if authorization is None or authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized")
