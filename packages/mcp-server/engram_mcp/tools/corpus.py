"""
engram_mcp.tools.corpus — MCP tool handlers for corpus constraint checking.

Tools exposed
-------------
corpus_check   — return constraints from an architecture corpus relevant to
                 a code snippet + context; agents use this before implementing
                 or reviewing code to surface applicable architectural rules.
corpus_list    — list all registered corpus sources and their sync status.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_ENGRAM_API_BASE = os.environ.get("ENGRAM_API_BASE", "http://localhost:8766")
_ENGRAM_API_KEY  = os.environ.get("ENGRAM_API_KEY", "engram-local-dev-key")


async def handle_corpus_check(
    client,
    corpus_id: str,
    code: str,
    context: str = "",
    top_k: int = 10,
) -> dict:
    """
    Return constraints from an architecture corpus relevant to a code snippet.

    Parameters
    ----------
    client     : EngramClient (unused — calls REST API directly for corpus endpoints)
    corpus_id  : ID of the registered corpus (from corpus_list)
    code       : code snippet being reviewed or implemented
    context    : free-text description of what the code does / which component it belongs to
    top_k      : max constraints to return (default 10)

    Returns
    -------
    dict with keys:
      corpus_id   : str
      namespace   : str
      constraints : list of {memory_id, content, severity, source_file, section, score}

    Usage example (agent CLAUDE.md)
    --------------------------------
    Before implementing or reviewing patient-access consent filter code:
      corpus_check(corpus_id="<id>", code=diff_text, context="patient-access consent filter")
    Returns relevant SHALL/SHOULD constraints from lld.md with source citations.
    """
    import httpx

    url = f"{_ENGRAM_API_BASE}/api/v1/corpus/{corpus_id}/check"
    payload = {"code": code, "context": context, "top_k": top_k}

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {_ENGRAM_API_KEY}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("corpus_check HTTP error: %s", exc)
        return {"error": str(exc), "constraints": []}

    constraints = data.get("constraints", [])
    if not constraints:
        return {
            "corpus_id": corpus_id,
            "namespace": data.get("namespace", ""),
            "constraints": [],
            "message": "No matching constraints found for this context.",
        }

    lines = [
        f"Corpus: {corpus_id} | Namespace: {data.get('namespace', '')}",
        f"Found {len(constraints)} relevant constraint(s):\n",
    ]
    for i, c in enumerate(constraints, 1):
        sev = f"[{c['severity']}] " if c.get("severity") else ""
        lines.append(f"{i}. {sev}{c['content']}")
        if c.get("source_file") or c.get("section"):
            src = c.get("source_file", "")
            sec = c.get("section", "")
            lines.append(f"   Source: {src}" + (f" | Section: {sec}" if sec else ""))
        lines.append(f"   Score: {c.get('score', 0):.3f} | ID: {c.get('memory_id', '')}")
        lines.append("")

    return {
        "corpus_id": corpus_id,
        "namespace": data.get("namespace", ""),
        "constraints": constraints,
        "formatted": "\n".join(lines),
    }


async def handle_corpus_list(client) -> dict:
    """
    List all registered corpus sources and their sync status.

    Returns
    -------
    dict with key ``corpora``: list of {id, name, source_path, namespace,
    status, node_count, last_sync_sha, last_sync_at}

    Use the ``id`` from this response as ``corpus_id`` in corpus_check.
    """
    import httpx

    url = f"{_ENGRAM_API_BASE}/api/v1/corpus/"
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {_ENGRAM_API_KEY}"})
            resp.raise_for_status()
            corpora = resp.json()
    except Exception as exc:
        logger.warning("corpus_list HTTP error: %s", exc)
        return {"error": str(exc), "corpora": []}

    if not corpora:
        return {"corpora": [], "message": "No corpus sources registered yet."}

    lines = [f"Registered corpora ({len(corpora)}):\n"]
    for c in corpora:
        lines.append(
            f"  id: {c['id']}\n"
            f"  name: {c['name']}\n"
            f"  namespace: {c['namespace']}\n"
            f"  status: {c['status']}  nodes: {c['node_count']}  sha: {c.get('last_sync_sha','')}\n"
            f"  source: {c['source_path']}\n"
        )

    return {"corpora": corpora, "formatted": "\n".join(lines)}
