"""Orchestrator skills — spawn sub-tasks and retrieve their results."""
import logging

from engram.skills.decorator import skill

logger = logging.getLogger(__name__)


@skill(
    name="spawn_task",
    description=(
        "Spawn a new sub-task and hand it off to the orchestrator for async execution. "
        "Use this to delegate work that can run independently — for example, kicking off "
        "a research query, a data-processing job, or a specialised sub-agent while the "
        "current task continues. Returns a task_id that can be passed to get_task_result "
        "to check status or retrieve the final output."
    ),
    parameters={
        "prompt": {
            "type": "string",
            "description": "The task description or prompt to hand off to the sub-agent.",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace the sub-task should operate within.",
            "default": "personal:default",
        },
        "agent": {
            "type": "string",
            "description": (
                "Optional name of a specific agent to handle this task. "
                "If omitted, the orchestrator selects the default agent."
            ),
        },
    },
    required=["prompt"],
)
async def spawn_task(
    prompt: str,
    namespace: str = "personal:default",
    agent: str | None = None,
    **kwargs,
) -> dict:
    orchestrator = kwargs.get("orchestrator")
    if orchestrator is None:
        return {"error": "orchestrator not provided"}
    try:
        task_id = await orchestrator.spawn(prompt, namespace=namespace, agent_name=agent)
        return {
            "task_id": task_id,
            "status": "queued",
            "namespace": namespace,
            "agent": agent,
        }
    except Exception as exc:
        logger.warning("spawn_task failed: %s", exc)
        return {"error": str(exc)}


@skill(
    name="get_task_result",
    description=(
        "Retrieve the status and result of a previously spawned sub-task. "
        "Use this after calling spawn_task to check whether the task has finished "
        "and to collect its output. Set wait=true to block until the task completes "
        "(suitable for short tasks); leave wait=false (the default) to poll without blocking "
        "and check again later if the result is not yet available."
    ),
    parameters={
        "task_id": {
            "type": "string",
            "description": "The task ID returned by a prior spawn_task call.",
        },
        "wait": {
            "type": "boolean",
            "description": (
                "If true, block until the task completes before returning. "
                "If false (default), return immediately with the current status."
            ),
            "default": False,
        },
    },
    required=["task_id"],
)
async def get_task_result(
    task_id: str,
    wait: bool = False,
    **kwargs,
) -> dict:
    orchestrator = kwargs.get("orchestrator")
    if orchestrator is None:
        return {"error": "orchestrator not provided", "found": False}
    try:
        result = await orchestrator.get_result(task_id, wait=wait)
        if result is None:
            return {"found": False, "task_id": task_id, "status": "pending"}
        return {
            "found": True,
            "task_id": task_id,
            "status": result.get("status"),
            "result": result.get("result"),
        }
    except Exception as exc:
        logger.warning("get_task_result failed: %s", exc)
        return {"error": str(exc), "found": False, "task_id": task_id}
