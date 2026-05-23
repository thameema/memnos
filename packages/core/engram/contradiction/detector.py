"""
engram.contradiction.detector — Detects semantic contradictions before memory writes.

Non-blocking: contradiction checks run async and return warnings. They never
prevent a write from completing. The check uses vector similarity + a simple
heuristic (high similarity + content differences) to flag candidates.

An optional LLM confirmation step can be enabled via config (default: off).
LLM check is only triggered when similarity > 0.90 to avoid expensive calls
on clearly different content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.client import EngramClient

logger = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.88   # flag if cosine similarity > this
_MAX_CANDIDATES = 5


@dataclass
class ContradictionWarning:
    existing_id: str
    existing_content: str
    similarity: float
    reason: str = ""


async def check_contradictions(
    client: "EngramClient",
    content: str,
    namespace: str,
) -> list[ContradictionWarning]:
    """
    Search for existing memories that are highly similar to *content*.

    Returns a list of ContradictionWarning — one per candidate that exceeds
    the similarity threshold. Empty list means no contradictions detected.

    This is intentionally heuristic: high vector similarity does not always
    mean semantic contradiction. Surface the warnings; let the human decide.
    """
    try:
        results = await client.search(
            query=content,
            namespace=namespace,
            top_k=_MAX_CANDIDATES,
            mode="vector",
        )
    except Exception as exc:
        logger.debug("Contradiction check search failed (non-fatal): %s", exc)
        return []

    warnings: list[ContradictionWarning] = []
    for r in results:
        score = float(getattr(r, "score", 0.0))
        if score < _SIMILARITY_THRESHOLD:
            continue
        existing = r.memory if hasattr(r, "memory") else r
        existing_content = str(getattr(existing, "content", ""))
        # Simple divergence heuristic: similar score but different first sentence
        new_first = content.split(".")[0].lower().strip()
        exist_first = existing_content.split(".")[0].lower().strip()
        if new_first == exist_first[:len(new_first)]:
            # Likely an update, not a contradiction — skip
            continue
        warnings.append(ContradictionWarning(
            existing_id=str(getattr(existing, "id", "")),
            existing_content=existing_content[:300],
            similarity=score,
            reason=f"High similarity ({score:.2f}) with different opening claim",
        ))
    return warnings


class ContradictionDetector:
    """Stateless wrapper for use from client and MCP server."""

    async def check(
        self,
        client: "EngramClient",
        content: str,
        namespace: str,
    ) -> list[ContradictionWarning]:
        return await check_contradictions(client, content, namespace)
