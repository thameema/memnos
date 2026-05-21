"""
engram_api.auth — FastAPI dependency-injection helpers for authentication and shared state.

Provides:
    get_config()       — returns app.state.config (EngramConfig)
    get_client()       — returns app.state.client (EngramClient)
    get_orchestrator() — returns app.state.orchestrator (Orchestrator)
    require_api_key()  — validates Authorization: Bearer <token>, returns user_id
"""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared-state dependencies (stored on app.state at startup)
# ---------------------------------------------------------------------------

def get_config(request: Request):
    """Return the EngramConfig singleton stored on app.state."""
    return request.app.state.config


def get_client(request: Request):
    """Return the EngramClient singleton stored on app.state."""
    return request.app.state.client


def get_orchestrator(request: Request):
    """Return the Orchestrator singleton stored on app.state."""
    return request.app.state.orchestrator


# ---------------------------------------------------------------------------
# API-key authentication dependency
# ---------------------------------------------------------------------------

async def require_api_key(
    authorization: str | None = Header(default=None),
    config=Depends(get_config),
) -> str:
    """
    Validate the ``Authorization: Bearer <key>`` header.

    Returns the associated ``user_id`` on success.
    Raises ``HTTPException(401)`` on missing / invalid key.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must use Bearer scheme",
        )

    raw_key = authorization[len("Bearer "):]

    # Walk the api_keys list from config.auth
    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    for entry in api_keys:
        stored_key = getattr(entry, "key", None)
        if stored_key and stored_key == raw_key:
            user_id = getattr(entry, "user_id", "unknown")
            logger.debug("API key authenticated: user_id=%s", user_id)
            return str(user_id)

    logger.warning(
        "API key authentication failed (key prefix=%s…)",
        raw_key[:6] if len(raw_key) >= 6 else raw_key,
    )
    raise HTTPException(status_code=401, detail="Invalid API key")
