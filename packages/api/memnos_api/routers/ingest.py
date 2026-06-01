"""
memnos_api.routers.ingest — File ingestion (Feature 4).

POST /ingest/file uploads a single PDF, DOCX, Markdown, or plain-text file.
The pipeline is:

  1. Extract plain text from the file (per-format extractor).
  2. Create ONE immutable Episode containing the full extracted text.
     File metadata (name, sha256, type, char_count) is recorded on the
     Episode for audit and re-derivation.
  3. Chunk the text into ~800-char overlapping windows.
  4. For each chunk, create a Memory linked back to the Episode via
     ``source_episode_ids = [episode.id]``.

The Episode is the source of truth; any chunk can be re-derived later by
re-running the chunker against the Episode's text. That is the lineage
guarantee Feature 2 was built to support — Feature 4 is its first real
producer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from memnos.ingest import SUPPORTED_EXTENSIONS, ingest_file
from memnos.models import Episode, MemoryType, Provenance
from memnos_api.auth import (
    check_namespace_access,
    get_client,
    require_api_key,
    require_api_key_entry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Cap upload size at 20 MB — large enough for normal docs, small enough to
# protect the parser. Returns 413 if exceeded.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class IngestResponse(BaseModel):
    """Summary of what was created from one file upload."""
    episode_id: str
    file_name: str
    file_type: str
    file_sha256: str
    char_count: int
    chunk_count: int
    memory_ids: list[str] = Field(default_factory=list)


@router.post("/file", response_model=IngestResponse, status_code=201)
async def ingest_file_endpoint(
    file: UploadFile = File(..., description="PDF, DOCX, Markdown, or plain text"),
    namespace: str = Form(..., description="Target namespace"),
    target_chunk_chars: int = Form(800, ge=200, le=4000),
    overlap_chars: int = Form(100, ge=0, le=500),
    user_id: str = Depends(require_api_key),
    key_entry=Depends(require_api_key_entry),
    client=Depends(get_client),
):
    """Ingest one uploaded file into memnos.

    The file becomes ONE Episode plus N chunked Memories, all linked.
    Returns the Episode id and the ids of every Memory written.
    """
    await check_namespace_access(key_entry, namespace, operation="write")

    name = file.filename or "uploaded"
    lower = name.lower()
    if not any(lower.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type. Supported: {sorted(SUPPORTED_EXTENSIONS)}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap",
        )

    try:
        result = ingest_file(
            data, name,
            target_chunk_chars=target_chunk_chars,
            overlap_chars=overlap_chars,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Ingest failed for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=f"Ingest failed: {exc}") from exc

    # ── 1. Write the immutable Episode with full text + file metadata ──
    ep_metadata: dict[str, Any] = {
        "file_name": name,
        "file_type": result.file_type,
        "file_sha256": result.file_sha256,
        "char_count": result.char_count,
        "chunk_count": len(result.chunks),
    }
    ep = Episode(
        content=result.text,
        namespace=namespace,
        source="file",
        author=user_id,
        metadata=ep_metadata,
        provenance=Provenance(user_id=user_id, tool="ingest"),
    )
    try:
        await client._arcadedb.insert_episode(ep)
    except Exception as exc:
        logger.exception("Episode insert failed during ingest: %s", exc)
        raise HTTPException(status_code=500, detail=f"Episode insert failed: {exc}") from exc

    # ── 2. Write one Memory per chunk, each linked to the Episode ──
    memory_ids: list[str] = []
    for i, chunk in enumerate(result.chunks):
        try:
            memory = await client.add(
                content=chunk,
                namespace=namespace,
                tags=[f"file:{name}", f"chunk:{i}"],
                source="file",
                metadata={
                    "file_name": name,
                    "chunk_index": i,
                    "chunk_count": len(result.chunks),
                },
                memory_type=MemoryType.fact,
                author=user_id,
                provenance=Provenance(user_id=user_id, tool="ingest"),
                source_episode_ids=[ep.id],
            )
            memory_ids.append(str(memory.id))
        except Exception as exc:
            # Don't abort the whole ingest if one chunk fails — record and continue.
            logger.warning(
                "chunk %d/%d failed for file %s: %s",
                i, len(result.chunks), name, exc,
            )

    logger.info(
        "ingest complete | file=%s ns=%s episode=%s chunks=%d memories=%d",
        name, namespace, ep.id, len(result.chunks), len(memory_ids),
    )

    return IngestResponse(
        episode_id=ep.id,
        file_name=name,
        file_type=result.file_type,
        file_sha256=result.file_sha256,
        char_count=result.char_count,
        chunk_count=len(result.chunks),
        memory_ids=memory_ids,
    )
