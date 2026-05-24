"""
engram_api.routers.admin — Health-check, namespace management, and diagnostics.

Endpoints
---------
GET    /admin/health              — check ArcadeDB connectivity
GET    /admin/namespaces          — list all configured namespaces
POST   /admin/namespaces          — create a new namespace definition  [admin only]
DELETE /admin/namespaces/{ns}     — delete a namespace and all its data [admin only]

Admin operations (create/delete namespace) require an API key with wildcard ("*")
namespace access.  Keys scoped to specific namespaces will receive HTTP 403.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from engram_api.auth import (
    get_client,
    get_config,
    require_admin_access,
    require_api_key,
)
from engram_api.schemas import HealthResponse, KeyCreateRequest, KeyResponse, NamespaceCreateRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health_check(
    client=Depends(get_client),
    config=Depends(get_config),
) -> HealthResponse:
    """
    Check the health of all backing services.

    This endpoint is intentionally *unauthenticated* so load-balancer health
    probes work without credentials.  (Auth middleware must whitelist this path.)
    """
    arcadedb_status = "unknown"
    overall = "ok"

    try:
        result = await client.query_graph("SELECT 1 AS n FROM Memory LIMIT 1", "health:probe")
        arcadedb_status = "ok" if result is not None else "degraded"
    except Exception as exc:
        logger.warning("ArcadeDB health probe failed: %s", exc)
        arcadedb_status = f"error: {exc}"
        overall = "degraded"

    return HealthResponse(
        status=overall,
        arcadedb=arcadedb_status,
    )


# ---------------------------------------------------------------------------
# Namespace listing
# ---------------------------------------------------------------------------

@router.get("/namespaces")
async def list_namespaces(
    user_id: str = Depends(require_api_key),
    config=Depends(get_config),
) -> list[dict]:
    """List all namespace definitions from the current configuration."""
    ns_config = getattr(config, "namespaces", None)
    if ns_config is None:
        return []

    definitions = getattr(ns_config, "definitions", {}) or {}
    result = []
    for name, defn in definitions.items():
        result.append({
            "name": name,
            "owners": list(getattr(defn, "owners", [])),
            "readers": list(getattr(defn, "readers", [])),
            "writers": list(getattr(defn, "writers", [])),
        })

    return result


# ---------------------------------------------------------------------------
# Namespace creation  [admin only — requires wildcard ("*") key]
# ---------------------------------------------------------------------------

@router.post("/namespaces", status_code=201)
async def create_namespace(
    req: NamespaceCreateRequest,
    key_entry=Depends(require_admin_access),
    config=Depends(get_config),
) -> dict:
    """
    Register a new namespace definition in the running configuration.

    Requires an API key with wildcard (``"*"``) namespace access.

    Note: this updates the in-memory configuration only.  Persist by editing
    ``engram.yaml`` for durability across restarts.
    """
    user_id = getattr(key_entry, "user_id", "unknown")
    ns_config = getattr(config, "namespaces", None)
    if ns_config is None:
        raise HTTPException(status_code=500, detail="Namespace config not available")

    definitions = getattr(ns_config, "definitions", None)
    if definitions is None:
        raise HTTPException(status_code=500, detail="Namespace definitions not available")

    if req.name in definitions:
        raise HTTPException(
            status_code=409,
            detail=f"Namespace {req.name!r} already exists",
        )

    from engram.config import NamespaceDefinition  # type: ignore

    definitions[req.name] = NamespaceDefinition(
        owners=req.owners,
        readers=req.readers,
        writers=req.writers,
    )
    logger.info("Namespace created: %s by user %s", req.name, user_id)

    return {
        "name": req.name,
        "owners": req.owners,
        "readers": req.readers,
        "writers": req.writers,
    }


# ---------------------------------------------------------------------------
# Namespace deletion  [admin only — requires wildcard ("*") key]
# ---------------------------------------------------------------------------

@router.delete("/namespaces/{ns}", status_code=204, response_model=None)
async def delete_namespace(
    ns: str,
    key_entry=Depends(require_admin_access),
    config=Depends(get_config),
    client=Depends(get_client),
) -> None:
    """
    Delete a namespace definition and optionally purge all its data.

    Requires an API key with wildcard (``"*"``) namespace access.

    **Warning:** this is a destructive operation.  All memories, entities,
    facts, and relations stored under ``ns`` will be permanently removed.
    """
    user_id = getattr(key_entry, "user_id", "unknown")
    ns_config = getattr(config, "namespaces", None)
    definitions = getattr(ns_config, "definitions", {}) if ns_config else {}

    if ns not in definitions:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace {ns!r} not found",
        )

    # Purge all Memory + Entity + Fact vertices for the namespace
    for type_name in ("Memory", "Entity", "Fact", "Asset"):
        try:
            await client.query_graph(
                f"DELETE VERTEX {type_name} WHERE namespace = :namespace",
                ns,
            )
        except Exception as exc:
            logger.warning("Failed to purge %s data for namespace %s: %s", type_name, ns, exc)

    # Remove from in-memory config
    del definitions[ns]
    logger.info("Namespace deleted: %s by user %s", ns, user_id)


# ---------------------------------------------------------------------------
# Runtime key management  [admin only — requires wildcard ("*") key]
# ---------------------------------------------------------------------------

@router.get("/keys", response_model=list[KeyResponse])
async def list_keys(
    request: Request,
    key_entry=Depends(require_admin_access),
) -> list[KeyResponse]:
    """
    List all runtime API keys (active and revoked).

    The ``key_hash`` column is never included in the response.  The
    ``key`` field is always ``None`` here — it is only returned once at
    creation time.

    Requires admin access (wildcard ``"*"`` namespace permission).
    """
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(status_code=503, detail="Runtime key store not available")

    rows = await key_store.list_keys()
    return [KeyResponse(**row) for row in rows]


@router.post("/keys", response_model=KeyResponse, status_code=201)
async def create_key(
    req: KeyCreateRequest,
    request: Request,
    key_entry=Depends(require_admin_access),
) -> KeyResponse:
    """
    Create a new runtime API key.

    The plaintext ``key`` value is included **once** in the response and is
    never retrievable again.  Store it securely immediately.

    Requires admin access (wildcard ``"*"`` namespace permission).
    """
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(status_code=503, detail="Runtime key store not available")

    result = await key_store.create(
        user_id=req.user_id,
        namespaces=req.namespaces,
        read_only=req.read_only,
        description=req.description,
    )
    return KeyResponse(**result)


@router.delete("/keys/{key_id}", status_code=204, response_model=None)
async def revoke_key(
    key_id: str,
    request: Request,
    key_entry=Depends(require_admin_access),
) -> None:
    """
    Revoke (soft-delete) a runtime API key by ID.

    The key row is retained in the database with ``revoked_at`` set so that
    audit history is preserved.  Revoked keys are immediately rejected by the
    authentication layer.

    Requires admin access (wildcard ``"*"`` namespace permission).
    """
    key_store = getattr(request.app.state, "key_store", None)
    if key_store is None:
        raise HTTPException(status_code=503, detail="Runtime key store not available")

    revoked = await key_store.revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"Key {key_id!r} not found or already revoked",
        )
