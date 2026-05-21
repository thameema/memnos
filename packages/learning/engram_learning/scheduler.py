"""Learning scheduler — runs reflection and decay on cron schedules."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class LearningScheduler:
    def __init__(self, config, reflection_service, decay_service, namespace: str):
        self.config = config
        self.reflection = reflection_service
        self.decay = decay_service
        self.namespace = namespace
        self._scheduler = None

    def start(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            self._scheduler = AsyncIOScheduler()

            ref_cfg = getattr(getattr(self.config, "learning", None), "reflection", None)
            schedule = getattr(ref_cfg, "schedule", "0 2 * * *") or "0 2 * * *"
            minute, hour, day, month, dow = schedule.split()
            self._scheduler.add_job(
                self._run_reflection,
                CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
                id="engram_reflection",
                replace_existing=True,
            )

            decay_cfg = getattr(getattr(self.config, "learning", None), "heuristic_decay", None)
            decay_schedule = getattr(decay_cfg, "schedule", "0 3 * * 0") or "0 3 * * 0"
            dm, dh, dd, dmo, ddow = decay_schedule.split()
            self._scheduler.add_job(
                self._run_decay,
                CronTrigger(minute=dm, hour=dh, day=dd, month=dmo, day_of_week=ddow),
                id="engram_decay",
                replace_existing=True,
            )

            self._scheduler.start()
            logger.info("Learning scheduler started")
        except ImportError:
            logger.warning("apscheduler not installed — learning scheduler disabled")
        except Exception as exc:
            logger.error("Learning scheduler failed to start: %s", exc)

    def stop(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

    async def _run_reflection(self):
        try:
            ref_cfg = getattr(getattr(self.config, "learning", None), "reflection", None)
            days = getattr(ref_cfg, "lookback_days", 7)
            await self.reflection.run(lookback_days=days)
        except Exception as exc:
            logger.error("Scheduled reflection failed: %s", exc)

    async def _run_decay(self):
        try:
            await self.decay.run(self.namespace)
        except Exception as exc:
            logger.error("Scheduled decay failed: %s", exc)
