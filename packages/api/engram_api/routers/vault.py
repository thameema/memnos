"""
engram_api.routers.vault — Encrypted secrets vault endpoints.

Endpoints
---------
POST   /vault/secrets              — store a secret (vault_write)
GET    /vault/secrets              — list secret metadata, no values (vault_read)
GET    /vault/secrets/{key_name}   — retrieve decrypted value (vault_read)
PUT    /vault/secrets/{key_name}/rotate — rotate a secret (vault_write)
DELETE /vault/secrets/{key_name}   — supersede/retire a secret (vault_write)
GET    /vault/audit                — audit log (vault_admin)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from engram_api.auth import (
    check_vault_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vault", tags=["vault"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class SecretSetRequest(BaseModel):
    key_name: str
    value: str
    namespace: str
    secret_type: str = "api_key"
    note: str = ""
    tags: list[str] = []


class SecretRotateRequest(BaseModel):
    new_value: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/secrets", status_code=201)
async def set_secret(
    req: SecretSetRequest,
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Store (or replace) a secret.  Requires ``vault_write`` permission."""
    await check_vault_access(key_entry, req.namespace, required="vault_write")
    logger.debug("vault/set | key=%s ns=%s user=%s", req.key_name, req.namespace, user_id)
    try:
        return await client.secret_set(
            key_name=req.key_name,
            value=req.value,
            namespace=req.namespace,
            secret_type=req.secret_type,
            note=req.note,
            created_by=user_id,
            tags=req.tags,
        )
    except Exception as exc:
        logger.exception("vault set failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/secrets")
async def list_secrets(
    namespace: str = Query(..., description="Namespace to list secrets from"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[dict]:
    """List secret metadata (no values) for a namespace.  Requires ``vault_read``."""
    await check_vault_access(key_entry, namespace, required="vault_read")
    logger.debug("vault/list | ns=%s user=%s", namespace, user_id)
    try:
        return await client.secret_list(namespace=namespace, accessed_by=user_id)
    except Exception as exc:
        logger.exception("vault list failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/secrets/{key_name}")
async def get_secret(
    key_name: str,
    namespace: str = Query(..., description="Namespace that owns the secret"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Retrieve the decrypted value of a secret.  Requires ``vault_read``."""
    await check_vault_access(key_entry, namespace, required="vault_read")
    logger.debug("vault/get | key=%s ns=%s user=%s", key_name, namespace, user_id)
    try:
        value = await client.secret_get(
            key_name=key_name, namespace=namespace, accessed_by=user_id
        )
        return {"key_name": key_name, "namespace": namespace, "value": value}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("vault get failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/secrets/{key_name}/rotate", status_code=200)
async def rotate_secret(
    key_name: str,
    req: SecretRotateRequest,
    namespace: str = Query(..., description="Namespace that owns the secret"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Re-encrypt a secret with a new value and fresh DEK.  Requires ``vault_write``."""
    await check_vault_access(key_entry, namespace, required="vault_write")
    logger.debug("vault/rotate | key=%s ns=%s user=%s", key_name, namespace, user_id)
    try:
        return await client.secret_rotate(
            key_name=key_name,
            new_value=req.new_value,
            namespace=namespace,
            accessed_by=user_id,
        )
    except Exception as exc:
        logger.exception("vault rotate failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/secrets/{key_name}", status_code=200)
async def delete_secret(
    key_name: str,
    namespace: str = Query(..., description="Namespace that owns the secret"),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> dict:
    """Supersede (retire) a secret.  Requires ``vault_write``.

    The record is soft-deleted — history is preserved in the audit log.
    """
    await check_vault_access(key_entry, namespace, required="vault_write")
    logger.debug("vault/delete | key=%s ns=%s user=%s", key_name, namespace, user_id)
    try:
        existing = await client._arcadedb.get_secret(key_name, namespace)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Secret '{key_name}' not found in namespace '{namespace}'",
            )
        ok = await client._arcadedb.supersede_secret(existing.id, namespace)
        return {"key_name": key_name, "namespace": namespace, "superseded": ok}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("vault delete failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/audit")
async def get_audit_log(
    namespace: str = Query(..., description="Namespace to fetch audit log for"),
    limit: int = Query(100, ge=1, le=1000),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
) -> list[dict]:
    """Return immutable vault audit log entries.  Requires ``vault_admin``."""
    await check_vault_access(key_entry, namespace, required="vault_admin")
    logger.debug("vault/audit | ns=%s user=%s limit=%d", namespace, user_id, limit)
    try:
        return await client.secret_audit(namespace=namespace, limit=limit)
    except Exception as exc:
        logger.exception("vault audit failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
