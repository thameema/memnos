"""
engram.graph.graphiti_client — Async Graphiti (temporal KG) wrapper.

Graphiti API used here:
  - Graphiti(uri, user, password)  — constructor
  - graphiti.build_indices_and_constraints()
  - graphiti.add_episode(name, episode_body, source_description, reference_time)
  - graphiti.search(query, center_node_uuid=None)
  - graphiti.driver  — the raw neo4j AsyncDriver for Cypher queries
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engram.models import Entity, Fact, Graph, MemoryEntry, Relation

if TYPE_CHECKING:
    from engram.config import Neo4jConfig

logger = logging.getLogger(__name__)

# Allowlist of Cypher clause starters that are considered read-only
_READ_ONLY_START = re.compile(
    r"^\s*(MATCH|CALL|WITH|RETURN)\b", re.IGNORECASE
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_memory_from_node(node_data: dict, namespace: str) -> MemoryEntry:
    """Convert a raw Graphiti/Neo4j node dict into a MemoryEntry."""
    return MemoryEntry(
        id=node_data.get("uuid", node_data.get("id", "")),
        content=node_data.get("content", node_data.get("episode_body", "")),
        namespace=node_data.get("namespace", namespace),
        created_at=node_data.get("created_at", _now()),
        updated_at=node_data.get("updated_at", _now()),
        tags=node_data.get("tags", []),
        source=node_data.get("source", "agent"),
        graph_node_id=node_data.get("uuid", node_data.get("id")),
        metadata=node_data.get("metadata", {}),
    )


class EngramGraphitiClient:
    """Async wrapper around ``graphiti_core.Graphiti``."""

    def __init__(self, config: "Neo4jConfig") -> None:
        self._config = config
        self._graphiti: Any = None  # graphiti_core.Graphiti
        self._driver: Any = None    # neo4j.AsyncDriver (borrowed from graphiti)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Initialise Graphiti and build Neo4j indices/constraints."""
        try:
            from graphiti_core import Graphiti  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "graphiti-core package is required. Install: pip install graphiti-core"
            ) from exc

        logger.info(
            "Connecting to Neo4j at %s (db=%s)", self._config.uri, self._config.database
        )
        self._graphiti = Graphiti(
            self._config.uri,
            self._config.username,
            self._config.password,
        )
        await self._graphiti.build_indices_and_constraints()
        # Expose the driver for raw Cypher queries
        self._driver = getattr(self._graphiti, "driver", None)
        logger.info("Graphiti initialised and indices built")

    async def close(self) -> None:
        """Close Graphiti and Neo4j connections."""
        if self._graphiti is not None:
            try:
                close_fn = getattr(self._graphiti, "close", None)
                if close_fn is not None:
                    await close_fn()
            except Exception:
                logger.debug("Graphiti close raised (ignored)", exc_info=True)
            self._graphiti = None
            self._driver = None
            logger.debug("Graphiti client closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_ready(self) -> None:
        if self._graphiti is None:
            raise RuntimeError("EngramGraphitiClient.init() must be called before use")

    # ------------------------------------------------------------------
    # Memory operations
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def add_memory(self, memory: MemoryEntry) -> str:
        """Add a MemoryEntry as an episodic node in Graphiti.

        Returns
        -------
        str
            The episode/node UUID assigned by Graphiti.
        """
        self._assert_ready()
        tags_str = ",".join(memory.tags) if memory.tags else ""
        source_description = (
            f"namespace={memory.namespace} source={memory.source} tags={tags_str}"
        )
        logger.debug(
            "Graphiti add_episode: memory_id=%s namespace=%s", memory.id, memory.namespace
        )
        result = await self._graphiti.add_episode(
            name=memory.id,
            episode_body=memory.content,
            source_description=source_description,
            reference_time=memory.created_at,
        )
        # Graphiti returns the episode object or UUID — handle both
        if result is None:
            return memory.id
        if isinstance(result, str):
            return result
        return getattr(result, "uuid", None) or getattr(result, "id", None) or memory.id

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def add_fact(self, fact: Fact) -> str:
        """Store a subject-predicate-object fact as an episodic episode.

        Returns
        -------
        str
            The episode UUID.
        """
        self._assert_ready()
        fact_body = f"{fact.subject} {fact.predicate} {fact.object}"
        source_description = f"fact namespace={fact.namespace}"
        result = await self._graphiti.add_episode(
            name=fact.id,
            episode_body=fact_body,
            source_description=source_description,
            reference_time=fact.valid_from,
        )
        if result is None:
            return fact.id
        if isinstance(result, str):
            return result
        return getattr(result, "uuid", None) or getattr(result, "id", None) or fact.id

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def get_entity(self, name: str, namespace: str) -> Entity | None:
        """Look up a named entity within a namespace.

        Returns ``None`` if not found.
        """
        self._assert_ready()
        cypher = (
            "MATCH (n:Entity) "
            "WHERE n.name = $name AND n.namespace = $namespace "
            "RETURN n LIMIT 1"
        )
        rows = await self.raw_query(cypher, {"name": name, "namespace": namespace}, namespace)
        if not rows:
            return None
        node = rows[0].get("n", rows[0])
        return Entity(
            id=str(node.get("uuid", node.get("id", ""))),
            name=node.get("name", name),
            entity_type=node.get("entity_type", node.get("type", "unknown")),
            namespace=node.get("namespace", namespace),
            attributes={
                k: v for k, v in node.items()
                if k not in {"uuid", "id", "name", "entity_type", "type", "namespace", "created_at"}
            },
            created_at=node.get("created_at", _now()),
        )

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def get_related(self, entity_name: str, namespace: str, depth: int = 2) -> Graph:
        """Return a sub-graph of entities and relations up to ``depth`` hops away."""
        self._assert_ready()
        cypher = (
            f"MATCH path = (start:Entity {{name: $name, namespace: $namespace}})"
            f"-[*1..{depth}]-(related) "
            "WHERE all(n IN nodes(path) WHERE n.namespace = $namespace) "
            "UNWIND relationships(path) AS r "
            "RETURN "
            "  startNode(r) AS src_node, "
            "  endNode(r) AS tgt_node, "
            "  type(r) AS rel_type, "
            "  properties(r) AS rel_props"
        )
        rows = await self.raw_query(
            cypher, {"name": entity_name, "namespace": namespace}, namespace
        )

        entities_by_id: dict[str, Entity] = {}
        relations: list[Relation] = []

        for row in rows:
            for node_key in ("src_node", "tgt_node"):
                nd = row.get(node_key, {})
                if not nd:
                    continue
                nid = str(nd.get("uuid", nd.get("id", "")))
                if nid and nid not in entities_by_id:
                    entities_by_id[nid] = Entity(
                        id=nid,
                        name=nd.get("name", ""),
                        entity_type=nd.get("entity_type", nd.get("type", "unknown")),
                        namespace=nd.get("namespace", namespace),
                        attributes={
                            k: v for k, v in nd.items()
                            if k not in {"uuid", "id", "name", "entity_type", "type", "namespace", "created_at"}
                        },
                        created_at=nd.get("created_at", _now()),
                    )

            src_node = row.get("src_node", {})
            tgt_node = row.get("tgt_node", {})
            rel_type = row.get("rel_type", "RELATED_TO")
            rel_props = row.get("rel_props", {}) or {}

            src_id = str(src_node.get("uuid", src_node.get("id", "")))
            tgt_id = str(tgt_node.get("uuid", tgt_node.get("id", "")))
            if src_id and tgt_id:
                relations.append(
                    Relation(
                        source_entity_id=src_id,
                        target_entity_id=tgt_id,
                        relation_type=rel_type,
                        namespace=namespace,
                        weight=float(rel_props.get("weight", 1.0)),
                        attributes={k: v for k, v in rel_props.items() if k != "weight"},
                    )
                )

        return Graph(entities=list(entities_by_id.values()), relations=relations)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def search_episodes(
        self, query: str, namespace: str, top_k: int = 10
    ) -> list[MemoryEntry]:
        """Semantic search over Graphiti episodes, filtered by namespace.

        Returns a list of ``MemoryEntry`` objects ordered by relevance.
        """
        self._assert_ready()
        logger.debug("Graphiti search: query=%r namespace=%s top_k=%d", query, namespace, top_k)
        raw_results = await self._graphiti.search(query)

        memories: list[MemoryEntry] = []
        for item in raw_results:
            if item is None:
                continue
            # Graphiti can return objects or dicts
            if isinstance(item, dict):
                node_data = item
            else:
                node_data = {
                    k: getattr(item, k, None)
                    for k in ("uuid", "id", "content", "episode_body", "namespace",
                              "created_at", "updated_at", "tags", "source", "metadata")
                    if getattr(item, k, None) is not None
                }

            # Namespace filter — skip items that don't belong to this namespace
            item_ns = node_data.get("namespace", "")
            if item_ns and item_ns != namespace:
                continue

            memories.append(_parse_memory_from_node(node_data, namespace))
            if len(memories) >= top_k:
                break

        return memories

    # ------------------------------------------------------------------
    # Raw Cypher
    # ------------------------------------------------------------------

    async def raw_query(
        self, cypher: str, params: dict, namespace: str
    ) -> list[dict]:
        """Execute a read-only Cypher query and return results as dicts.

        Only queries beginning with MATCH, CALL, WITH, or RETURN are
        permitted to prevent accidental writes.
        """
        self._assert_ready()
        stripped = cypher.strip()
        if not _READ_ONLY_START.match(stripped):
            raise ValueError(
                "raw_query only allows read-only Cypher statements "
                "(must start with MATCH, CALL, WITH, or RETURN). "
                f"Received: {stripped[:60]!r}"
            )

        # Inject namespace into params so callers can use $namespace in Cypher
        full_params = {"namespace": namespace, **params}

        driver = self._driver
        if driver is None:
            # Fallback: try to get driver from graphiti attribute
            driver = getattr(self._graphiti, "driver", None)
        if driver is None:
            raise RuntimeError(
                "No Neo4j driver available. Ensure Graphiti was initialised correctly."
            )

        logger.debug("Cypher query: %s | params keys: %s", stripped[:120], list(full_params.keys()))

        async with driver.session(database=self._config.database) as session:
            result = await session.run(cypher, full_params)
            records = await result.data()
            return records
