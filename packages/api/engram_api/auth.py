"""
engram_api.auth — FastAPI dependency-injection helpers for authentication and shared state.

Provides:
    get_config()             — returns app.state.config (EngramConfig)
    get_client()             — returns app.state.client (EngramClient)
    get_orchestrator()       — returns app.state.orchestrator (Orchestrator)
    require_api_key()        — validates Authorization: Bearer <token>, returns user_id
    require_api_key_entry()  — validates Authorization: Bearer <token>, returns ApiKeyEntry
    check_namespace_access() — raises HTTPException(403) if key lacks namespace access
    require_admin_access()   — raises HTTPException(403) unless key has wildcard ("*") access
"""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Request

from engram.config import ApiKeyEntry  # type: ignore

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
# Internal key-validation helper (DRY core used by both public dependencies)
# ---------------------------------------------------------------------------

async def _validate_key(
    authorization: str | None,
    config,
) -> ApiKeyEntry:
    """
    Validate a raw ``Authorization: Bearer <key>`` header value.

    Returns the matching ``ApiKeyEntry`` on success.
    Raises ``HTTPException(401)`` on missing or invalid key.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must use Bearer scheme",
        )

    raw_key = authorization[len("Bearer "):]

    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    for entry in api_keys:
        stored_key = getattr(entry, "key", None)
        if stored_key and stored_key == raw_key:
            logger.debug(
                "API key authenticated: user_id=%s", getattr(entry, "user_id", "unknown")
            )
            return entry

    logger.warning(
        "API key authentication failed (key prefix=%s…)",
        raw_key[:6] if len(raw_key) >= 6 else raw_key,
    )
    raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Public authentication dependencies
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
    entry = await _validate_key(authorization, config)
    return str(getattr(entry, "user_id", "unknown"))


async def require_api_key_entry(
    authorization: str | None = Header(default=None),
    config=Depends(get_config),
) -> ApiKeyEntry:
    """
    Validate the ``Authorization: Bearer <key>`` header.

    Returns the full ``ApiKeyEntry`` on success (key, user_id, namespaces).
    Raises ``HTTPException(401)`` on missing / invalid key.
    """
    return await _validate_key(authorization, config)


# ---------------------------------------------------------------------------
# Namespace access control
# ---------------------------------------------------------------------------

async def check_namespace_access(key_entry: ApiKeyEntry, namespace: str) -> None:
    """
    Assert that *key_entry* is permitted to operate on *namespace*.

    Access rules (evaluated in order — first match wins):

    1. ``"*"`` in the key's namespaces list  →  allow everything.
    2. Exact match: ``namespace`` is listed verbatim  →  allow.
    3. Prefix wildcard: the key lists ``"prefix:*"`` and *namespace* starts
       with ``"prefix:"``  →  allow (e.g. key has ``"personal:*"`` and the
       requested namespace is ``"personal:thameema"``).
    4. No match  →  raise ``HTTPException(403)``.

    Parameters
    ----------
    key_entry:
        The ``ApiKeyEntry`` returned by ``require_api_key_entry()``.
    namespace:
        The namespace value extracted from the incoming request.
    """
    allowed: list[str] = getattr(key_entry, "namespaces", []) or []

    # Rule 1 — wildcard: key can access everything
    if "*" in allowed:
        return

    # Rule 2 — exact match
    if namespace in allowed:
        return

    # Rule 3 — prefix wildcard  e.g. "personal:*" matches "personal:thameema"
    for pattern in allowed:
        if pattern.endswith(":*") and namespace.startswith(pattern[:-1]):
            return

    user_id = getattr(key_entry, "user_id", "unknown")
    logger.warning(
        "Namespace access denied: user=%s namespace=%r allowed=%r",
        user_id,
        namespace,
        allowed,
    )
    raise HTTPException(
        status_code=403,
        detail=f"API key does not have access to namespace '{namespace}'",
    )


async def require_admin_access(
    key_entry: ApiKeyEntry = Depends(require_api_key_entry),
) -> ApiKeyEntry:
    """
    Dependency that requires the API key to carry wildcard (``"*"``) namespace
    access — i.e. admin-level permission.

    Used on namespace create/delete endpoints where scoped keys must be rejected
    even if they match an individual namespace, because those operations affect
    the global configuration.

    Raises ``HTTPException(403)`` for non-admin keys.
    """
    allowed: list[str] = getattr(key_entry, "namespaces", []) or []
    if "*" not in allowed:
        user_id = getattr(key_entry, "user_id", "unknown")
        logger.warning(
            "Admin access denied: user=%s namespaces=%r", user_id, allowed
        )
        raise HTTPException(
            status_code=403,
            detail="This operation requires admin access (wildcard namespace permission)",
        )
    return key_entry
