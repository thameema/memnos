"""
engram_orchestrator.workers.base — Abstract base class for all worker runtimes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseWorker(ABC):
    """Abstract worker that executes a single task prompt and returns a string result."""

    worker_id: str

    @abstractmethod
    async def run(self, task_prompt: str, system_prompt: str | None = None) -> str:
        """Execute the task and return result as string."""
        ...

    async def teardown(self) -> None:
        """Clean up resources (override as needed)."""
        pass
