"""Heuristic confidence decay — stale rules fade out automatically."""
from __future__ import annotations

import logging
from datetime import datetime

from engram_learning.heuristic_store import HeuristicStore

logger = logging.getLogger(__name__)


class HeuristicDecayService:
    def __init__(
        self,
        heuristic_store: HeuristicStore,
        inactive_days: int = 30,
        decay_rate: float = 0.9,
    ):
        self.store = heuristic_store
        self.inactive_days = inactive_days
        self.decay_rate = decay_rate

    async def run(self, namespace: str):
        heuristics = await self.store.get_all(namespace)
        now = datetime.utcnow()
        deleted = 0
        decayed = 0
        for h in heuristics:
            last = h.last_triggered_at or h.created_at
            days_since = (now - last).days
            if days_since > self.inactive_days:
                new_conf = h.confidence * self.decay_rate
                if new_conf < 0.1:
                    await self.store.delete(h.id)
                    deleted += 1
                else:
                    await self.store.update_confidence(h.id, new_conf - h.confidence)
                    decayed += 1
        logger.info("Heuristic decay complete for %s: %d decayed, %d deleted", namespace, decayed, deleted)
