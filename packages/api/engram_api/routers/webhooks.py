"""
engram_api.routers.webhooks — Incident webhook receiver.

Accepts incoming incident notifications from PagerDuty, AlertManager, or any
generic JSON payload and writes a memory_type=incident record. Creates
SIMILAR_TO graph edges to related past incidents automatically.

Endpoints
---------
POST /webhooks/incident            — receive a new incident trigger
POST /webhooks/incident/{id}/resolve — mark an incident resolved (RESOLVED_BY edge)

Authentication
--------------
Optional header X-Engram-Webhook-Secret must match ENGRAM_WEBHOOK_SECRET env var
when that env var is set.  If not set, the endpoint is unauthenticated (deploy
behind your network perimeter in that case).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from engram_api.auth import get_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_WEBHOOK_SECRET = os.environ.get("ENGRAM_WEBHOOK_SECRET", "")
_SIMILARITY_THRESHOLD = 0.75
_DEFAULT_NAMESPACE   = os.environ.get("ENGRAM_NAMESPACE", "org:default")


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------

def _normalise_pagerduty(body: dict) -> dict | None:
    """Extract standard fields from a PagerDuty v3 event."""
    event = body.get("event") or {}
    data = event.get("data") or {}
    event_type = event.get("event_type", "")
    if "trigger" in event_type or "alert" in event_type:
        status_val = "firing"
    elif "resolve" in event_type:
        status_val = "resolved"
    else:
        status_val = "unknown"
    return {
        "title":       data.get("title") or data.get("summary") or "PagerDuty incident",
        "description": data.get("description") or data.get("body", {}).get("details", ""),
        "severity":    (data.get("severity") or "").upper() or "UNKNOWN",
        "status":      status_val,
        "source":      "pagerduty",
        "external_id": data.get("id") or event.get("id") or "",
    }


def _normalise_alertmanager(body: dict) -> dict | None:
    """Extract standard fields from an AlertManager v2 webhook."""
    alerts = body.get("alerts") or []
    if not alerts:
        return None
    first = alerts[0]
    labels = first.get("labels") or {}
    annotations = first.get("annotations") or {}
    raw_status = first.get("status", "firing").lower()
    return {
        "title":       labels.get("alertname") or annotations.get("summary") or "AlertManager alert",
        "description": annotations.get("description") or annotations.get("summary") or "",
        "severity":    (labels.get("severity") or "").upper() or "UNKNOWN",
        "status":      "resolved" if raw_status == "resolved" else "firing",
        "source":      "alertmanager",
        "external_id": labels.get("alertname") or "",
    }


def _normalise_generic(body: dict) -> dict:
    """Passthrough for generic JSON incident payloads."""
    return {
        "title":       str(body.get("title") or body.get("name") or "Incident"),
        "description": str(body.get("description") or body.get("message") or ""),
        "severity":    (str(body.get("severity") or "")).upper() or "UNKNOWN",
        "status":      str(body.get("status") or "firing").lower(),
        "source":      str(body.get("source") or "webhook"),
        "external_id": str(body.get("id") or body.get("external_id") or ""),
    }


def normalise_webhook_payload(body: dict) -> dict:
    """Detect provider and return normalised incident dict."""
    if "event" in body and "data" in body.get("event", {}):
        return _normalise_pagerduty(body) or _normalise_generic(body)
    if "alerts" in body:
        return _normalise_alertmanager(body) or _normalise_generic(body)
    return _normalise_generic(body)


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------

def _verify_webhook_secret(secret_header: str | None) -> None:
    if not _WEBHOOK_SECRET:
        return  # secret not configured — open endpoint
    if not secret_header:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Engram-Webhook-Secret")
    if not hmac.compare_digest(secret_header, _WEBHOOK_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PastIncidentSummary(BaseModel):
    memory_id: str
    content: str
    severity: str
    similarity: float
    created_at: str
    resolution: str = ""   # content of RESOLVED_BY memory if one exists


class IncidentWebhookResponse(BaseModel):
    memory_id: str
    namespace: str
    severity: str
    similar_incidents: list[str]          # backward-compat IDs
    past_incidents: list[PastIncidentSummary] = []   # enriched with full content


class ResolveRequest(BaseModel):
    resolution_notes: str = ""
    resolver: str = ""
    namespace: str = _DEFAULT_NAMESPACE


class ResolveResponse(BaseModel):
    incident_id: str
    resolution_memory_id: str
    namespace: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/incident", response_model=IncidentWebhookResponse, status_code=201)
async def receive_incident(
    request: Request,
    namespace: str = _DEFAULT_NAMESPACE,
    x_engram_webhook_secret: str | None = Header(default=None),
    client=Depends(get_client),
) -> IncidentWebhookResponse:
    """
    Receive an incident notification and write it as memory_type=incident.

    Automatically links to similar past incidents via SIMILAR_TO graph edges.
    Supports PagerDuty v3, AlertManager v2, and generic JSON payloads.
    """
    _verify_webhook_secret(x_engram_webhook_secret)

    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    incident = normalise_webhook_payload(body)
    title       = incident["title"]
    description = incident["description"]
    severity    = incident["severity"]
    inc_status  = incident["status"]
    source_name = incident["source"]
    external_id = incident["external_id"]

    content_parts = [f"Incident: {title}", f"Severity: {severity}"]
    if description:
        content_parts.append(f"Description: {description}")
    if external_id:
        content_parts.append(f"External ID: {external_id}")
    content = "\n".join(content_parts)

    from engram.models import MemoryType, MemoryStatus
    memory = await client.add(
        content=content,
        namespace=namespace,
        tags=["incident", severity.lower(), source_name],
        source=f"webhook:{source_name}",
        memory_type=MemoryType.incident,
        status=MemoryStatus.active,
        metadata={
            "severity": severity,
            "external_id": external_id,
            "provider": source_name,
            "incident_status": inc_status,
        },
    )

    # Link to similar past incidents via SIMILAR_TO edges (best-effort)
    similar_ids: list[str] = []
    past_incidents: list[PastIncidentSummary] = []
    try:
        embedding = await client._embedder.embed(content)
        pairs = await client._arcadedb.find_similar_incidents(
            namespace, embedding, exclude_id=str(memory.id),
            top_k=5, threshold=_SIMILARITY_THRESHOLD,
        )
        for sim_id, sim_score in pairs:
            await client._arcadedb.create_similar_to_edge(
                str(memory.id), sim_id, namespace, similarity=sim_score
            )
            similar_ids.append(sim_id)
            logger.debug("SIMILAR_TO edge: %s → %s (%.2f)", memory.id, sim_id, sim_score)

            # Fetch full content for the enriched response
            past_mem = await client._arcadedb.get_memory(sim_id, namespace)
            if past_mem is not None:
                past_incidents.append(PastIncidentSummary(
                    memory_id=sim_id,
                    content=past_mem.content,
                    severity=past_mem.metadata.get("severity", "UNKNOWN") if past_mem.metadata else "UNKNOWN",
                    similarity=round(sim_score, 3),
                    created_at=past_mem.created_at.isoformat() if past_mem.created_at else "",
                ))
    except Exception as exc:
        logger.warning("SIMILAR_TO edge creation failed (non-fatal): %s", exc)

    return IncidentWebhookResponse(
        memory_id=str(memory.id),
        namespace=namespace,
        severity=severity,
        similar_incidents=similar_ids,
        past_incidents=past_incidents,
    )


@router.post("/incident/{incident_id}/resolve", response_model=ResolveResponse, status_code=201)
async def resolve_incident(
    incident_id: str,
    req: ResolveRequest,
    x_engram_webhook_secret: str | None = Header(default=None),
    client=Depends(get_client),
) -> ResolveResponse:
    """
    Mark an incident as resolved. Writes a resolution memory and creates a
    RESOLVED_BY edge from the original incident to the resolution record.
    """
    _verify_webhook_secret(x_engram_webhook_secret)
    namespace = req.namespace

    original = await client._arcadedb.get_memory(incident_id, namespace)
    if original is None:
        raise HTTPException(status_code=404, detail=f"Incident memory {incident_id!r} not found in {namespace!r}")

    notes = req.resolution_notes or "Incident resolved."
    resolver = req.resolver or "webhook"
    content = (
        f"Resolution for incident: {original.content[:200]}\n"
        f"Resolver: {resolver}\n"
        f"Notes: {notes}"
    )

    from engram.models import MemoryType
    resolution = await client.add(
        content=content,
        namespace=namespace,
        tags=["incident", "resolution"],
        source="webhook:resolve",
        memory_type=MemoryType.incident,
        metadata={"incident_id": incident_id, "resolver": resolver},
        author=resolver,
    )

    await client._arcadedb.create_resolved_by_edge(incident_id, str(resolution.id), namespace)

    return ResolveResponse(
        incident_id=incident_id,
        resolution_memory_id=str(resolution.id),
        namespace=namespace,
    )
