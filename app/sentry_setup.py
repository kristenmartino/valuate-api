"""Optional Sentry integration — off by default.

Activates when SENTRY_DSN is set in the environment. The Sentry SDK is
import-time-optional: we don't import it unless the env var is present,
so a clean install without `sentry-sdk` doesn't error and the deploy
size doesn't include the dep unless wanted.

Use case is the obvious one: production exceptions get reported with
stack traces + request context, instead of being lost to Railway's
log buffer. The structured-logging middleware handles routine request
logs; Sentry handles the unexpected.

Set SENTRY_DSN on Railway to activate. Leave unset for local dev.
"""

from __future__ import annotations

import os


def init_sentry() -> bool:
    """Initialize Sentry if SENTRY_DSN is set. Returns True if active.

    Lazy-imports `sentry_sdk` so a Python install without the package
    doesn't fail. The package is listed as optional in requirements.txt
    (commented-out line + setup notes) so a deployer who wants Sentry
    can `pip install sentry-sdk[fastapi]` and set the env var.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        # SENTRY_DSN set but the package isn't installed — log a warning
        # via stdout (so the structured logger picks it up if it's also
        # active) and proceed without Sentry.
        import sys

        print(
            '{"level":"warn","msg":"SENTRY_DSN set but sentry-sdk not installed; '
            'pip install sentry-sdk[fastapi] to enable error reporting"}',
            file=sys.stdout,
            flush=True,
        )
        return False

    environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "local")
    release = os.environ.get("RAILWAY_GIT_COMMIT_SHA")  # short-circuits to None when unset
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
        ],
        # Conservative sample rates — this is a portfolio demo, not a
        # high-traffic production service. 100% errors, 10% transactions.
        traces_sample_rate=0.1,
        # Don't send PII (we don't have any, but better-safe).
        send_default_pii=False,
    )
    return True
