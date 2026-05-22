"""Learning scheduler — runs reflection and decay on cron schedules."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class LearningScheduler:
    def __init__(
        self,
        config,
        reflection_service,
        decay_service,
        namespace: str,
        episode_store=None,
        reflection_factory=None,
    ):
        self.config = config
        self.reflection = reflection_service
        self.decay = decay_service
        self.namespace = namespace
        # Optional: episode store for discovering active namespaces
        self._episode_store = episode_store
        # Optional factory: (namespace) -> ReflectionService — used for multi-namespace runs
        self._reflection_factory = reflection_factory
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
        ref_cfg = getattr(getattr(self.config, "learning", None), "reflection", None)
        days = getattr(ref_cfg, "lookback_days", 7)

        # Collect all namespaces that have had recent activity
        namespaces_to_reflect: list[str] = [self.namespace]
        if self._episode_store:
            try:
                active = await self._episode_store.get_active_namespaces(days=days)
                # Merge, preserving default namespace and deduplicating
                seen = {self.namespace}
                for ns in active:
                    if ns not in seen:
                        namespaces_to_reflect.append(ns)
                        seen.add(ns)
            except Exception as disc_exc:
                logger.warning("Namespace discovery failed: %s", disc_exc)

        for ns in namespaces_to_reflect:
            try:
                if ns == self.namespace:
                    await self.reflection.run(lookback_days=days)
                elif self._reflection_factory:
                    svc = self._reflection_factory(ns)
                    await svc.run(lookback_days=days)
                else:
                    # Re-use the default service but swap namespace temporarily
                    original_ns = self.reflection.namespace
                    self.reflection.namespace = ns
                    try:
                        await self.reflection.run(lookback_days=days)
                    finally:
                        self.reflection.namespace = original_ns
                logger.info("Reflection complete for namespace %s", ns)
            except Exception as exc:
                logger.error("Scheduled reflection failed for ns=%s: %s", ns, exc)

    async def _run_decay(self):
        # Run decay across all active namespaces
        namespaces_to_decay: list[str] = [self.namespace]
        if self._episode_store:
            try:
                active = await self._episode_store.get_active_namespaces(days=30)
                seen = {self.namespace}
                for ns in active:
                    if ns not in seen:
                        namespaces_to_decay.append(ns)
                        seen.add(ns)
            except Exception:
                pass

        for ns in namespaces_to_decay:
            try:
                await self.decay.run(ns)
            except Exception as exc:
                logger.error("Scheduled decay failed for ns=%s: %s", ns, exc)
