"""
engram.storage.qdrant_backend — Qdrant vector backend implementation.

Each engram Memory is stored as a Qdrant point:
  point_id  : the memory UUID string
  vector    : the embedding (list[float])
  payload   : {namespace, memory_type, superseded}

Namespace filtering uses exact match only (Qdrant does not support prefix
matching natively). Parent-namespace expansion is handled upstream in
EngramClient.search() via the existing while-loop retry.

Requires: qdrant-client >= 1.9  (``pip install 'qdrant-client>=1.9'``)
"""

from __future__ import annotations

import logging
from typing import Any

from engram.storage.vector_backend import VectorBackend

logger = logging.getLogger(__name__)

_SUPERSEDED_FIELD = "superseded"
_NAMESPACE_FIELD  = "namespace"


class QdrantVectorBackend(VectorBackend):
    """
    VectorBackend implementation backed by a Qdrant collection.

    The collection is created automatically on first use if it does not exist.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        collection: str = "engram_memories",
        vector_dim: int = 1536,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._collection = collection
        self._vector_dim = vector_dim
        self._client: Any = None  # qdrant_client.AsyncQdrantClient

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import AsyncQdrantClient  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "qdrant-client is required for the Qdrant vector backend. "
                    "Install it with: pip install 'qdrant-client>=1.9'"
                ) from exc
            kwargs: dict[str, Any] = {"url": self._url}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = AsyncQdrantClient(**kwargs)
            await self._ensure_collection()
        return self._client

    async def _ensure_collection(self) -> None:
        from qdrant_client.models import VectorParams, Distance  # type: ignore

        client = self._client
        existing = await client.get_collections()
        names = [c.name for c in (existing.collections or [])]
        if self._collection not in names:
            await client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._vector_dim, distance=Distance.COSINE),
            )
            logger.info(
                "Qdrant: created collection '%s' (dim=%d, cosine)",
                self._collection, self._vector_dim,
            )
        else:
            logger.debug("Qdrant: collection '%s' already exists", self._collection)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:
                logger.debug("Qdrant close error (ignored): %s", exc)
            finally:
                self._client = None

    # ------------------------------------------------------------------
    # VectorBackend interface
    # ------------------------------------------------------------------

    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        namespace: str,
        memory_type: str = "fact",
    ) -> None:
        from qdrant_client.models import PointStruct  # type: ignore

        client = await self._ensure_client()
        point = PointStruct(
            id=memory_id,
            vector=embedding,
            payload={
                _NAMESPACE_FIELD: namespace,
                "memory_type": memory_type,
                _SUPERSEDED_FIELD: False,
            },
        )
        await client.upsert(collection_name=self._collection, points=[point])
        logger.debug("Qdrant upsert: %s in %s", memory_id, namespace)

    async def search(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 10,
        include_superseded: bool = False,
    ) -> list[tuple[str, float]]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue  # type: ignore

        client = await self._ensure_client()

        # When namespace is "all" / "" / "*" the caller wants a global search;
        # ACL filtering is applied upstream — skip the namespace clause here.
        _cross_ns = namespace.lower().strip() in ("", "all", "*")

        conditions = []
        if not _cross_ns:
            conditions.append(
                FieldCondition(key=_NAMESPACE_FIELD, match=MatchValue(value=namespace))
            )
        if not include_superseded:
            conditions.append(
                FieldCondition(key=_SUPERSEDED_FIELD, match=MatchValue(value=False))
            )

        query_filter = Filter(must=conditions) if conditions else None

        # qdrant-client >= 1.10 replaced search() with query_points()
        result = await client.query_points(
            collection_name=self._collection,
            query=embedding,
            query_filter=query_filter,
            limit=top_k,
            with_payload=False,
            with_vectors=False,
        )
        return [(str(h.id), float(h.score)) for h in result.points]

    async def delete(self, memory_id: str) -> None:
        from qdrant_client.models import PointIdsList  # type: ignore

        client = await self._ensure_client()
        await client.delete(
            collection_name=self._collection,
            points_selector=PointIdsList(points=[memory_id]),
        )
        logger.debug("Qdrant delete: %s", memory_id)

    async def mark_superseded(self, memory_id: str) -> None:
        from qdrant_client.models import SetPayload  # type: ignore

        client = await self._ensure_client()
        await client.set_payload(
            collection_name=self._collection,
            payload={_SUPERSEDED_FIELD: True},
            points=[memory_id],
        )
        logger.debug("Qdrant mark_superseded: %s", memory_id)
