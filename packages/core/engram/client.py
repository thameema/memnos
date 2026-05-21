"""
engram.client — EngramClient: the primary public API for engram.

Usage (async context manager):

    async with EngramClient(config) as client:
        entry = await client.add("Alice is a software engineer", namespace="org:acme")
        results = await client.search("software engineer", namespace="org:acme")

Usage (manual lifecycle):

    client = EngramClient(config)
    await client.start()
    ...
    await client.stop()
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engram.config import EngramConfig
from engram.graph.graphiti_client import EngramGraphitiClient
from engram.models import Entity, Fact, Graph, MemoryEntry, SearchResult
from engram.search import HybridSearch
from engram.vector.embedder import get_embedder
from engram.vector.qdrant_client import EngramQdrantClient

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EngramClient:
    """Primary public API for engram persistent memory.

    All methods are async. Use as an async context manager for automatic
    lifecycle management, or call :meth:`start` / :meth:`stop` manually.
    """

    def __init__(self, config: EngramConfig) -> None:
        self._config = config
        self._embedder = get_embedder(config.embeddings)
        self._qdrant = EngramQdrantClient(
            config.qdrant, vector_size=self._embedder.vector_size
        )
        self._graphiti = EngramGraphitiClient(config.neo4j)
        self._search = HybridSearch(
            qdrant=self._qdrant,
            graphiti=self._graphiti,
            embedder=self._embedder,
        )
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise all backends.  Must be called before any other method."""
        if self._started:
            logger.debug("EngramClient.start() called but already started — skipping")
            return
        logger.info("Starting EngramClient")
        await self._qdrant.init()
        await self._graphiti.init()
        self._started = True
        logger.info("EngramClient ready")

    async def stop(self) -> None:
        """Gracefully close all backend connections."""
        logger.info("Stopping EngramClient")
        await self._graphiti.close()
        await self._qdrant.close()
        self._started = False
        logger.info("EngramClient stopped")

    # ------------------------------------------------------------------
    # Async context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "EngramClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_started(self) -> None:
        if not self._started:
            raise RuntimeError(
                "EngramClient must be started before use. "
                "Call `await client.start()` or use it as `async with EngramClient(...) as client:`."
            )

    def _build_payload(self, memory: MemoryEntry) -> dict:
        """Serialise a MemoryEntry into a Qdrant-safe payload dict."""
        return {
            "memory_id": memory.id,
            "content": memory.content,
            "namespace": memory.namespace,
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
            "tags": memory.tags,
            "source": memory.source,
            "graph_node_id": memory.graph_node_id,
            "metadata": memory.metadata,
        }

    # ------------------------------------------------------------------
    # Core memory operations
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def add(
        self,
        content: str,
        namespace: str,
        tags: list[str] | None = None,
        source: str = "agent",
        metadata: dict | None = None,
    ) -> MemoryEntry:
        """Persist a new memory entry across both Qdrant and Graphiti.

        Steps:
          1. Create a ``MemoryEntry`` with a fresh uuid4 ID.
          2. Embed ``content`` via the configured embedder.
          3. Upsert the vector to Qdrant.
          4. Add an episode to Graphiti.
          5. Update ``embedding_id`` and ``graph_node_id`` on the entry.
          6. Return the completed ``MemoryEntry``.

        Parameters
        ----------
        content:
            The text to remember.
        namespace:
            Target namespace (e.g. ``"org:acme"`` or ``"personal:alice"``).
        tags:
            Optional list of searchable tags.
        source:
            Human-readable label for the recording agent/tool (default ``"agent"``).
        metadata:
            Arbitrary key-value pairs stored alongside the memory.

        Returns
        -------
        MemoryEntry
            The persisted memory, with both ``embedding_id`` and
            ``graph_node_id`` populated.
        """
        self._assert_started()
        memory = MemoryEntry(
            content=content,
            namespace=namespace,
            tags=tags or [],
            source=source,
            metadata=metadata or {},
        )
        logger.debug("add: memory_id=%s namespace=%s", memory.id, namespace)

        # Step 2 — embed
        vector = await self._embedder.embed(content)

        # Step 3 — upsert to Qdrant (use memory.id as point ID)
        payload = self._build_payload(memory)
        await self._qdrant.upsert(memory_id=memory.id, vector=vector, payload=payload)
        memory.embedding_id = memory.id

        # Step 4 — add episode to Graphiti
        graph_node_id = await self._graphiti.add_memory(memory)
        memory.graph_node_id = graph_node_id

        # Step 5 — update the Qdrant payload with the graph node ID
        updated_payload = self._build_payload(memory)
        await self._qdrant.upsert(memory_id=memory.id, vector=vector, payload=updated_payload)

        logger.info(
            "Memory stored: id=%s namespace=%s embedding_id=%s graph_node_id=%s",
            memory.id,
            namespace,
            memory.embedding_id,
            memory.graph_node_id,
        )
        return memory

    async def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        mode: str = "hybrid",
    ) -> list[SearchResult]:
        """Search for memories matching *query* within *namespace*.

        Parameters
        ----------
        query:
            Natural-language query string.
        namespace:
            Restrict results to this namespace.
        top_k:
            Maximum number of results (default 10).
        mode:
            ``"vector"``, ``"graph"``, or ``"hybrid"`` (default).

        Returns
        -------
        list[SearchResult]
            Ranked results, highest score first.
        """
        self._assert_started()
        logger.debug("search: query=%r namespace=%s mode=%s top_k=%d", query, namespace, mode, top_k)
        return await self._search.search(query, namespace=namespace, top_k=top_k, mode=mode)

    async def delete(self, memory_id: str, namespace: str) -> bool:
        """Delete a memory by ID from both Qdrant and (best-effort) Graphiti.

        Parameters
        ----------
        memory_id:
            The ``MemoryEntry.id`` to delete.
        namespace:
            The namespace the memory belongs to (used for safety checks).

        Returns
        -------
        bool
            ``True`` if the point existed in Qdrant and was deleted,
            ``False`` if it was not found.
        """
        self._assert_started()
        logger.debug("delete: memory_id=%s namespace=%s", memory_id, namespace)

        # Check existence first
        payload = await self._qdrant.get(memory_id)
        if payload is None:
            logger.info("delete: memory_id=%s not found in Qdrant", memory_id)
            return False

        # Safety: verify namespace matches
        stored_ns = payload.get("namespace", "")
        if stored_ns and stored_ns != namespace:
            raise ValueError(
                f"Memory {memory_id!r} belongs to namespace {stored_ns!r}, "
                f"not {namespace!r}. Deletion refused."
            )

        await self._qdrant.delete(memory_id)
        logger.info("delete: memory_id=%s deleted from Qdrant", memory_id)
        return True

    async def get_memory(self, memory_id: str, namespace: str) -> MemoryEntry | None:
        """Retrieve a single memory by ID.

        Parameters
        ----------
        memory_id:
            The ``MemoryEntry.id`` to look up.
        namespace:
            Expected namespace (used to verify ownership).

        Returns
        -------
        MemoryEntry | None
            The memory, or ``None`` if not found.
        """
        self._assert_started()
        payload = await self._qdrant.get(memory_id)
        if payload is None:
            return None
        from engram.search import _payload_to_memory
        return _payload_to_memory(memory_id, payload, namespace)

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------

    async def get_entity(self, name: str, namespace: str) -> Entity | None:
        """Look up a named entity in the knowledge graph.

        Returns ``None`` if not found.
        """
        self._assert_started()
        return await self._graphiti.get_entity(name=name, namespace=namespace)

    async def get_related(
        self, entity_name: str, namespace: str, depth: int = 2
    ) -> Graph:
        """Return a sub-graph of entities and relations near *entity_name*.

        Parameters
        ----------
        entity_name:
            Starting entity name.
        namespace:
            Restrict traversal to this namespace.
        depth:
            Maximum number of hops (default 2).

        Returns
        -------
        Graph
            A ``Graph`` containing discovered entities and their relations.
        """
        self._assert_started()
        return await self._graphiti.get_related(
            entity_name=entity_name, namespace=namespace, depth=depth
        )

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        namespace: str,
        valid_until: datetime | None = None,
    ) -> Fact:
        """Record a subject-predicate-object fact in the knowledge graph.

        Parameters
        ----------
        subject, predicate, object:
            The three parts of the triple (e.g. ``"Alice"`` ``"works at"`` ``"Acme"``).
        namespace:
            Target namespace.
        valid_until:
            Optional expiry datetime after which the fact is no longer valid.

        Returns
        -------
        Fact
            The persisted fact with a populated ``id``.
        """
        self._assert_started()
        fact = Fact(
            subject=subject,
            predicate=predicate,
            object=object,
            namespace=namespace,
            valid_until=valid_until,
        )
        graph_node_id = await self._graphiti.add_fact(fact)
        # Store the Graphiti node ID back on the fact via source_memory_id
        fact.source_memory_id = graph_node_id
        logger.info(
            "Fact stored: id=%s %s %s %s namespace=%s",
            fact.id,
            subject,
            predicate,
            object,
            namespace,
        )
        return fact

    async def query_graph(
        self,
        cypher: str,
        namespace: str,
        params: dict | None = None,
    ) -> list[dict]:
        """Execute a read-only Cypher query against Neo4j.

        The ``$namespace`` parameter is automatically injected so callers
        can reference it in their Cypher without passing it explicitly.

        Parameters
        ----------
        cypher:
            A read-only Cypher statement (must start with MATCH, CALL, WITH,
            or RETURN).
        namespace:
            Injected as ``$namespace`` in the query parameters.
        params:
            Additional Cypher parameters.

        Returns
        -------
        list[dict]
            Raw records returned by Neo4j.
        """
        self._assert_started()
        return await self._graphiti.raw_query(
            cypher=cypher, params=params or {}, namespace=namespace
        )
