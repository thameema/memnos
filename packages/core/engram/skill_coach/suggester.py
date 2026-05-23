"""
engram.skill_coach.suggester — Retrieve relevant Claude Code skills for a task.

Used by the skill_suggest MCP tool. Takes what the developer is trying to
accomplish and returns the most relevant Claude Code capabilities they may
not know about.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SKILL_NAMESPACE = "tool:claude-code:capabilities"
_DEFAULT_TOP_K = 3


async def suggest_skills(
    client,
    task_description: str,
    top_k: int = _DEFAULT_TOP_K,
) -> list[dict]:
    """Return relevant Claude Code skills for the given task description.

    Searches the skill namespace semantically. Returns a list of dicts with
    title, category, example, and a short reason why it's relevant.

    Returns empty list if the skill namespace has not been seeded yet.
    """
    results = await client.search(
        query=task_description,
        namespace=SKILL_NAMESPACE,
        top_k=top_k,
        mode="hybrid",
    )

    if not results:
        return []

    suggestions = []
    for r in results:
        m = r.memory
        meta = m.metadata or {}
        # Extract structured fields from content prefix
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

        suggestions.append({
            "skill_id": meta.get("skill_id", ""),
            "title": title,
            "category": category,
            "example": example,
            "relevance_score": round(r.score, 3),
            "tip": m.content.split("\n\n", 1)[-1][:300] if "\n\n" in m.content else m.content[:300],
        })

    return suggestions
