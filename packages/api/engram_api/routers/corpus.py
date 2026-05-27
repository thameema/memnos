"""
engram_api.routers.corpus — Architecture corpus ingestion and constraint checking.

Endpoints
---------
POST   /corpus/              — register a corpus source and trigger initial sync
GET    /corpus/              — list all registered corpora
GET    /corpus/{id}          — get corpus status and node count
POST   /corpus/{id}/sync     — trigger re-sync (GitLab CI webhook target)
DELETE /corpus/{id}          — unregister corpus and remove its nodes
POST   /corpus/{id}/check    — return constraints relevant to a code snippet

The /sync endpoint is the GitLab CI integration point: add it as a push webhook
on the hdig-platform repo so constraint nodes stay current on every doc push.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from engram.models import Corpus
from engram_api.auth import get_client, require_api_key, require_api_key_entry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/corpus", tags=["corpus"])


# ---------------------------------------------------------------------------
# Dependency: corpus store singleton
# ---------------------------------------------------------------------------

_store: "CorpusStore | None" = None  # type: ignore[name-defined]  # noqa: F821


async def _get_store() -> "CorpusStore":  # type: ignore[name-defined]  # noqa: F821
    global _store
    if _store is None:
        from engram.corpus.store import CorpusStore
        _store = CorpusStore()
        await _store.init()
    return _store


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CorpusCreateRequest(BaseModel):
    name: str
    source_path: str
    path_pattern: str = "**/*.md"
    namespace: str
    watch: bool = False
    webhook_secret: str = ""


class CorpusResponse(BaseModel):
    id: str
    name: str
    source_path: str
    path_pattern: str
    namespace: str
    watch: bool
    last_sync_sha: str
    last_sync_at: str | None
    node_count: int
    status: str
    error_msg: str
    created_at: str
    created_by: str


class CheckRequest(BaseModel):
    code: str
    context: str = ""
    top_k: int = 10


class ConstraintHit(BaseModel):
    memory_id: str
    content: str
    severity: str
    source_file: str
    section: str
    score: float


class CheckResponse(BaseModel):
    corpus_id: str
    namespace: str
    constraints: list[ConstraintHit]


def _to_response(corpus: Corpus) -> CorpusResponse:
    return CorpusResponse(
        id=corpus.id,
        name=corpus.name,
        source_path=corpus.source_path,
        path_pattern=corpus.path_pattern,
        namespace=corpus.namespace,
        watch=corpus.watch,
        last_sync_sha=corpus.last_sync_sha,
        last_sync_at=corpus.last_sync_at.isoformat() if corpus.last_sync_at else None,
        node_count=corpus.node_count,
        status=corpus.status,
        error_msg=corpus.error_msg,
        created_at=corpus.created_at.isoformat(),
        created_by=corpus.created_by,
    )


# ---------------------------------------------------------------------------
# Background sync worker
# ---------------------------------------------------------------------------

async def _run_sync(corpus: Corpus, client, store) -> None:
    """Extract nodes from corpus source and write them to engram. Background task."""
    corpus_id = corpus.id
    logger.info("corpus sync start | id=%s source=%s ns=%s", corpus_id, corpus.source_path, corpus.namespace)
    await store.update_sync_state(corpus_id, status="syncing")

    try:
        from engram.corpus.connectors import get_connector

        connector_type = getattr(corpus, "connector_type", "git-doc") or "git-doc"
        connector = get_connector(
            connector_type,
            corpus_id=corpus_id,
            namespace=corpus.namespace,
            source_path=corpus.source_path,
            path_pattern=corpus.path_pattern,
        )
        sync_result = await connector.sync(client)

        status = "ready" if sync_result.ok else "error"
        error_msg = "; ".join(sync_result.errors) if sync_result.errors else ""
        await store.update_sync_state(
            corpus_id,
            status=status,
            node_count=sync_result.nodes_written,
            last_sync_sha=sync_result.git_sha,
            error_msg=error_msg,
        )
        logger.info(
            "corpus sync done | id=%s connector=%s nodes=%d sha=%s",
            corpus_id, connector_type, sync_result.nodes_written, sync_result.git_sha,
        )

    except Exception as exc:
        logger.exception("corpus sync failed | id=%s: %s", corpus_id, exc)
        await store.update_sync_state(corpus_id, status="error", error_msg=str(exc))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/", response_model=CorpusResponse, status_code=201)
async def create_corpus(
    req: CorpusCreateRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_api_key),
    client=Depends(get_client),
    store=Depends(_get_store),
) -> CorpusResponse:
    """Register a corpus source and trigger initial ingestion in the background."""
    corpus = Corpus(
        name=req.name,
        source_path=req.source_path,
        path_pattern=req.path_pattern,
        namespace=req.namespace,
        watch=req.watch,
        webhook_secret=req.webhook_secret,
        created_by=user_id,
    )
    await store.create(corpus)
    background_tasks.add_task(_run_sync, corpus, client, store)
    logger.info("corpus registered | id=%s name=%s by=%s", corpus.id, corpus.name, user_id)
    return _to_response(corpus)


@router.get("/", response_model=list[CorpusResponse])
async def list_corpora(
    user_id: str = Depends(require_api_key),
    store=Depends(_get_store),
) -> list[CorpusResponse]:
    """List all registered corpus sources."""
    corpora = await store.list_all()
    return [_to_response(c) for c in corpora]


@router.get("/{corpus_id}", response_model=CorpusResponse)
async def get_corpus(
    corpus_id: str,
    user_id: str = Depends(require_api_key),
    store=Depends(_get_store),
) -> CorpusResponse:
    corpus = await store.get(corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"Corpus {corpus_id!r} not found")
    return _to_response(corpus)


@router.post("/{corpus_id}/sync", response_model=CorpusResponse)
async def sync_corpus(
    corpus_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    user_id: str = Depends(require_api_key),
    client=Depends(get_client),
    store=Depends(_get_store),
) -> CorpusResponse:
    """Re-sync corpus nodes from source. Used as a GitLab CI push webhook.

    When watch=True on the corpus, configure your GitLab repo to POST here on
    push events.  Set webhook_secret on the corpus and pass it as
    X-Gitlab-Token (GitLab) or X-Hub-Signature-256 (GitHub) for verification.
    """
    corpus = await store.get(corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"Corpus {corpus_id!r} not found")

    # Webhook secret verification (optional — only enforced when secret is set)
    if corpus.webhook_secret:
        gitlab_ok = x_gitlab_token and hmac.compare_digest(x_gitlab_token, corpus.webhook_secret)
        github_ok = False
        if x_hub_signature_256 and not gitlab_ok:
            try:
                body = await request.body()
                expected = "sha256=" + hmac.new(
                    corpus.webhook_secret.encode(), body, hashlib.sha256
                ).hexdigest()
                github_ok = hmac.compare_digest(x_hub_signature_256, expected)
            except Exception:
                pass
        if not gitlab_ok and not github_ok:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if corpus.status == "syncing":
        raise HTTPException(status_code=409, detail="Sync already in progress")

    background_tasks.add_task(_run_sync, corpus, client, store)
    logger.info("corpus sync triggered | id=%s user=%s", corpus_id, user_id)
    return _to_response(corpus)


@router.delete("/{corpus_id}", status_code=204, response_model=None)
async def delete_corpus(
    corpus_id: str,
    user_id: str = Depends(require_api_key),
    store=Depends(_get_store),
) -> None:
    """Unregister a corpus. Does not delete the ingested memory nodes."""
    deleted = await store.delete(corpus_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Corpus {corpus_id!r} not found")
    logger.info("corpus deleted | id=%s user=%s", corpus_id, user_id)


@router.post("/{corpus_id}/check", response_model=CheckResponse)
async def check_corpus(
    corpus_id: str,
    req: CheckRequest,
    user_id: str = Depends(require_api_key),
    client=Depends(get_client),
    store=Depends(_get_store),
) -> CheckResponse:
    """Return constraints from this corpus relevant to a code snippet.

    Searches the corpus namespace for constraint nodes whose content is
    semantically similar to the provided code + context string.  The calling
    agent (Claude) determines which returned constraints are actually violated.

    Example::

        POST /corpus/{id}/check
        {"code": "@Cacheable\\npublic Mono<Void> filter(...)",
         "context": "patient-access consent validation filter"}
    """
    corpus = await store.get(corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"Corpus {corpus_id!r} not found")

    if corpus.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Corpus is not ready (status={corpus.status!r}). Wait for sync to complete.",
        )

    # Build a rich query: context + code keywords stripped of noise
    query = req.context
    if req.code:
        # Extract identifiers from the code snippet for semantic matching
        identifiers = set(re.findall(r'\b[A-Za-z][A-Za-z0-9_]{3,}\b', req.code))
        # Drop Java/Python keywords
        _KW = {"public", "private", "protected", "void", "return", "class",
               "import", "from", "async", "await", "final", "static", "new",
               "this", "super", "null", "true", "false", "override", "throws"}
        identifiers -= _KW
        if identifiers:
            query = f"{query} {' '.join(list(identifiers)[:15])}"

    try:
        results = await client.search(
            query.strip() or "constraint SHALL MUST architecture",
            corpus.namespace,
            top_k=req.top_k * 2,
            mode="hybrid",
        )
    except Exception as exc:
        logger.exception("corpus check search failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Filter to constraint/decision nodes from this corpus
    corpus_tag = f"corpus:{corpus_id}"
    hits: list[ConstraintHit] = []
    for r in results:
        mem = r.memory
        if corpus_tag not in (mem.tags or []):
            continue
        if mem.memory_type.value not in ("constraint", "decision"):
            continue
        if r.score < 0.45:
            continue

        meta = mem.metadata or {}
        severity = meta.get("severity", "")
        # Fall back: parse severity from content prefix
        if not severity and mem.content.startswith("[CONSTRAINT|"):
            m = re.match(r'\[CONSTRAINT\|([^\]]+)\]', mem.content)
            if m:
                severity = m.group(1)

        hits.append(ConstraintHit(
            memory_id=str(mem.id),
            content=mem.content,
            severity=severity,
            source_file=meta.get("source_file", ""),
            section=meta.get("section", ""),
            score=round(r.score, 3),
        ))
        if len(hits) >= req.top_k:
            break

    return CheckResponse(
        corpus_id=corpus_id,
        namespace=corpus.namespace,
        constraints=hits,
    )
