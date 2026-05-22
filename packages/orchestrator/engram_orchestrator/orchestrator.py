"""
engram_orchestrator.orchestrator — Main orchestration loop.

Coordinates planning, parallel worker execution, synthesis, and memory persistence
for multi-agent task execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from engram.config import EngramConfig

from .critic import CriticWorker
from .models import SubTask, Task, TaskStatus
from .planner import Planner
from .pool import WorkerPool
from .router import AgentRouter
from .synthesizer import Synthesizer
from .tag_extractor import extract_tags
from .task_store import TaskStore
from .workers.api_worker import ApiWorker
from .workers.base import BaseWorker
from .workers.claude_code_worker import ClaudeCodeWorker
from .workers.openrouter_worker import OpenRouterWorker

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5  # seconds between status polls in get_result(wait=True)


class Orchestrator:
    """
    Top-level orchestrator: decompose → run workers → synthesize → store.

    Usage
    -----
    orch = Orchestrator(config, engram_client, task_store)
    await orch.start()
    task = await orch.run("Summarise my last 5 meetings", namespace="personal:alice")
    """

    def __init__(
        self,
        config: EngramConfig,
        engram_client: Any,  # EngramClient
        task_store: TaskStore,
    ) -> None:
        self._config = config
        self._engram_client = engram_client
        self._task_store = task_store

        # Derived handles — set in start()
        self._planner: Planner | None = None
        self._synthesizer: Synthesizer | None = None
        self._critic: CriticWorker | None = None
        self._router: AgentRouter | None = None
        self._pool: WorkerPool | None = None

        # Background tasks
        self._background_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all sub-components."""
        await self._task_store.init()

        api_key = self._config.runtime.api.api_key
        model = self._config.runtime.api.model
        max_concurrent = self._config.runtime.max_concurrent_workers

        self._planner = Planner(api_key=api_key, model=model)
        self._synthesizer = Synthesizer(api_key=api_key, model=model)
        self._critic = CriticWorker(api_key=api_key, model=model)
        self._pool = WorkerPool(max_concurrent=max_concurrent)

        import os
        agents_dir = os.environ.get("ENGRAM_AGENTS_DIR", "agents")
        self._router = AgentRouter(
            agents_dir=agents_dir,
            engram_client=self._engram_client,
        )
        await self._router.init()

        logger.info(
            "Orchestrator started (model=%s, max_concurrent=%d)", model, max_concurrent
        )

    # ------------------------------------------------------------------
    # Primary run path
    # ------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        namespace: str,
        runtime: str | None = None,
        agent: str | None = None,
        timeout_s: int = 300,
    ) -> Task:
        """
        Full synchronous orchestration pipeline.

        1.  Create Task and save to store
        2.  Load past context from memory
        3.  Load heuristics from memory
        4.  Match skill template from memory
        5.  Decompose into subtasks via Planner
        6.  Assign agents per subtask (router or explicit)
        7.  Run subtasks in parallel via WorkerPool
        8.  Synthesize (if >1 subtask with results)
        9.  Run critic + revision if agent has use_critic=true
        10. Store task summary in memory
        11. Mark COMPLETE

        Returns the completed Task.
        """
        assert self._planner is not None, "Call start() before run()"
        assert self._pool is not None

        effective_runtime = runtime or self._config.runtime.default

        task = Task(
            prompt=prompt,
            namespace=namespace,
            runtime=effective_runtime,
            agent=agent,
            tags=extract_tags(prompt),
        )
        await self._task_store.save(task)
        logger.info(
            "Orchestrator: task %s created prompt=%r tags=%s",
            task.id[:8], prompt[:80], task.tags,
        )

        try:
            # ----------------------------------------------------------
            # 2-4. Load memory context
            # ----------------------------------------------------------
            await self._task_store.update_status(task.id, TaskStatus.PLANNING)
            task.status = TaskStatus.PLANNING

            past_context = await self._get_past_context(prompt, namespace)
            heuristics = await self._get_heuristics(prompt, namespace)
            template = await self._get_skill_template(prompt, namespace)

            # ----------------------------------------------------------
            # 5. Plan
            # ----------------------------------------------------------
            subtask_dicts = await self._planner.decompose(
                task=prompt,
                past_context=past_context,
                heuristics=heuristics,
                template=template,
            )

            # ----------------------------------------------------------
            # 6. Build SubTask objects, assign agents
            # ----------------------------------------------------------
            resolved_agent_def: dict | None = None
            if agent and self._router:
                resolved_agent_def = self._router.load_agent(agent)

            subtasks: list[SubTask] = []
            for st_dict in subtask_dicts:
                st = SubTask(
                    parent_task_id=task.id,
                    prompt=st_dict["prompt"],
                    agent=st_dict.get("agent") or agent,
                )
                # If no explicit agent, try router match
                if not st.agent and self._router:
                    matched = await self._router.match(st.prompt, namespace)
                    if matched:
                        st.agent = matched.get("name")

                subtasks.append(st)
                await self._task_store.save_subtask(st)

            task.subtasks = subtasks
            await self._task_store.save(task)

            # ----------------------------------------------------------
            # 7. Run subtasks in parallel
            # ----------------------------------------------------------
            await self._task_store.update_status(task.id, TaskStatus.RUNNING)
            task.status = TaskStatus.RUNNING

            def worker_factory(subtask: SubTask) -> BaseWorker:
                agent_def = None
                if subtask.agent and self._router:
                    agent_def = self._router.load_agent(subtask.agent)
                return self._make_worker(
                    runtime=effective_runtime,
                    namespace=namespace,
                    agent_def=agent_def,
                )

            completed_subtasks = await asyncio.wait_for(
                self._pool.run_parallel(subtasks, worker_factory),
                timeout=timeout_s,
            )

            # Persist updated subtask status
            for st in completed_subtasks:
                await self._task_store.update_subtask(
                    st.id,
                    st.status,
                    result=st.result,
                    error=st.error,
                )

            task.subtasks = completed_subtasks

            # ----------------------------------------------------------
            # 8. Synthesize
            # ----------------------------------------------------------
            await self._task_store.update_status(task.id, TaskStatus.SYNTHESIZING)
            task.status = TaskStatus.SYNTHESIZING

            successful = [
                (st.prompt, st.result or "")
                for st in completed_subtasks
                if st.status == TaskStatus.COMPLETE and st.result
            ]
            failed_count = sum(
                1 for st in completed_subtasks if st.status == TaskStatus.FAILED
            )

            if not successful:
                errors = "; ".join(
                    st.error or "unknown"
                    for st in completed_subtasks
                    if st.status == TaskStatus.FAILED
                )
                raise RuntimeError(f"All subtasks failed: {errors}")

            if len(successful) == 1:
                # Single subtask — no synthesis needed
                final_result = successful[0][1]
            else:
                assert self._synthesizer is not None
                final_result = await self._synthesizer.synthesize(prompt, successful)

            if failed_count > 0:
                final_result = (
                    f"Note: {failed_count} subtask(s) failed and were excluded.\n\n"
                    + final_result
                )

            # ----------------------------------------------------------
            # 9. Critic (if agent definition has use_critic=true)
            # ----------------------------------------------------------
            use_critic = False
            agent_system_prompt = ""
            critic_prompt = ""

            effective_agent_def = resolved_agent_def
            if not effective_agent_def and agent and self._router:
                effective_agent_def = self._router.load_agent(agent)

            if effective_agent_def:
                use_critic = bool(effective_agent_def.get("use_critic", False))
                agent_system_prompt = str(effective_agent_def.get("system_prompt", ""))
                critic_prompt = str(effective_agent_def.get("critic_prompt", ""))

            if use_critic and self._critic is not None:
                passed, corrections = await self._critic.evaluate(
                    task=prompt,
                    draft=final_result,
                    agent_system_prompt=agent_system_prompt,
                    critic_prompt=critic_prompt,
                )
                if not passed and corrections:
                    logger.info(
                        "Orchestrator: critic flagged issues for task %s — running revision",
                        task.id[:8],
                    )
                    revision_prompt = (
                        f"Original task:\n{prompt}\n\n"
                        f"Draft response:\n{final_result}\n\n"
                        f"Critic feedback (fix these issues):\n{corrections}"
                    )
                    revision_worker = self._make_worker(
                        runtime=effective_runtime,
                        namespace=namespace,
                        agent_def=effective_agent_def,
                    )
                    try:
                        revised = await asyncio.wait_for(
                            revision_worker.run(
                                revision_prompt,
                                system_prompt=agent_system_prompt or None,
                            ),
                            timeout=min(timeout_s, 120),
                        )
                        final_result = revised
                    except Exception as rev_exc:
                        logger.warning(
                            "Orchestrator: revision failed — %s, keeping original draft",
                            rev_exc,
                        )
                    finally:
                        await revision_worker.teardown()

            # ----------------------------------------------------------
            # 10. Store task summary in memory + record episode
            # ----------------------------------------------------------
            summary = (
                f"Task completed: {prompt[:200]}\n"
                f"Result summary: {final_result[:500]}"
            )
            try:
                await self._engram_client.add(
                    content=summary,
                    namespace=namespace,
                    tags=["task_outcome", "success"] + task.tags,
                    source="orchestrator",
                )
            except Exception as mem_exc:
                logger.warning(
                    "Orchestrator: failed to store task summary — %s", mem_exc
                )

            # Best-effort episodic record — requires engram_learning
            try:
                from engram_learning.episode_store import EpisodeStore  # type: ignore
                from engram_learning.models import EpisodicRecord, Outcome  # type: ignore

                ep_store = EpisodeStore()
                await ep_store.init()
                elapsed = (
                    (datetime.utcnow() - task.created_at).total_seconds()
                )
                ep = EpisodicRecord(
                    task_id=task.id,
                    namespace=namespace,
                    original_prompt=prompt,
                    decomposition=[st.prompt for st in completed_subtasks],
                    agent_used=agent,
                    runtime=effective_runtime,
                    outcome=Outcome.SUCCESS,
                    duration_s=elapsed,
                    token_cost=task.token_cost,
                    tags=task.tags,
                )
                await ep_store.save(ep)
                logger.debug(
                    "Orchestrator: episode %s saved tags=%s", ep.id[:8], ep.tags
                )
            except (ImportError, Exception) as ep_exc:
                logger.debug("Orchestrator: episode not recorded — %s", ep_exc)

            # ----------------------------------------------------------
            # 11. Mark COMPLETE
            # ----------------------------------------------------------
            task.result = final_result
            task.status = TaskStatus.COMPLETE
            task.completed_at = datetime.utcnow()
            await self._task_store.update_status(
                task.id, TaskStatus.COMPLETE, result=final_result
            )
            await self._task_store.save(task)

            logger.info("Orchestrator: task %s COMPLETE", task.id[:8])
            return task

        except asyncio.TimeoutError:
            err = f"Task timed out after {timeout_s}s"
            logger.error("Orchestrator: task %s — %s", task.id[:8], err)
            task.error = err
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.utcnow()
            await self._task_store.update_status(task.id, TaskStatus.FAILED, error=err)
            return task

        except Exception as exc:
            err = str(exc)
            logger.error(
                "Orchestrator: task %s FAILED — %s", task.id[:8], exc, exc_info=True
            )
            task.error = err
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.utcnow()
            await self._task_store.update_status(task.id, TaskStatus.FAILED, error=err)
            return task

    # ------------------------------------------------------------------
    # Non-blocking spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        prompt: str,
        namespace: str,
        runtime: str = "api",
        agent: str | None = None,
        timeout_s: int = 300,
    ) -> str:
        """
        Start a task in the background and return the task_id immediately.

        The task runs as a background asyncio Task; poll with get_result().
        """
        # Pre-create the task record so callers can poll before run() starts
        task = Task(
            prompt=prompt,
            namespace=namespace,
            runtime=runtime,
            agent=agent,
            status=TaskStatus.PENDING,
        )
        await self._task_store.save(task)

        async def _bg() -> None:
            await self.run(
                prompt=prompt,
                namespace=namespace,
                runtime=runtime,
                agent=agent,
                timeout_s=timeout_s,
            )

        bg_task = asyncio.create_task(_bg())
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        logger.info("Orchestrator: spawned background task %s", task.id[:8])
        return task.id

    # ------------------------------------------------------------------
    # Result retrieval
    # ------------------------------------------------------------------

    async def get_result(
        self,
        task_id: str,
        wait: bool = False,
        wait_timeout: int = 30,
    ) -> Task | None:
        """
        Retrieve a task by ID.

        Parameters
        ----------
        wait:
            If True, poll until COMPLETE/FAILED or wait_timeout seconds.
        """
        if not wait:
            return await self._task_store.get(task_id)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + wait_timeout
        while True:
            task = await self._task_store.get(task_id)
            if task is None:
                return None
            if task.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                return task
            remaining = deadline - loop.time()
            if remaining <= 0:
                return task
            await asyncio.sleep(min(_POLL_INTERVAL, remaining))

    # ------------------------------------------------------------------
    # Task listing
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        namespace: str,
        status: str = "ALL",
        limit: int = 20,
    ) -> list[Task]:
        return await self._task_store.list(namespace, status, limit)

    # ------------------------------------------------------------------
    # Worker factory
    # ------------------------------------------------------------------

    def _make_worker(
        self,
        runtime: str,
        namespace: str,
        agent_def: dict | None = None,
    ) -> BaseWorker:
        """Instantiate the correct BaseWorker for the given runtime string."""
        rt = self._config.runtime
        api_cfg = rt.api
        or_cfg = rt.openrouter

        match runtime:
            case "api":
                return ApiWorker(
                    api_key=api_cfg.api_key,
                    model=api_cfg.model,
                    engram_client=self._engram_client,
                    namespace=namespace,
                )

            case "openrouter":
                return OpenRouterWorker(
                    api_key=or_cfg.api_key,
                    model=or_cfg.model,
                    engram_client=self._engram_client,
                    namespace=namespace,
                )

            case "claude-code":
                import os

                mcp_server_url = os.environ.get(
                    "ENGRAM_MCP_URL",
                    f"http://localhost:{self._config.server.mcp_port}/sse",
                )
                return ClaudeCodeWorker(
                    api_key=api_cfg.api_key,
                    mcp_server_url=mcp_server_url,
                    namespace=namespace,
                    model=api_cfg.model,
                    timeout_s=rt.worker_timeout_s,
                )

            case _:
                logger.warning(
                    "Unknown runtime %r — falling back to 'api'", runtime
                )
                return ApiWorker(
                    api_key=api_cfg.api_key,
                    model=api_cfg.model,
                    engram_client=self._engram_client,
                    namespace=namespace,
                )

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    async def _get_past_context(self, task: str, namespace: str) -> str:
        """Return a bullet-list of similar past successful task summaries."""
        try:
            results = await self._engram_client.search(
                f"task outcome success {task}",
                namespace,
                top_k=3,
                mode="vector",
            )
            if not results:
                return ""
            lines: list[str] = []
            for r in results:
                memory = r.memory if hasattr(r, "memory") else r
                tags = list(getattr(memory, "tags", []))
                if "task_outcome" in tags or "success" in tags:
                    lines.append(f"- {memory.content[:300]}")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("_get_past_context failed: %s", exc)
            return ""

    async def _get_heuristics(self, task: str, namespace: str) -> str:
        """Return numbered heuristic rules from memory for this task type."""
        try:
            results = await self._engram_client.search(
                task,
                namespace,
                top_k=5,
                mode="vector",
            )
            if not results:
                return ""
            lines: list[str] = []
            idx = 1
            for r in results:
                memory = r.memory if hasattr(r, "memory") else r
                tags = list(getattr(memory, "tags", []))
                if "heuristic" in tags:
                    lines.append(f"{idx}. {memory.content[:300]}")
                    idx += 1
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("_get_heuristics failed: %s", exc)
            return ""

    async def _get_skill_template(self, task: str, namespace: str) -> str:
        """Return the best-matching skill template from memory, if one exists."""
        try:
            results = await self._engram_client.search(
                f"skill template {task}",
                namespace,
                top_k=3,
                mode="vector",
            )
            if not results:
                return ""
            for r in results:
                memory = r.memory if hasattr(r, "memory") else r
                tags = list(getattr(memory, "tags", []))
                score = float(getattr(r, "score", 0.0))
                if "skill_template" in tags and score > 0.80:
                    return memory.content[:1000]
            return ""
        except Exception as exc:
            logger.debug("_get_skill_template failed: %s", exc)
            return ""
