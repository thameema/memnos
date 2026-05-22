"""Skill template extractor — captures successful task patterns."""
from __future__ import annotations

import json
import logging

import anthropic

from engram_learning.episode_store import EpisodeStore
from engram_learning.skill_store import SkillStore
from engram_learning.models import EpisodicRecord, Outcome, SkillTemplate

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """A task was completed successfully. Decide if it represents a reusable approach pattern.

TASK: {task}
APPROACH TAKEN (subtasks):
{decomposition}
OUTCOME: success (quality score: {score})

If this approach would be useful as a template for similar future tasks:
1. Write a one-sentence description of the problem type this solves
2. List 3-5 short phrases that would indicate a future task matches this pattern
3. List the approach as numbered steps

If the task is too specific or one-off, respond with: {{"extract": false}}

Respond in JSON only, no markdown:
{{
  "extract": true,
  "description": "...",
  "trigger_patterns": ["...", "..."],
  "steps": ["1. ...", "2. ..."]
}}
"""


class SkillExtractor:
    def __init__(self, api_key: str, model: str, skill_store: SkillStore, engram_client=None):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.store = skill_store
        self._engram_client = engram_client

    async def maybe_extract(self, episode: EpisodicRecord):
        if not episode.quality_score or episode.quality_score < 0.8:
            return
        if episode.outcome != Outcome.SUCCESS:
            return

        existing = await self.store.find_match(episode.original_prompt, episode.namespace)
        if existing:
            await self.store.increment_use(existing.id, True)
            return

        prompt = EXTRACTION_PROMPT.format(
            task=episode.original_prompt[:400],
            decomposition="\n".join(f"- {s}" for s in episode.decomposition[:10]),
            score=episode.quality_score,
        )

        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(raw)
        except Exception as exc:
            logger.debug("Skill extraction failed: %s", exc)
            return

        if result.get("extract"):
            template = SkillTemplate(
                namespace=episode.namespace,
                name=result.get("description", "")[:50].lower().replace(" ", "-"),
                description=result.get("description", ""),
                trigger_patterns=result.get("trigger_patterns", []),
                steps=result.get("steps", []),
                source_episode_id=episode.id,
            )
            await self.store.add(template)
            logger.info("Skill template extracted: %s", template.name)
            # Sync to ArcadeDB so the planner can find it via vector search
            if self._engram_client:
                try:
                    steps_text = "\n".join(template.steps)
                    patterns_text = ", ".join(template.trigger_patterns)
                    content = (
                        f"Skill template: {template.description}\n"
                        f"Triggers: {patterns_text}\n"
                        f"Steps:\n{steps_text}"
                    )
                    await self._engram_client.add(
                        content=content,
                        namespace=episode.namespace,
                        tags=["skill_template", "learning"],
                        source="skill_extractor",
                    )
                except Exception as sync_exc:
                    logger.debug("ArcadeDB skill template sync failed: %s", sync_exc)
