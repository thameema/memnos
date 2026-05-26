"""
engram_orchestrator.pool — Concurrent worker pool for parallel subtask execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .models import SubTask, TaskStatus
from .workers.base import BaseWorker

logger = logging.getLogger(__name__)


class WorkerPool:
    """
    Runs a list of SubTasks concurrently, bounded by a semaphore.

    Each SubTask gets its own worker instance created via `worker_factory`.
    If a subtask raises an exception it is marked FAILED and execution continues
    — the pool never fails the whole batch due to a single subtask error.
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self._max_concurrent = max_concurrent

    async def run_parallel(
        self,
        subtasks: list[SubTask],
        worker_factory: Callable[[SubTask], BaseWorker],
    ) -> list[SubTask]:
        """
        Execute all subtasks concurrently and return updated SubTask list.

        Parameters
        ----------
        subtasks:
            List of SubTask instances to execute.
        worker_factory:
            Callable that accepts a SubTask and returns a BaseWorker.
            Called once per subtask immediately before execution begins.

        Returns
        -------
        The same SubTask instances with `status`, `result`, `error`,
        `started_at`, and `completed_at` updated in-place.
        """
        sem = asyncio.Semaphore(self._max_concurrent)

        async def run_one(subtask: SubTask) -> SubTask:
            worker: BaseWorker | None = None
            async with sem:
                subtask.status = TaskStatus.RUNNING
                subtask.started_at = datetime.now(timezone.utc)

                try:
                    worker = worker_factory(subtask)
                    result = await worker.run(subtask.prompt)
                    subtask.result = result
                    subtask.status = TaskStatus.COMPLETE
                    logger.debug(
                        "WorkerPool: subtask %s COMPLETE (worker=%s)",
                        subtask.id[:8],
                        subtask.worker_id,
                    )
                except Exception as exc:
                    subtask.error = str(exc)
                    subtask.status = TaskStatus.FAILED
                    logger.warning(
                        "WorkerPool: subtask %s FAILED — %s",
                        subtask.id[:8],
                        exc,
                    )
                finally:
                    subtask.completed_at = datetime.now(timezone.utc)
                    if worker is not None:
                        try:
                            await worker.teardown()
                        except Exception as tear_exc:
                            logger.debug(
                                "WorkerPool: teardown error for subtask %s — %s",
                                subtask.id[:8],
                                tear_exc,
                            )

            return subtask

        results = await asyncio.gather(
            *[run_one(st) for st in subtasks],
            return_exceptions=True,
        )

        # Gather returns exceptions if return_exceptions=True and run_one itself
        # raises (which it shouldn't, but handle defensively).
        updated: list[SubTask] = []
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                subtasks[i].error = f"Unexpected pool error: {res}"
                subtasks[i].status = TaskStatus.FAILED
                subtasks[i].completed_at = datetime.now(timezone.utc)
                updated.append(subtasks[i])
            else:
                updated.append(res)  # type: ignore[arg-type]

        return updated
