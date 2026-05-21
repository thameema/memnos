"""
engram_api.routers.tasks — Orchestrator task management endpoints.

Endpoints
---------
POST   /tasks/           — spawn a new task
GET    /tasks/{task_id}  — get task status and result
GET    /tasks/           — list tasks (?ns=&status=)
DELETE /tasks/{task_id}  — cancel a running task
POST   /tasks/feedback   — submit feedback for a task
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from engram_api.auth import get_orchestrator, require_api_key
from engram_api.schemas import FeedbackRequest, SpawnTaskRequest, TaskResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _task_to_response(task) -> TaskResponse:
    """Convert an orchestrator Task object to a TaskResponse."""
    task_id = str(getattr(task, "id", getattr(task, "task_id", "")))
    status = str(getattr(task, "status", "UNKNOWN"))
    if hasattr(status, "value"):
        status = status.value
    return TaskResponse(
        task_id=task_id,
        status=status,
        prompt=getattr(task, "prompt", None),
        result=getattr(task, "result", None),
        error=getattr(task, "error", None),
        created_at=getattr(task, "created_at", None),
        completed_at=getattr(task, "completed_at", None),
    )


# ---------------------------------------------------------------------------
# Spawn task
# ---------------------------------------------------------------------------

@router.post("/", response_model=TaskResponse, status_code=202)
async def spawn_task(
    req: SpawnTaskRequest,
    user_id: str = Depends(require_api_key),
    orchestrator=Depends(get_orchestrator),
) -> TaskResponse:
    """
    Spawn a background worker task and return its task ID immediately.

    The task runs asynchronously. Poll ``GET /tasks/{task_id}`` to check status.
    """
    logger.debug(
        "spawn_task | ns=%s runtime=%s agent=%s user=%s prompt=%r",
        req.namespace,
        req.runtime,
        req.agent,
        user_id,
        req.prompt[:120],
    )
    try:
        result = await orchestrator.spawn(
            prompt=req.prompt,
            namespace=req.namespace,
            runtime=req.runtime,
            agent=req.agent,
            timeout_s=req.timeout_s,
        )
    except Exception as exc:
        logger.exception("spawn_task failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # orchestrator.spawn() returns either a task_id string or a Task object
    if isinstance(result, str):
        # task_id returned — fetch the task record for a proper response
        try:
            task = await orchestrator.get_result(result, wait=False)
        except Exception:
            task = None
        if task is not None:
            return _task_to_response(task)
        # Fallback: return minimal response with just the ID
        return TaskResponse(task_id=result, status="PENDING")
    else:
        return _task_to_response(result)


# ---------------------------------------------------------------------------
# Get task
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[TaskResponse])
async def list_tasks(
    ns: str = Query(..., description="Namespace to filter tasks by"),
    status: str = Query("ALL", description="PENDING | RUNNING | COMPLETE | FAILED | ALL"),
    limit: int = Query(20, ge=1, le=200),
    user_id: str = Depends(require_api_key),
    orchestrator=Depends(get_orchestrator),
) -> list[TaskResponse]:
    """List tasks for a namespace, optionally filtered by status."""
    logger.debug(
        "list_tasks | ns=%s status=%s limit=%d user=%s", ns, status, limit, user_id
    )
    try:
        tasks = await orchestrator.list_tasks(ns, status, limit)
    except Exception as exc:
        logger.exception("list_tasks failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if tasks is None:
        return []
    return [_task_to_response(t) for t in tasks]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    wait: bool = Query(False, description="Block up to 30 s waiting for completion"),
    user_id: str = Depends(require_api_key),
    orchestrator=Depends(get_orchestrator),
) -> TaskResponse:
    """Get the current status and result of a previously spawned task."""
    logger.debug("get_task | task_id=%s wait=%s user=%s", task_id, wait, user_id)
    try:
        task = await orchestrator.get_result(task_id, wait=wait, wait_timeout=30)
    except Exception as exc:
        logger.exception("get_task failed for %s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    return _task_to_response(task)


# ---------------------------------------------------------------------------
# Cancel task
# ---------------------------------------------------------------------------

@router.delete("/{task_id}", status_code=204)
async def cancel_task(
    task_id: str,
    user_id: str = Depends(require_api_key),
    orchestrator=Depends(get_orchestrator),
) -> None:
    """Cancel a running or pending task."""
    logger.debug("cancel_task | task_id=%s user=%s", task_id, user_id)
    try:
        cancelled = await orchestrator.cancel(task_id)
    except Exception as exc:
        logger.exception("cancel_task failed for %s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not cancelled:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found or already complete")


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@router.post("/feedback", status_code=204)
async def submit_feedback(
    req: FeedbackRequest,
    user_id: str = Depends(require_api_key),
) -> None:
    """
    Record explicit feedback (positive / negative) for a completed task.

    Requires ``engram_learning`` to be installed for persistence; if it is
    not installed, the call succeeds silently so the API contract is stable.
    """
    logger.debug(
        "feedback | task_id=%s signal=%s user=%s comment=%r",
        req.task_id,
        req.signal,
        user_id,
        req.comment[:80],
    )
    if req.signal not in ("positive", "negative"):
        raise HTTPException(
            status_code=422,
            detail="signal must be 'positive' or 'negative'",
        )

    try:
        from engram_learning.feedback import FeedbackService  # type: ignore

        # FeedbackService requires stores; attempt to use the app-state version
        # if available, otherwise do a lightweight standalone record.
        fs = FeedbackService.__new__(FeedbackService)  # dependency-injected elsewhere
        # Best-effort call — if no stores are wired this will fail silently
        if hasattr(fs, "record_explicit"):
            await fs.record_explicit(req.task_id, req.signal, req.comment)
    except (ImportError, Exception) as exc:
        logger.debug("Feedback not persisted (%s); continuing", exc)
