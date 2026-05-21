"""
engram_mcp.tools.orchestrator_tools — MCP tool handlers for task orchestration,
heuristics, reflection, and agent discovery.

Handlers
--------
handle_spawn_task          : fork a background worker task
handle_get_task_result     : poll or await a task result
handle_list_tasks          : list tasks in a namespace
handle_get_heuristics      : fetch learned heuristic rules
handle_add_heuristic       : store a manual heuristic rule
handle_trigger_reflection  : kick off the reflection agent
handle_list_agents         : list available agent YAML definitions
"""

from __future__ import annotations

import glob
import logging
import os
from datetime import datetime
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_AGENTS_DIR_ENV = "ENGRAM_AGENTS_DIR"
_DEFAULT_AGENTS_DIR = "./agents"


def _dt_to_iso(value: Any) -> Any:
    """Recursively convert datetime objects to ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _dt_to_iso(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dt_to_iso(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Task management
# ---------------------------------------------------------------------------

async def handle_spawn_task(
    orchestrator,
    prompt: str,
    namespace: str,
    runtime: str = "api",
    agent: str | None = None,
    timeout_s: int = 300,
) -> dict:
    """
    Fork a background worker task.

    Returns
    -------
    {"task_id": str, "status": "PENDING"}
    """
    logger.debug(
        "spawn_task | ns=%s runtime=%s agent=%s prompt=%r",
        namespace,
        runtime,
        agent,
        prompt[:120],
    )

    task = await orchestrator.spawn(
        prompt=prompt,
        namespace=namespace,
        runtime=runtime,
        agent=agent,
        timeout_s=timeout_s,
    )

    task_id = str(getattr(task, "task_id", getattr(task, "id", str(task))))

    return {"task_id": task_id, "status": "PENDING"}


async def handle_get_task_result(
    orchestrator,
    task_id: str,
    wait: bool = False,
) -> dict:
    """
    Retrieve the result of a previously spawned task.

    Parameters
    ----------
    wait : if True, block for up to 30 s waiting for the task to complete.

    Returns
    -------
    {"task_id", "status", "result", "completed_at", "error"}
    """
    logger.debug("get_task_result | task_id=%s wait=%s", task_id, wait)

    result = await orchestrator.get_result(task_id, wait=wait, wait_timeout=30)

    if result is None:
        return {
            "task_id": task_id,
            "status": "NOT_FOUND",
            "result": None,
            "completed_at": None,
            "error": None,
        }

    completed_at = getattr(result, "completed_at", None)
    if isinstance(completed_at, datetime):
        completed_at = completed_at.isoformat()

    return {
        "task_id": str(getattr(result, "task_id", task_id)),
        "status": str(getattr(result, "status", "UNKNOWN")),
        "result": getattr(result, "result", None),
        "completed_at": completed_at,
        "error": getattr(result, "error", None),
    }


async def handle_list_tasks(
    orchestrator,
    namespace: str,
    status: str = "ALL",
    limit: int = 20,
) -> dict:
    """
    List tasks for a given namespace, optionally filtered by status.

    Returns
    -------
    {"tasks": [...], "total": N}
    """
    logger.debug("list_tasks | ns=%s status=%s limit=%d", namespace, status, limit)

    raw_tasks = await orchestrator.list_tasks(namespace, status, limit)

    if raw_tasks is None:
        raw_tasks = []

    tasks = []
    for t in raw_tasks:
        created_at = getattr(t, "created_at", None)
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()

        prompt = getattr(t, "prompt", "") or ""
        tasks.append(
            {
                "task_id": str(getattr(t, "task_id", getattr(t, "id", ""))),
                "status": str(getattr(t, "status", "UNKNOWN")),
                "prompt_preview": prompt[:100],
                "created_at": created_at,
            }
        )

    return {"tasks": tasks, "total": len(tasks)}


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

async def handle_get_heuristics(
    namespace: str,
    query: str | None = None,
    limit: int = 20,
) -> dict:
    """
    Fetch learned heuristic rules for a namespace.

    Returns
    -------
    {"heuristics": [...], "total": N}
    """
    logger.debug("get_heuristics | ns=%s query=%r limit=%d", namespace, query, limit)

    try:
        from engram_learning.heuristic_store import HeuristicStore  # type: ignore

        store = HeuristicStore(namespace=namespace)
        heuristics = await store.get(query=query, limit=limit)
        if heuristics is None:
            heuristics = []
    except ImportError:
        logger.warning("engram_learning not installed; returning empty heuristics")
        heuristics = []

    serialised = []
    for h in heuristics:
        if isinstance(h, dict):
            serialised.append(_dt_to_iso(h))
        else:
            serialised.append(
                _dt_to_iso(
                    {
                        "id": str(getattr(h, "id", "")),
                        "rule": str(getattr(h, "rule", "")),
                        "namespace": str(getattr(h, "namespace", namespace)),
                        "rationale": str(getattr(h, "rationale", "")),
                        "applies_to_tags": list(getattr(h, "applies_to_tags", []) or []),
                        "score": float(getattr(h, "score", 1.0)),
                        "created_at": getattr(h, "created_at", None),
                    }
                )
            )

    return {"heuristics": serialised, "total": len(serialised)}


async def handle_add_heuristic(
    namespace: str,
    rule: str,
    rationale: str = "",
    applies_to_tags: list[str] | None = None,
) -> dict:
    """
    Add a manual heuristic rule to the store.

    Returns
    -------
    {"id": str, "rule": str, "namespace": str, "created_at": str}
    """
    logger.debug("add_heuristic | ns=%s rule=%r tags=%s", namespace, rule[:120], applies_to_tags)

    try:
        from engram_learning.heuristic_store import HeuristicStore  # type: ignore

        store = HeuristicStore(namespace=namespace)
        heuristic = await store.add(
            rule=rule,
            namespace=namespace,
            rationale=rationale,
            applies_to_tags=applies_to_tags or [],
        )
    except ImportError:
        logger.warning("engram_learning not installed; heuristic not persisted")
        from datetime import timezone
        heuristic = {
            "id": "unavailable",
            "rule": rule,
            "namespace": namespace,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return _dt_to_iso(heuristic)

    if isinstance(heuristic, dict):
        return _dt_to_iso(heuristic)

    created_at = getattr(heuristic, "created_at", None)
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()

    return {
        "id": str(getattr(heuristic, "id", "")),
        "rule": str(getattr(heuristic, "rule", rule)),
        "namespace": namespace,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------

async def handle_trigger_reflection(
    namespace: str,
    lookback_days: int = 7,
) -> dict:
    """
    Trigger the reflection agent for a given namespace.

    Returns
    -------
    {"triggered": bool, "namespace": str, "lookback_days": int, "message": str}
    """
    logger.debug("trigger_reflection | ns=%s lookback_days=%d", namespace, lookback_days)

    try:
        from engram_learning.reflection import ReflectionAgent  # type: ignore

        agent = ReflectionAgent(namespace=namespace)
        result = await agent.run(lookback_days=lookback_days)
        message = str(result) if result is not None else "Reflection completed"
        triggered = True
    except ImportError:
        logger.warning("engram_learning not installed; reflection not available")
        message = "engram_learning package not installed"
        triggered = False
    except Exception as exc:
        logger.exception("Reflection agent raised an error: %s", exc)
        message = f"Reflection agent error: {exc}"
        triggered = False

    return {
        "triggered": triggered,
        "namespace": namespace,
        "lookback_days": lookback_days,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Agent discovery
# ---------------------------------------------------------------------------

def _load_agent_yaml(path: str) -> dict | None:
    """Parse a YAML agent definition file; return None on error."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:
        logger.warning("Failed to load agent YAML %s: %s", path, exc)
        return None


async def handle_list_agents(
    filter: str | None = None,
) -> dict:
    """
    Scan the agents directory and return available agent definitions.

    The directory is resolved from the ``ENGRAM_AGENTS_DIR`` environment
    variable (default ``./agents``).

    Returns
    -------
    {"agents": [{"name", "description", "model", "tools"}], "total": N}
    """
    agents_dir = os.environ.get(_AGENTS_DIR_ENV, _DEFAULT_AGENTS_DIR)
    agents_dir = os.path.abspath(agents_dir)
    logger.debug("list_agents | dir=%s filter=%r", agents_dir, filter)

    if not os.path.isdir(agents_dir):
        logger.warning("Agents directory not found: %s", agents_dir)
        return {"agents": [], "total": 0}

    pattern = os.path.join(agents_dir, "**", "*.yaml")
    yaml_files = glob.glob(pattern, recursive=True)
    # Also check top-level *.yml
    yaml_files += glob.glob(os.path.join(agents_dir, "**", "*.yml"), recursive=True)
    yaml_files = sorted(set(yaml_files))

    agents = []
    for path in yaml_files:
        data = _load_agent_yaml(path)
        if data is None:
            continue

        name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
        description = data.get("description") or ""
        model = data.get("model") or ""
        tools = data.get("tools") or []

        if filter and filter.lower() not in name.lower() and filter.lower() not in description.lower():
            continue

        agents.append(
            {
                "name": str(name),
                "description": str(description),
                "model": str(model),
                "tools": list(tools) if isinstance(tools, (list, tuple)) else [],
            }
        )

    return {"agents": agents, "total": len(agents)}
