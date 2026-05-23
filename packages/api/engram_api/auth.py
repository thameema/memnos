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
    key_store=None,
) -> ApiKeyEntry:
    """
    Validate a raw ``Authorization: Bearer <key>`` header value.

    Checks YAML-configured keys first, then falls back to the runtime key
    store (SQLite) when ``key_store`` is provided.

    Returns the matching ``ApiKeyEntry`` on success.
    Raises ``HTTPException(401)`` on missing or invalid key.
    """
    # open_mode: local single-user install with no network exposure
    auth_config = getattr(config, "auth", None)
    if getattr(auth_config, "open_mode", False):
        return ApiKeyEntry(key="", user_id="local", namespaces=["*"])

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header must use Bearer scheme",
        )

    raw_key = authorization[len("Bearer "):]

    # 1. Check YAML-configured keys
    api_keys = getattr(getattr(config, "auth", None), "api_keys", [])
    for entry in api_keys:
        stored_key = getattr(entry, "key", None)
        if stored_key and stored_key == raw_key:
            logger.debug(
                "API key authenticated: user_id=%s", getattr(entry, "user_id", "unknown")
            )
            return entry

    # 2. Fall back to runtime key store (if available)
    if key_store is not None:
        try:
            entry = await key_store.verify(raw_key)
            if entry is not None:
                logger.debug(
                    "Runtime API key authenticated: user_id=%s",
                    getattr(entry, "user_id", "unknown"),
                )
                return entry
        except Exception as exc:
            logger.warning("Runtime key store lookup failed: %s", exc)

    logger.warning(
        "API key authentication failed (key prefix=%s…)",
        raw_key[:6] if len(raw_key) >= 6 else raw_key,
    )
    raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Public authentication dependencies
# ---------------------------------------------------------------------------

async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    config=Depends(get_config),
) -> str:
    """
    Validate the ``Authorization: Bearer <key>`` header.

    Returns the associated ``user_id`` on success.
    Raises ``HTTPException(401)`` on missing / invalid key.
    """
    key_store = getattr(request.app.state, "key_store", None)
    entry = await _validate_key(authorization, config, key_store=key_store)
    return str(getattr(entry, "user_id", "unknown"))


async def require_api_key_entry(
    request: Request,
    authorization: str | None = Header(default=None),
    config=Depends(get_config),
) -> ApiKeyEntry:
    """
    Validate the ``Authorization: Bearer <key>`` header.

    Returns the full ``ApiKeyEntry`` on success (key, user_id, namespaces).
    Raises ``HTTPException(401)`` on missing / invalid key.
    """
    key_store = getattr(request.app.state, "key_store", None)
    return await _validate_key(authorization, config, key_store=key_store)


# ---------------------------------------------------------------------------
# Namespace access control
# ---------------------------------------------------------------------------

async def check_namespace_access(
    key_entry: ApiKeyEntry,
    namespace: str,
    operation: str = "read",
) -> None:
    """
    Assert that *key_entry* is permitted to operate on *namespace*.

    Access rules (evaluated in order — first match wins):

    1. ``operation="write"`` and ``key_entry.read_only is True``  →  deny (403).
    2. ``"*"`` in the key's namespaces list  →  allow everything.
    3. Exact match: ``namespace`` is listed verbatim  →  allow.
    4. Prefix wildcard: the key lists ``"prefix:*"`` and *namespace* starts
       with ``"prefix:"``  →  allow (e.g. key has ``"personal:*"`` and the
       requested namespace is ``"personal:thameema"``).
    5. No match  →  raise ``HTTPException(403)``.

    Parameters
    ----------
    key_entry:
        The ``ApiKeyEntry`` returned by ``require_api_key_entry()``.
    namespace:
        The namespace value extracted from the incoming request.
    operation:
        Either ``"read"`` (default) or ``"write"``.  Write operations are
        rejected when the key's ``read_only`` flag is ``True``.
    """
    # Rule 1 — read-only key cannot perform write/delete operations
    if operation == "write" and getattr(key_entry, "read_only", False):
        user_id = getattr(key_entry, "user_id", "unknown")
        logger.warning("Write denied for read-only key: user=%s namespace=%r", user_id, namespace)
        raise HTTPException(status_code=403, detail="API key is read-only")

    allowed: list[str] = getattr(key_entry, "namespaces", []) or []

    # Rule 2 — wildcard: key can access everything
    if "*" in allowed:
        return

    # Rule 3 — exact match
    if namespace in allowed:
        return

    # Rule 4 — prefix wildcard  e.g. "personal:*" matches "personal:thameema"
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


async def check_vault_access(
    key_entry: ApiKeyEntry,
    namespace: str,
    required: str = "vault_read",
) -> None:
    """Assert that *key_entry* has vault permission on *namespace*.

    Permission hierarchy: vault_admin > vault_write > vault_read.

    Keys with ``"*"`` in their namespaces list are implicitly vault_admin.
    Keys must otherwise have an explicit entry in vault_namespaces.
    """
    _LEVELS = {"vault_read": 1, "vault_write": 2, "vault_admin": 3}
    required_level = _LEVELS.get(required, 1)

    # Wildcard keys are implicitly vault_admin everywhere
    allowed_ns: list[str] = getattr(key_entry, "namespaces", []) or []
    if "*" in allowed_ns:
        return

    vault_ns = getattr(key_entry, "vault_namespaces", []) or []
    for entry in vault_ns:
        ns = getattr(entry, "namespace", "")
        access = getattr(entry, "access", "vault_read")
        entry_level = _LEVELS.get(access, 1)

        # Exact match or prefix match (org:acme:* covers org:acme:eng)
        ns_matches = (
            ns == namespace
            or ns == "*"
            or (ns.endswith(":*") and namespace.startswith(ns[:-1]))
        )
        if ns_matches and entry_level >= required_level:
            return

    user_id = getattr(key_entry, "user_id", "unknown")
    logger.warning(
        "Vault access denied: user=%s namespace=%r required=%s",
        user_id, namespace, required,
    )
    raise HTTPException(
        status_code=403,
        detail=f"API key does not have {required} access to vault namespace '{namespace}'",
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
