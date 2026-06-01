"""
memnos.ingest — File ingestion for Markdown, plain text, PDF, and DOCX.

Pipeline
--------
    bytes + filename
      → detect mime
      → extract plain text
      → chunk into overlapping windows
      → return (full_text, list[chunk_str])

The Episode is built from ``full_text`` (verbatim, immutable); the
Memories are built from ``chunks`` and each carries a
``source_episode_ids = [episode.id]`` lineage pointer back to the file.

This module is dependency-light: PDF and DOCX libs are imported lazily so
the runtime cost is zero when those formats aren't used.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}


@dataclass(frozen=True)
class IngestResult:
    text: str
    chunks: list[str]
    file_type: str           # "markdown" | "text" | "pdf" | "docx"
    file_sha256: str
    char_count: int


# ---------------------------------------------------------------------------
# Per-format extractors
# ---------------------------------------------------------------------------

def _extract_text(data: bytes) -> str:
    # UTF-8 with replacement keeps malformed bytes from crashing ingest.
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # local import — only loaded when used
    except ImportError as exc:
        raise RuntimeError(
            "PDF ingestion requires 'pypdf' — pip install pypdf"
        ) from exc
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            # One bad page should not abort the whole document.
            continue
    return "\n\n".join(parts).strip()


def _extract_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise RuntimeError(
            "DOCX ingestion requires 'python-docx' — pip install python-docx"
        ) from exc
    document = docx.Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    return "\n\n".join(paragraphs).strip()


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def chunk_text(text: str, target_chars: int = 800, overlap_chars: int = 100) -> list[str]:
    """Sentence-aware sliding-window chunker.

    Splits the text on sentence boundaries first, then greedily packs
    sentences into windows of up to ``target_chars`` characters. Each
    subsequent window starts ``overlap_chars`` characters before the prior
    window ends — preserving cross-chunk context for retrieval.

    Empty input returns an empty list, never a single empty chunk.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target_chars:
        return [text]

    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for sent in sentences:
        if buf_len + len(sent) + 1 <= target_chars:
            buf.append(sent)
            buf_len += len(sent) + 1
            continue
        # Flush current buffer
        if buf:
            chunks.append(" ".join(buf).strip())
            # Seed next buffer with the tail of the prior chunk for overlap
            tail = chunks[-1][-overlap_chars:] if overlap_chars > 0 else ""
            buf = [tail, sent] if tail else [sent]
            buf_len = len(tail) + len(sent) + 1
        else:
            # Single sentence longer than target_chars — hard-split it.
            for i in range(0, len(sent), target_chars):
                chunks.append(sent[i : i + target_chars])
            buf = []
            buf_len = 0
    if buf:
        chunks.append(" ".join(buf).strip())
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest_file(
    data: bytes,
    filename: str,
    target_chunk_chars: int = 800,
    overlap_chars: int = 100,
) -> IngestResult:
    """Detect file type, extract text, chunk it. Returns IngestResult."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        file_type = "pdf"
        text = _extract_pdf(data)
    elif lower.endswith(".docx"):
        file_type = "docx"
        text = _extract_docx(data)
    elif lower.endswith((".md", ".markdown")):
        file_type = "markdown"
        text = _extract_text(data)
    elif lower.endswith(".txt"):
        file_type = "text"
        text = _extract_text(data)
    else:
        raise ValueError(
            f"Unsupported file extension for {filename!r}; "
            f"supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )

    if not text.strip():
        raise ValueError(f"No extractable text in {filename!r}")

    chunks = chunk_text(text, target_chars=target_chunk_chars, overlap_chars=overlap_chars)
    sha = hashlib.sha256(data).hexdigest()
    return IngestResult(
        text=text,
        chunks=chunks,
        file_type=file_type,
        file_sha256=sha,
        char_count=len(text),
    )
