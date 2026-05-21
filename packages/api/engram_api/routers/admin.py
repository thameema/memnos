"""
engram_api.routers.admin — Health-check, namespace management, and diagnostics.

Endpoints
---------
GET    /admin/health              — check Neo4j and Qdrant connectivity
GET    /admin/namespaces          — list all configured namespaces
POST   /admin/namespaces          — create a new namespace definition
DELETE /admin/namespaces/{ns}     — delete a namespace and all its data
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from engram_api.auth import get_client, get_config, require_api_key
from engram_api.schemas import HealthResponse, NamespaceCreateRequest

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
    neo4j_status = "unknown"
    qdrant_status = "unknown"
    overall = "ok"

    # --- Neo4j probe ---
    try:
        graph_client = getattr(client, "_graph", None) or getattr(client, "graph", None)
        if graph_client is not None and hasattr(graph_client, "ping"):
            await graph_client.ping()
            neo4j_status = "ok"
        else:
            # Attempt a trivial Cypher query as a probe
            result = await client.query_graph("RETURN 1 AS n", "health:probe", {})
            neo4j_status = "ok" if result is not None else "degraded"
    except Exception as exc:
        logger.warning("Neo4j health probe failed: %s", exc)
        neo4j_status = f"error: {exc}"
        overall = "degraded"

    # --- Qdrant probe ---
    try:
        vector_client = (
            getattr(client, "_vector", None) or getattr(client, "vector", None)
        )
        if vector_client is not None and hasattr(vector_client, "ping"):
            await vector_client.ping()
            qdrant_status = "ok"
        else:
            # Try collections list as a probe
            qdrant_raw = getattr(vector_client, "_client", None)
            if qdrant_raw and hasattr(qdrant_raw, "get_collections"):
                qdrant_raw.get_collections()
            qdrant_status = "ok"
    except Exception as exc:
        logger.warning("Qdrant health probe failed: %s", exc)
        qdrant_status = f"error: {exc}"
        overall = "degraded"

    return HealthResponse(
        status=overall,
        neo4j=neo4j_status,
        qdrant=qdrant_status,
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
# Namespace creation
# ---------------------------------------------------------------------------

@router.post("/namespaces", status_code=201)
async def create_namespace(
    req: NamespaceCreateRequest,
    user_id: str = Depends(require_api_key),
    config=Depends(get_config),
) -> dict:
    """
    Register a new namespace definition in the running configuration.

    Note: this updates the in-memory configuration only.  Persist by editing
    ``engram.yaml`` for durability across restarts.
    """
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
# Namespace deletion
# ---------------------------------------------------------------------------

@router.delete("/namespaces/{ns}", status_code=204)
async def delete_namespace(
    ns: str,
    user_id: str = Depends(require_api_key),
    config=Depends(get_config),
    client=Depends(get_client),
) -> None:
    """
    Delete a namespace definition and optionally purge all its data.

    **Warning:** this is a destructive operation.  All memories, entities,
    facts, and relations stored under ``ns`` will be permanently removed.
    """
    ns_config = getattr(config, "namespaces", None)
    definitions = getattr(ns_config, "definitions", {}) if ns_config else {}

    if ns not in definitions:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace {ns!r} not found",
        )

    # Attempt to purge vector store data for the namespace
    try:
        vector_client = (
            getattr(client, "_vector", None) or getattr(client, "vector", None)
        )
        if vector_client and hasattr(vector_client, "delete_namespace"):
            await vector_client.delete_namespace(ns)
    except Exception as exc:
        logger.warning("Failed to purge vector data for namespace %s: %s", ns, exc)

    # Attempt to purge graph data for the namespace
    try:
        await client.query_graph(
            "MATCH (n {namespace: $ns}) DETACH DELETE n",
            ns,
            {"ns": ns},
        )
    except Exception as exc:
        logger.warning("Failed to purge graph data for namespace %s: %s", ns, exc)

    # Remove from in-memory config
    del definitions[ns]
    logger.info("Namespace deleted: %s by user %s", ns, user_id)
