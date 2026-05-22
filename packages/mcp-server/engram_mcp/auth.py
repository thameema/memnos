"""
engram_mcp.auth — API-key authentication helpers.

verify_api_key   : checks a raw key against config.auth.api_keys
APIKeyMiddleware : FastAPI middleware that reads Authorization: Bearer <key>
                   and injects request.state.user_id / allowed_namespaces.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helper — no framework dependency
# ---------------------------------------------------------------------------

def verify_api_key(key: str, config) -> str | None:
    """
    Validate *key* against the list in ``config.auth.api_keys``.

    Each entry in that list must expose ``.key``, ``.user_id``, and
    ``.namespaces`` attributes (Pydantic model or plain object).

    Returns the ``user_id`` string on success, ``None`` on failure.
    """
    if not key:
        return None

    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    for entry in api_keys:
        stored_key = getattr(entry, "key", None)
        if stored_key and stored_key == key:
            user_id = getattr(entry, "user_id", None)
            logger.debug("API key verified for user_id=%s", user_id)
            return user_id

    logger.warning("API key verification failed (key prefix=%s…)", key[:6] if len(key) >= 6 else key)
    return None


def get_allowed_namespaces(key: str, config) -> list[str]:
    """Return the namespace list for the given key (empty list = no access)."""
    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    for entry in api_keys:
        if getattr(entry, "key", None) == key:
            namespaces = getattr(entry, "namespaces", [])
            return list(namespaces) if namespaces else []
    return []


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------

_OPEN_PATHS = {"/health", "/sse", "/sse/"}  # paths that skip auth


class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Starlette/FastAPI middleware that enforces ``Authorization: Bearer <key>``.

    On success  → sets ``request.state.user_id`` and
                         ``request.state.allowed_namespaces``
    On failure  → returns 401 JSON immediately
    On skipped  → passes through (health-check and SSE negotiation)
    """

    def __init__(self, app: ASGIApp, config, skip_paths: set[str] | None = None) -> None:
        super().__init__(app)
        self._config = config
        self._skip_paths = skip_paths if skip_paths is not None else _OPEN_PATHS

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Let health-checks and SSE-upgrade requests through unauthenticated
        if path in self._skip_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or malformed Authorization header"},
            )

        raw_key = auth_header[len("Bearer "):]
        user_id = verify_api_key(raw_key, self._config)
        if user_id is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key"},
            )

        request.state.user_id = user_id
        request.state.allowed_namespaces = get_allowed_namespaces(raw_key, self._config)

        # Store the full ApiKeyEntry on request.state so downstream handlers
        # can inspect read_only and other per-key properties.
        api_keys = getattr(getattr(self._config, "auth", None), "api_keys", [])
        for entry in api_keys:
            if getattr(entry, "key", None) == raw_key:
                request.state.key_entry = entry
                break

        return await call_next(request)
