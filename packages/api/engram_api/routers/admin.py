"""
engram_api.routers.admin — Health-check, namespace management, and diagnostics.

Endpoints
---------
GET    /admin/health              — check ArcadeDB connectivity
GET    /admin/namespaces          — list all configured namespaces
POST   /admin/namespaces          — create a new namespace definition  [admin only]
DELETE /admin/namespaces/{ns}     — delete a namespace and all its data [admin only]
GET    /admin/export              — export all memories in a namespace (JSON or CSV)
POST   /admin/import              — import memories from an export envelope

Admin operations (create/delete namespace) require an API key with wildcard ("*")
namespace access.  Keys scoped to specific namespaces will receive HTTP 403.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from engram_api.auth import (
    check_namespace_access,
    get_client,
    get_config,
    require_admin_access,
    require_api_key,
    require_api_key_entry,
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
        schema_version="1.0",
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


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def _memory_to_dict(memory) -> dict:
    """Convert a MemoryEntry to a plain dict suitable for JSON serialisation."""
    prov_dict = {}
    if memory.provenance:
        prov_dict = memory.provenance.model_dump() if hasattr(memory.provenance, "model_dump") else {}
    return {
        "id": str(memory.id),
        "content": memory.content,
        "namespace": memory.namespace,
        "created_at": memory.created_at.isoformat() if isinstance(memory.created_at, datetime) else str(memory.created_at),
        "tags": list(memory.tags or []),
        "score": None,
        "memory_type": memory.memory_type.value if hasattr(memory.memory_type, "value") else str(memory.memory_type),
        "author": getattr(memory, "author", "") or "",
        "affects": list(getattr(memory, "affects", None) or []),
        "rationale": getattr(memory, "rationale", "") or "",
        "provenance": prov_dict,
        "contradiction_warnings": [],
    }


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _ns_slug(namespace: str) -> str:
    return namespace.replace(":", "-").replace("/", "-")


@router.get("/export")
async def export_namespace(
    ns: str = Query(..., description="Namespace to export"),
    format: str = Query("json", description="json or csv"),
    memory_type: str | None = Query(None, description="Filter to a specific memory_type"),
    include_superseded: bool = Query(False, description="Include superseded memories"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> StreamingResponse:
    """Export all memories in a namespace as a JSON envelope or CSV file."""
    await check_namespace_access(key_entry, ns)
    logger.debug("export_namespace | ns=%s format=%s user=%s", ns, format, user_id)

    try:
        memories = await client._arcadedb.scan_namespace(
            ns,
            memory_type=memory_type,
            include_superseded=include_superseded,
        )
    except Exception as exc:
        logger.exception("scan_namespace failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    slug = _ns_slug(ns)
    timestamp = int(time.time())

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "content", "namespace", "memory_type", "tags", "affects",
                          "rationale", "author", "created_at", "score"])
        for mem in memories:
            created = mem.created_at.isoformat() if isinstance(mem.created_at, datetime) else str(mem.created_at)
            writer.writerow([
                str(mem.id),
                mem.content,
                mem.namespace,
                mem.memory_type.value if hasattr(mem.memory_type, "value") else str(mem.memory_type),
                json.dumps(list(mem.tags or [])),
                json.dumps(list(getattr(mem, "affects", None) or [])),
                getattr(mem, "rationale", "") or "",
                getattr(mem, "author", "") or "",
                created,
                "",
            ])

        filename = f"{slug}-{timestamp}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    # JSON format
    envelope = {
        "engram_export": "1.0",
        "exported_at": int(time.time() * 1000),
        "namespace": ns,
        "count": len(memories),
        "memories": [_memory_to_dict(m) for m in memories],
    }
    body = json.dumps(envelope, default=_json_default, ensure_ascii=False)
    filename = f"{slug}-{timestamp}.json"
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/import")
async def import_namespace(
    body: dict,
    ns: str | None = Query(None, description="Override namespace for all imported memories"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Import memories from a JSON export envelope produced by GET /admin/export."""
    if body.get("engram_export") != "1.0":
        raise HTTPException(status_code=400, detail="Invalid export envelope: engram_export must be '1.0'")

    memories_raw = body.get("memories") or []
    target_ns = ns or body.get("namespace") or ""
    if not target_ns:
        raise HTTPException(status_code=400, detail="Target namespace not specified")

    await check_namespace_access(key_entry, target_ns, operation="write")
    logger.debug("import_namespace | ns=%s count=%d user=%s", target_ns, len(memories_raw), user_id)

    from engram.models import MemoryType, MemoryStatus

    imported = 0
    skipped = 0
    for item in memories_raw:
        try:
            raw_type = item.get("memory_type", "fact")
            try:
                mem_type = MemoryType(raw_type)
            except ValueError:
                mem_type = MemoryType.fact

            await client.add(
                content=item.get("content", ""),
                namespace=target_ns,
                tags=item.get("tags") or [],
                source="import",
                memory_type=mem_type,
                author=item.get("author") or "",
                affects=item.get("affects") or [],
                rationale=item.get("rationale") or "",
            )
            imported += 1
        except Exception as exc:
            logger.warning("import_namespace: skipped record id=%s: %s", item.get("id"), exc)
            skipped += 1

    return {"imported": imported, "skipped": skipped, "namespace": target_ns}
