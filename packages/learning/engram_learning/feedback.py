"""Feedback detection and recording."""
from __future__ import annotations

import logging
import re

from engram_learning.models import Outcome
from engram_learning.episode_store import EpisodeStore
from engram_learning.quality_store import QualityStore

logger = logging.getLogger(__name__)

CORRECTION_PATTERNS = [
    r"\b(no|nope|wrong|incorrect|that'?s not right|actually|wait|hold on)\b",
    r"\b(the correct .+ is|you missed|you forgot|you got that wrong)\b",
    r"\b(that'?s wrong|not quite|that'?s incorrect)\b",
    r"\b(you'?re wrong|that is wrong|that was wrong)\b",
]


def detect_correction(text: str) -> bool:
    for pattern in CORRECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


class FeedbackService:
    def __init__(
        self,
        episode_store: EpisodeStore,
        quality_store: QualityStore,
        reflection_service=None,
    ):
        self.episodes = episode_store
        self.quality = quality_store
        self._reflection = reflection_service

    async def record_explicit(self, task_id: str, signal: str, comment: str = ""):
        """Record a thumbs-up / thumbs-down signal."""
        ep = await self.episodes.get_by_task_id(task_id)
        if not ep:
            logger.warning("No episode found for task_id %s", task_id)
            return

        if signal == "positive":
            outcome = Outcome.SUCCESS
            score = 1.0
        else:
            outcome = Outcome.CORRECTED if comment else Outcome.FAILURE
            score = 0.0

        await self.episodes.update_outcome(ep.id, outcome, comment or None, score)

        if ep.agent_used:
            for tag in ep.tags or ["general"]:
                await self.quality.update(ep.agent_used, tag, ep.namespace, score, ep.duration_s, signal == "positive")

        if signal == "negative" and self._reflection:
            try:
                await self._reflection.run(lookback_days=1)
            except Exception as exc:
                logger.warning("Triggered reflection failed: %s", exc)

    async def record_correction(self, task_id: str, correction_text: str):
        """Record a user correction and trigger immediate reflection."""
        ep = await self.episodes.get_by_task_id(task_id)
        if not ep:
            return
        await self.episodes.update_outcome(ep.id, Outcome.CORRECTED, correction_text, 0.1)
        if self._reflection:
            try:
                await self._reflection.run(lookback_days=1)
            except Exception as exc:
                logger.warning("Post-correction reflection failed: %s", exc)
