"""
Simple API-key authentication.

Not a user-account system -- there's no database yet, and building one
prematurely is exactly the kind of infrastructure docs/PHASE_0_AUDIT.md
warns against adding before there's a concrete need for it (per-user
history, usage billing, and so on). This is the minimum that actually
blocks untrusted traffic: a request must present one of a small set of
pre-shared keys, configured via environment variable, or it's rejected.
A real multi-user identity system is a reasonable later phase once
there's an actual need for per-user data, not just access control.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from . import config


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency: raises 401 unless the request's Authorization
    header is "Bearer <a configured key>".

    If no keys are configured at all (config.API_KEYS is empty), auth is
    effectively disabled -- an explicit, documented choice (see
    config.py), not a silent bypass, so a fresh local/dev setup keeps
    working out of the box without extra configuration.
    """
    if not config.API_KEYS:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or malformed Authorization header."
        )

    presented = authorization.removeprefix("Bearer ").strip()
    # Constant-time comparison against every configured key, so response
    # timing doesn't leak how close a guess was to any one of them.
    if not any(secrets.compare_digest(presented, key) for key in config.API_KEYS):
        raise HTTPException(status_code=401, detail="Invalid API key.")
