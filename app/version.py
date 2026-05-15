"""Build-time identity for the running container.

Reads from env vars set by Railway / GitHub Actions during deploy. Falls
back to "unknown" + the import-time wallclock when the env vars are
absent (local dev). Surfaced through GET /version so a stale-deploy
diagnosis is one curl away instead of requiring rate-limit-respecting
behavioral probes.

Set on Railway via:
    RAILWAY_GIT_COMMIT_SHA       — auto-injected
    RAILWAY_DEPLOYMENT_ID        — auto-injected
The fields are documented as falling back to None so the endpoint never
errors; an "unknown SHA" response still tells the caller the service is
up.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Capture at module import so the timestamp reflects "when this container
# started" — the ops question this answers is "is the running container
# the version I expect?"
SERVICE_STARTED_AT = _utc_now_iso()


def get_version_info() -> dict[str, str | None]:
    """Build the /version response payload."""
    # Lazy import so the version module doesn't drag in the prompt at module
    # load — keeps the import graph clean.
    from extraction_prompt import PROMPT_HASH

    return {
        "commit_sha": os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
        "commit_sha_short": (
            os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or None
        ),
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID"),
        "started_at": SERVICE_STARTED_AT,
        "environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local"),
        # First 12 chars of sha256(EXTRACTION_SYSTEM_PROMPT) — a "did the
        # prompt change?" diagnostic separate from the commit SHA, since
        # the prompt can change without a code commit (e.g., after a
        # prompt-eval iteration).
        "prompt_hash": PROMPT_HASH,
    }
