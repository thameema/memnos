"""Memory read/write skills — persistent storage via EngramClient."""
import logging

from engram.skills.decorator import skill

logger = logging.getLogger(__name__)


@skill(
    name="memory_search",
    description=(
        "Search persistent memory for information relevant to a query. "
        "Use this to recall past task outcomes, learned heuristics, stored facts, "
        "or any content previously written with memory_write. "
        "Returns ranked results with content and relevance scores."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Natural-language search query describing what to recall.",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to search within (e.g. 'personal:default').",
            "default": "personal:default",
        },
        "top_k": {
            "type": "integer",
            "description": "Maximum number of results to return.",
            "default": 10,
        },
    },
    required=["query"],
)
async def memory_search(
    query: str,
    namespace: str = "personal:default",
    top_k: int = 10,
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "results": []}
    try:
        results = await client.search(query, namespace, top_k=top_k)
        return {
            "results": [
                {
                    "content": r.memory.content,
                    "score": float(r.score),
                    "id": r.memory.id,
                    "tags": r.memory.tags,
                }
                for r in results
            ],
            "count": len(results),
            "query": query,
            "namespace": namespace,
        }
    except Exception as exc:
        logger.warning("memory_search failed: %s", exc)
        return {"error": str(exc), "results": []}


@skill(
    name="memory_write",
    description=(
        "Persist a piece of information to memory so it can be recalled in future sessions. "
        "Use this to store important findings, conclusions, facts, task outcomes, or any "
        "information that should survive beyond the current conversation."
    ),
    parameters={
        "content": {
            "type": "string",
            "description": "The text content to store in memory.",
        },
        "namespace": {
            "type": "string",
            "description": "Engram namespace to write into (e.g. 'personal:default').",
            "default": "personal:default",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional list of tags to attach for easier filtering and retrieval.",
        },
    },
    required=["content"],
)
async def memory_write(
    content: str,
    namespace: str = "personal:default",
    tags: list[str] | None = None,
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided"}
    try:
        entry = await client.add(
            content=content,
            namespace=namespace,
            tags=tags or [],
            source="agent",
        )
        return {
            "id": entry.id,
            "content": entry.content,
            "namespace": entry.namespace,
            "tags": entry.tags,
        }
    except Exception as exc:
        logger.warning("memory_write failed: %s", exc)
        return {"error": str(exc)}


@skill(
    name="memory_delete",
    description=(
        "Delete a specific memory entry by its ID. "
        "Use this to remove outdated, incorrect, or no-longer-relevant information "
        "from persistent memory. Requires the exact memory ID from a prior memory_search "
        "or memory_write result."
    ),
    parameters={
        "memory_id": {
            "type": "string",
            "description": "The unique ID of the memory entry to delete.",
        },
        "namespace": {
            "type": "string",
            "description": "Namespace the memory belongs to. Must match the stored namespace.",
            "default": "personal:default",
        },
    },
    required=["memory_id"],
)
async def memory_delete(
    memory_id: str,
    namespace: str = "personal:default",
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "deleted": False}
    try:
        deleted = await client.delete(memory_id, namespace)
        return {"deleted": deleted, "id": memory_id, "namespace": namespace}
    except Exception as exc:
        logger.warning("memory_delete failed: %s", exc)
        return {"error": str(exc), "deleted": False, "id": memory_id}


@skill(
    name="memory_get",
    description=(
        "Retrieve a single memory entry by its exact ID. "
        "Use this when you already have a memory ID (e.g. from a previous memory_write) "
        "and want to fetch the full entry rather than doing a search."
    ),
    parameters={
        "memory_id": {
            "type": "string",
            "description": "The unique ID of the memory entry to retrieve.",
        },
        "namespace": {
            "type": "string",
            "description": "Namespace the memory belongs to.",
            "default": "personal:default",
        },
    },
    required=["memory_id"],
)
async def memory_get(
    memory_id: str,
    namespace: str = "personal:default",
    **kwargs,
) -> dict:
    client = kwargs.get("engram_client")
    if client is None:
        return {"error": "engram_client not provided", "found": False}
    try:
        entry = await client.get_memory(memory_id, namespace)
        if entry is None:
            return {"found": False, "id": memory_id}
        return {
            "found": True,
            "id": entry.id,
            "content": entry.content,
            "namespace": entry.namespace,
            "tags": entry.tags,
            "source": entry.source,
        }
    except Exception as exc:
        logger.warning("memory_get failed: %s", exc)
        return {"error": str(exc), "found": False, "id": memory_id}
