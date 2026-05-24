"""
engram.skill_coach.suggester — Retrieve relevant skills for a task.

Supports single or multi-namespace search. Can search across all seeded
tool catalogs, filter by tool name, or include team-authored skills.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 3
_TOOL_NS_PREFIX = "tool:"
_TOOL_NS_SUFFIX = ":capabilities"


def _tool_namespace(tool_name: str) -> str:
    return f"{_TOOL_NS_PREFIX}{tool_name}{_TOOL_NS_SUFFIX}"


def _extract_suggestion(r) -> dict:
    m = r.memory
    meta = m.metadata or {}
    lines = m.content.splitlines()
    title = meta.get("title", "")
    category = meta.get("category", "")
    example = ""
    for line in lines:
        if line.startswith("EXAMPLE:"):
            example = line[len("EXAMPLE:"):].strip()
            break
    if not title:
        for line in lines:
            if line.startswith("TITLE:"):
                title = line[len("TITLE:"):].strip()
                break
    return {
        "skill_id": meta.get("skill_id", ""),
        "title": title,
        "category": category,
        "tool": meta.get("tool", ""),
        "example": example,
        "relevance_score": round(r.score, 3),
        "tip": m.content.split("\n\n", 1)[-1][:300] if "\n\n" in m.content else m.content[:300],
    }


async def suggest_skills(
    client,
    task_description: str,
    top_k: int = _DEFAULT_TOP_K,
    namespaces: list[str] | None = None,
    tool_filter: str | None = None,
    include_team_skills: bool = False,
    org_namespace: str | None = None,
) -> list[dict]:
    """Return relevant skills for the given task description.

    Args:
        client: engram client
        task_description: what the developer is trying to do
        top_k: number of results to return
        namespaces: explicit list of namespaces to search; if None, searches
            tool:*:capabilities (all seeded tool catalogs)
        tool_filter: if set, restrict results to this tool (e.g. "gh", "kubectl")
        include_team_skills: if True, also search org_namespace for team-authored skills
        org_namespace: namespace for team-authored skills (required when include_team_skills=True)
    """
    if tool_filter:
        search_namespaces = [_tool_namespace(tool_filter)]
    elif namespaces is not None:
        search_namespaces = list(namespaces)
    else:
        from engram.skill_coach.capabilities import TOOL_CAPABILITY_CATALOGS
        search_namespaces = [_tool_namespace(t) for t in TOOL_CAPABILITY_CATALOGS]

    if include_team_skills and org_namespace:
        if org_namespace not in search_namespaces:
            search_namespaces.append(org_namespace)

    # Search each namespace, collect results with dedup by skill_id
    seen_ids: set[str] = set()
    all_suggestions: list[dict] = []

    for ns in search_namespaces:
        try:
            results = await client.search(
                query=task_description,
                namespace=ns,
                top_k=top_k,
                mode="hybrid",
            )
        except Exception:
            logger.debug("Namespace %s not found or empty, skipping", ns)
            continue

        for r in results:
            s = _extract_suggestion(r)
            key = s["skill_id"] or s["title"]
            if key and key in seen_ids:
                continue
            if key:
                seen_ids.add(key)
            all_suggestions.append(s)

    # Sort by relevance, return top_k
    all_suggestions.sort(key=lambda x: x["relevance_score"], reverse=True)
    return all_suggestions[:top_k]
