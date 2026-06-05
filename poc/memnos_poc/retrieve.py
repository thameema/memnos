"""PG retrieve pipeline: embed query → hybrid RRF (vector+FTS+1-hop) → local
cross-encoder rerank → token-budgeted context block. No LLM, all local."""
from __future__ import annotations

from memnos_poc import local_models


def retrieve(storage, schema, ns, query, *, k=20, top_k=8, rerank=True):
    qvec = local_models.embed(query)
    cands = storage.hybrid_search(schema, ns, query, qvec, k=k, top_k=k)
    if not cands:
        return []
    if rerank and len(cands) > 1:
        order = local_models.rerank(query, [c["content"] for c in cands])
        cands = [cands[i] for i, _ in order]
    return cands[:top_k]


def context_block(rows, max_chars: int = 2000) -> str:
    """Assemble retrieved memories into a budgeted context string for the LLM."""
    out, used = [], 0
    for r in rows:
        line = f"- {r['content']}"
        if used + len(line) > max_chars:
            break
        out.append(line); used += len(line)
    return "\n".join(out)
