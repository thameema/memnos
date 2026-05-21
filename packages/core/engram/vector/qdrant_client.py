"""
engram.vector.qdrant_client — Async Qdrant wrapper for engram vector storage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from engram.config import QdrantConfig

logger = logging.getLogger(__name__)

# Vector sizes by provider / model family
_VECTOR_SIZE_OPENAI = 1536
_VECTOR_SIZE_LOCAL = 384


class EngramQdrantClient:
    """Thin async wrapper around ``qdrant_client.AsyncQdrantClient``."""

    def __init__(self, config: "QdrantConfig", vector_size: int = _VECTOR_SIZE_OPENAI) -> None:
        self._config = config
        self._vector_size = vector_size
        self._client: Any = None  # qdrant_client.AsyncQdrantClient

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Connect to Qdrant and create the collection if it does not exist."""
        try:
            from qdrant_client import AsyncQdrantClient  # type: ignore
            from qdrant_client.models import Distance, VectorParams  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "qdrant-client package is required. Install: pip install qdrant-client"
            ) from exc

        self._client = AsyncQdrantClient(host=self._config.host, port=self._config.port)
        logger.info(
            "Connecting to Qdrant at %s:%d", self._config.host, self._config.port
        )

        collection_name = self._config.collection
        existing = await self._client.get_collections()
        existing_names = {c.name for c in existing.collections}

        if collection_name not in existing_names:
            logger.info(
                "Creating Qdrant collection %r (vector_size=%d)", collection_name, self._vector_size
            )
            await self._client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
            )
        else:
            logger.debug("Qdrant collection %r already exists", collection_name)

    async def close(self) -> None:
        """Close the underlying Qdrant connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.debug("Qdrant client closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_ready(self) -> None:
        if self._client is None:
            raise RuntimeError("EngramQdrantClient.init() must be called before use")

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def upsert(self, memory_id: str, vector: list[float], payload: dict) -> str:
        """Insert or update a vector point.

        Parameters
        ----------
        memory_id:
            Unique string ID for the point (used as the point ID in Qdrant).
        vector:
            The embedding vector.
        payload:
            Arbitrary metadata stored alongside the vector (must include
            ``namespace`` key for filtering).

        Returns
        -------
        str
            The ``memory_id`` passed in.
        """
        self._assert_ready()
        try:
            from qdrant_client.models import PointStruct  # type: ignore
        except ImportError as exc:
            raise ImportError("qdrant-client required") from exc

        point = PointStruct(id=memory_id, vector=vector, payload=payload)
        await self._client.upsert(
            collection_name=self._config.collection,
            points=[point],
            wait=True,
        )
        logger.debug("Qdrant upsert: point_id=%s namespace=%s", memory_id, payload.get("namespace"))
        return memory_id

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def search(
        self,
        vector: list[float],
        namespace: str,
        top_k: int = 10,
        filter_tags: list[str] | None = None,
    ) -> list[tuple[str, float, dict]]:
        """Search for similar vectors filtered by namespace.

        Parameters
        ----------
        vector:
            Query embedding.
        namespace:
            Only return points whose payload ``namespace`` matches.
        top_k:
            Maximum number of results.
        filter_tags:
            Optional list of tags; points must have ALL tags in their payload.

        Returns
        -------
        list[tuple[str, float, dict]]
            List of ``(point_id, score, payload)`` tuples, ordered by
            descending similarity score.
        """
        self._assert_ready()
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue, MatchAny  # type: ignore
        except ImportError as exc:
            raise ImportError("qdrant-client required") from exc

        must_conditions: list[Any] = [
            FieldCondition(key="namespace", match=MatchValue(value=namespace))
        ]

        if filter_tags:
            for tag in filter_tags:
                must_conditions.append(
                    FieldCondition(key="tags", match=MatchAny(any=[tag]))
                )

        query_filter = Filter(must=must_conditions)

        results = await self._client.search(
            collection_name=self._config.collection,
            query_vector=vector,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        return [(str(r.id), r.score, r.payload or {}) for r in results]

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def delete(self, point_id: str) -> None:
        """Delete a single point by ID."""
        self._assert_ready()
        try:
            from qdrant_client.models import PointIdsList  # type: ignore
        except ImportError as exc:
            raise ImportError("qdrant-client required") from exc

        await self._client.delete(
            collection_name=self._config.collection,
            points_selector=PointIdsList(points=[point_id]),
            wait=True,
        )
        logger.debug("Qdrant delete: point_id=%s", point_id)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def get(self, point_id: str) -> dict | None:
        """Retrieve a single point's payload by ID.

        Returns
        -------
        dict | None
            The payload dict, or ``None`` if the point does not exist.
        """
        self._assert_ready()
        results = await self._client.retrieve(
            collection_name=self._config.collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if not results:
            return None
        return results[0].payload or {}
