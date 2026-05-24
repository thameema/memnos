"""
engram.skill_coach.seeder — Seeds tool skill memories into engram.

Run via: engram-skill-seed (CLI) or skill_discover MCP tool.
Updates existing skills if content changed; adds new ones; never deletes.
"""
from __future__ import annotations

import hashlib
import logging

from engram.skill_coach.capabilities import CLAUDE_CODE_CAPABILITIES, TOOL_CAPABILITY_CATALOGS

logger = logging.getLogger(__name__)

SKILL_NAMESPACE = "tool:claude-code:capabilities"
SKILL_SOURCE = "skill-coach"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


async def _seed_capabilities(client, capabilities: list[dict], namespace: str, tool_tag: str) -> dict:
    """Internal: seed a capability list into the given namespace."""
    from engram.models import MemoryType, MemoryStatus

    added = updated = skipped = 0

    for cap in capabilities:
        skill_id = cap["id"]
        content = cap["content"]
        content_h = _content_hash(content)

        existing = await client.search(
            query=f"SKILL_ID:{skill_id}",
            namespace=namespace,
            top_k=3,
            mode="graph",
        )
        match = next(
            (r.memory for r in existing
             if r.memory.metadata.get("skill_id") == skill_id),
            None,
        )

        if match:
            if match.metadata.get("content_hash") == content_h:
                skipped += 1
                continue
            await client.supersede(match.id, namespace)
            updated += 1
        else:
            added += 1

        full_content = (
            f"SKILL_ID:{skill_id}\n"
            f"TITLE: {cap['title']}\n"
            f"CATEGORY: {cap['category']}\n"
            f"WHEN TO USE: {cap['when_to_use']}\n"
            f"EXAMPLE: {cap['example']}\n\n"
            f"{content}"
        )

        await client.add(
            content=full_content,
            namespace=namespace,
            tags=["skill-coach", tool_tag] + cap.get("tags", []),
            source=SKILL_SOURCE,
            metadata={
                "skill_id": skill_id,
                "title": cap["title"],
                "category": cap["category"],
                "content_hash": content_h,
                "tool": tool_tag,
            },
            memory_type=MemoryType.skill,
            status=MemoryStatus.active,
            author="engram-skill-coach",
            rationale=cap["when_to_use"],
        )

    logger.info(
        "Skill seeding complete [%s]: %d added, %d updated, %d skipped",
        namespace, added, updated, skipped,
    )
    return {"added": added, "updated": updated, "skipped": skipped}


async def seed_tool_capabilities(
    client,
    tool_name: str,
    capabilities: list[dict] | None = None,
) -> dict:
    """Seed capabilities for any tool into its namespace.

    If capabilities is None, uses the pre-built catalog from TOOL_CAPABILITY_CATALOGS.
    Seeds into namespace: tool:{tool_name}:capabilities

    Returns counts: added, updated, skipped. Raises ValueError for unknown tool
    when capabilities is None.
    """
    if capabilities is None:
        if tool_name not in TOOL_CAPABILITY_CATALOGS:
            raise ValueError(
                f"No built-in catalog for tool '{tool_name}'. "
                f"Available: {sorted(TOOL_CAPABILITY_CATALOGS)}. "
                "Pass capabilities= explicitly to seed a custom list."
            )
        capabilities = TOOL_CAPABILITY_CATALOGS[tool_name]

    namespace = f"tool:{tool_name}:capabilities"
    return await _seed_capabilities(client, capabilities, namespace, tool_tag=tool_name)


async def seed_claude_code_capabilities(client) -> dict:
    """Seed all Claude Code capability memories into the skill namespace.

    Idempotent: checks existing skills by their 'skill_id' metadata field.
    Updates content if changed. Returns counts: added, updated, skipped.
    """
    return await _seed_capabilities(
        client, CLAUDE_CODE_CAPABILITIES, SKILL_NAMESPACE, tool_tag="claude-code"
    )
