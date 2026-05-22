"""Reflection agent — derives heuristics from past failures."""
from __future__ import annotations

import json
import logging

import anthropic

from engram_learning.episode_store import EpisodeStore
from engram_learning.heuristic_store import HeuristicStore
from engram_learning.models import Heuristic, Outcome

logger = logging.getLogger(__name__)

REFLECTION_PROMPT = """You are a self-improvement agent for an AI orchestration system.

RECENT TASK OUTCOMES (last {lookback_days} days):
{episodes}

EXISTING HEURISTICS:
{existing_heuristics}

Analyse the failures and corrections. For each pattern you identify:
1. State the rule in one sentence (plain English, future-tense instruction)
2. State the rationale (which specific failure led to this rule)
3. List the topic tags this rule applies to

Also identify any existing heuristics that should be:
- Strengthened (pattern confirmed, increase confidence)
- Weakened (contradicted by successes, decrease confidence)
- Deleted (no longer relevant)

Respond in JSON only, no markdown:
{{
  "new_heuristics": [
    {{"rule": "...", "rationale": "...", "applies_to_tags": [...], "confidence": 0.8}}
  ],
  "update_heuristics": [
    {{"id": "...", "confidence_delta": 0.1, "reason": "..."}}
  ],
  "delete_heuristic_ids": []
}}
"""


def _fmt_episodes(episodes) -> str:
    lines = []
    for ep in episodes:
        status = ep.outcome.value
        feedback = f" Correction: {ep.user_feedback}" if ep.user_feedback else ""
        lines.append(f"- [{status}] {ep.original_prompt[:120]}{feedback}")
    return "\n".join(lines) or "No episodes."


def _fmt_heuristics(heuristics) -> str:
    if not heuristics:
        return "None."
    lines = []
    for h in heuristics:
        lines.append(f"- [{h.id[:8]}] (conf={h.confidence:.2f}) {h.rule}")
    return "\n".join(lines)


class ReflectionService:
    def __init__(
        self,
        api_key: str,
        model: str,
        episode_store: EpisodeStore,
        heuristic_store: HeuristicStore,
        namespace: str,
        engram_client=None,
    ):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.episodes = episode_store
        self.heuristics = heuristic_store
        self.namespace = namespace
        self._engram_client = engram_client

    async def run(self, lookback_days: int = 7):
        episodes = await self.episodes.get_recent(self.namespace, lookback_days)
        failed = [e for e in episodes if e.outcome in (Outcome.FAILURE, Outcome.CORRECTED)]
        if len(failed) < 2:
            logger.info("Reflection skipped — only %d failure/correction episodes", len(failed))
            return

        existing = await self.heuristics.get_all(self.namespace)
        prompt = REFLECTION_PROMPT.format(
            lookback_days=lookback_days,
            episodes=_fmt_episodes(episodes),
            existing_heuristics=_fmt_heuristics(existing),
        )

        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
            updates = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Reflection LLM returned invalid JSON: %s", exc)
            return
        except Exception as exc:
            logger.error("Reflection LLM call failed: %s", exc)
            return

        for h_data in updates.get("new_heuristics", []):
            try:
                h = Heuristic(
                    namespace=self.namespace,
                    rule=h_data["rule"],
                    rationale=h_data.get("rationale", ""),
                    applies_to_tags=h_data.get("applies_to_tags", []),
                    confidence=float(h_data.get("confidence", 0.8)),
                )
                await self.heuristics.add(h)
                logger.info("New heuristic added: %s", h.rule[:80])
                # Sync to ArcadeDB so the planner can find it via vector search
                if self._engram_client:
                    try:
                        content = f"Heuristic: {h.rule}"
                        if h.rationale:
                            content += f"\nRationale: {h.rationale}"
                        await self._engram_client.add(
                            content=content,
                            namespace=self.namespace,
                            tags=["heuristic", "learning"] + list(h.applies_to_tags),
                            source="reflection",
                        )
                    except Exception as sync_exc:
                        logger.debug("ArcadeDB heuristic sync failed: %s", sync_exc)
            except Exception as exc:
                logger.warning("Failed to add heuristic: %s", exc)

        for upd in updates.get("update_heuristics", []):
            try:
                await self.heuristics.update_confidence(upd["id"], float(upd.get("confidence_delta", 0)))
            except Exception as exc:
                logger.warning("Failed to update heuristic %s: %s", upd.get("id"), exc)

        for hid in updates.get("delete_heuristic_ids", []):
            await self.heuristics.delete(hid)

        logger.info(
            "Reflection complete: +%d new, %d updated, %d deleted heuristics",
            len(updates.get("new_heuristics", [])),
            len(updates.get("update_heuristics", [])),
            len(updates.get("delete_heuristic_ids", [])),
        )
