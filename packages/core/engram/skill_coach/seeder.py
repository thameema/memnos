"""
engram.skill_coach.seeder — Seeds Claude Code skill memories into engram.

Run via: engram-skill-seed (CLI) or skill_discover MCP tool.
Updates existing skills if content changed; adds new ones; never deletes.
"""
from __future__ import annotations

import hashlib
import logging

from engram.skill_coach.capabilities import CLAUDE_CODE_CAPABILITIES

logger = logging.getLogger(__name__)

SKILL_NAMESPACE = "tool:claude-code:capabilities"
SKILL_SOURCE = "skill-coach"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


async def seed_claude_code_capabilities(client) -> dict:
    """Seed all Claude Code capability memories into the skill namespace.

    Idempotent: checks existing skills by their 'skill_id' metadata field.
    Updates content if changed. Returns counts: added, updated, skipped.
    """
    from engram.models import MemoryType, MemoryStatus

    added = updated = skipped = 0

    for cap in CLAUDE_CODE_CAPABILITIES:
        skill_id = cap["id"]
        content = cap["content"]
        content_h = _content_hash(content)

        # Search for existing skill with this id
        existing = await client.search(
            query=f"SKILL_ID:{skill_id}",
            namespace=SKILL_NAMESPACE,
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
            # Content changed — supersede old, add new
            await client.supersede(match.id, SKILL_NAMESPACE)
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
            namespace=SKILL_NAMESPACE,
            tags=["skill-coach", "claude-code"] + cap.get("tags", []),
            source=SKILL_SOURCE,
            metadata={
                "skill_id": skill_id,
                "title": cap["title"],
                "category": cap["category"],
                "content_hash": content_h,
            },
            memory_type=MemoryType.skill,
            status=MemoryStatus.active,
            author="engram-skill-coach",
            rationale=cap["when_to_use"],
        )

    logger.info(
        "Skill seeding complete: %d added, %d updated, %d skipped",
        added, updated, skipped,
    )
    return {"added": added, "updated": updated, "skipped": skipped}
