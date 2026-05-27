from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

from engram_sdk._http import _AsyncTransport, _SyncTransport
from engram_sdk.models import HealthStatus, Memory, MemoryType


def _parse_memory(data: dict) -> Memory:
    raw_type = data.get("memory_type", "fact")
    try:
        mem_type = MemoryType(raw_type)
    except ValueError:
        mem_type = MemoryType.FACT
    return Memory(
        id=data["id"],
        content=data["content"],
        namespace=data["namespace"],
        memory_type=mem_type,
        tags=data.get("tags") or [],
        affects=data.get("affects") or [],
        rationale=data.get("rationale") or "",
        author=data.get("author") or "",
        created_at=data["created_at"],
        score=data.get("score"),
        provenance=data.get("provenance") or {},
        contradiction_warnings=data.get("contradiction_warnings") or [],
    )


def _write_payload(
    content: str,
    namespace: str,
    memory_type: MemoryType | str,
    tags: list[str],
    affects: list[str],
    rationale: str,
    author: str,
    source: str,
    metadata: dict,
    expires_at: datetime | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": content,
        "namespace": namespace,
        "memory_type": memory_type.value if isinstance(memory_type, MemoryType) else memory_type,
        "tags": tags,
        "affects": affects,
        "rationale": rationale,
        "author": author,
        "source": source,
        "metadata": metadata,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at.isoformat()
    return payload


def _search_params(
    query: str,
    namespace: str,
    top_k: int,
    as_of: datetime | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"q": query, "ns": namespace, "top_k": top_k}
    if as_of is not None:
        params["as_of"] = as_of.isoformat()
    return params


class AsyncEngramClient:
    def __init__(
        self,
        url: str = "http://localhost:8766",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._transport = _AsyncTransport(url, api_key, timeout)

    async def __aenter__(self) -> "AsyncEngramClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._transport.aclose()

    async def health(self) -> HealthStatus:
        data = await self._transport.get("/api/v1/admin/health")
        return HealthStatus(
            status=data.get("status", "unknown"),
            arcadedb=data.get("arcadedb", "unknown"),
            version=data.get("version", ""),
            schema_version=data.get("schema_version", "1.0"),
        )

    async def write(
        self,
        content: str,
        namespace: str,
        *,
        memory_type: MemoryType | str = MemoryType.FACT,
        tags: list[str] = [],
        affects: list[str] = [],
        rationale: str = "",
        author: str = "",
        source: str = "sdk",
        metadata: dict = {},
        expires_at: datetime | None = None,
    ) -> Memory:
        data = await self._transport.post(
            "/api/v1/memory/",
            json=_write_payload(
                content, namespace, memory_type, tags, affects,
                rationale, author, source, metadata, expires_at,
            ),
        )
        return _parse_memory(data)

    async def search(
        self,
        query: str,
        namespace: str,
        *,
        top_k: int = 10,
        as_of: datetime | None = None,
    ) -> list[Memory]:
        data = await self._transport.get(
            "/api/v1/memory/search",
            params=_search_params(query, namespace, top_k, as_of),
        )
        return [_parse_memory(item) for item in data]

    async def get(self, memory_id: str) -> Memory:
        data = await self._transport.get(f"/api/v1/memory/{memory_id}")
        return _parse_memory(data)

    async def delete(self, memory_id: str) -> None:
        await self._transport.delete(f"/api/v1/memory/{memory_id}")

    async def get_constraints(self, namespace: str) -> list[Memory]:
        """Return all active constraints for a namespace (score=2.0, always governs)."""
        results = await self.search(
            "constraints rules must always", namespace=namespace, top_k=50
        )
        return [m for m in results if m.memory_type == MemoryType.CONSTRAINT]

    async def get_governing_decisions(
        self, entities: list[str], namespace: str
    ) -> list[Memory]:
        """Return decisions/ADRs whose affects[] overlaps with the given entity names."""
        query = " ".join(entities)
        results = await self.search(query, namespace=namespace, top_k=100)
        entity_set = {e.lower() for e in entities}
        return [
            m for m in results
            if m.memory_type in (MemoryType.DECISION, MemoryType.ADR)
            and entity_set & {a.lower() for a in m.affects}
        ]

    async def export_namespace(
        self,
        namespace: str,
        *,
        memory_type: str | None = None,
        include_superseded: bool = False,
    ) -> dict:
        """Export all memories in a namespace. Returns the export envelope dict."""
        params: dict[str, Any] = {"ns": namespace, "format": "json"}
        if memory_type:
            params["memory_type"] = memory_type
        if include_superseded:
            params["include_superseded"] = "true"
        return await self._transport.get("/api/v1/admin/export", params=params)

    async def import_namespace(
        self,
        data: dict,
        *,
        target_namespace: str | None = None,
    ) -> dict:
        """Import memories from an export envelope. Returns {imported, skipped, namespace}."""
        path = "/api/v1/admin/import"
        if target_namespace:
            from urllib.parse import quote
            path = f"{path}?ns={quote(target_namespace, safe='')}"
        return await self._transport.post(path, json=data)

    async def list_namespaces(self) -> list[str]:
        """Return all configured namespace names."""
        data = await self._transport.get("/api/v1/admin/namespaces")
        if isinstance(data, list):
            return [item["name"] if isinstance(item, dict) else item for item in data]
        return []


class EngramClient:
    """Synchronous wrapper around AsyncEngramClient. Suitable for scripts and non-async code.

    Usage:
        client = EngramClient(url="http://localhost:8766", api_key="my-key")
        with client:
            memories = client.search("database decisions", "org:acme:engineering")
    """

    def __init__(
        self,
        url: str = "http://localhost:8766",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._async_client = AsyncEngramClient(url=url, api_key=api_key, timeout=timeout)
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()

    def _run(self, coro):
        with self._lock:
            return self._loop.run_until_complete(coro)

    def __enter__(self) -> "EngramClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._run(self._async_client._transport.aclose())
        self._loop.close()

    def health(self) -> HealthStatus:
        return self._run(self._async_client.health())

    def write(
        self,
        content: str,
        namespace: str,
        *,
        memory_type: MemoryType | str = MemoryType.FACT,
        tags: list[str] = [],
        affects: list[str] = [],
        rationale: str = "",
        author: str = "",
        source: str = "sdk",
        metadata: dict = {},
        expires_at: datetime | None = None,
    ) -> Memory:
        return self._run(
            self._async_client.write(
                content, namespace,
                memory_type=memory_type, tags=tags, affects=affects,
                rationale=rationale, author=author, source=source,
                metadata=metadata, expires_at=expires_at,
            )
        )

    def search(
        self,
        query: str,
        namespace: str,
        *,
        top_k: int = 10,
        as_of: datetime | None = None,
    ) -> list[Memory]:
        return self._run(
            self._async_client.search(query, namespace, top_k=top_k, as_of=as_of)
        )

    def get(self, memory_id: str) -> Memory:
        return self._run(self._async_client.get(memory_id))

    def delete(self, memory_id: str) -> None:
        self._run(self._async_client.delete(memory_id))

    def get_constraints(self, namespace: str) -> list[Memory]:
        return self._run(self._async_client.get_constraints(namespace))

    def get_governing_decisions(
        self, entities: list[str], namespace: str
    ) -> list[Memory]:
        return self._run(
            self._async_client.get_governing_decisions(entities, namespace)
        )

    def export_namespace(
        self,
        namespace: str,
        *,
        memory_type: str | None = None,
        include_superseded: bool = False,
    ) -> dict:
        return self._run(
            self._async_client.export_namespace(
                namespace,
                memory_type=memory_type,
                include_superseded=include_superseded,
            )
        )

    def import_namespace(
        self,
        data: dict,
        *,
        target_namespace: str | None = None,
    ) -> dict:
        return self._run(
            self._async_client.import_namespace(data, target_namespace=target_namespace)
        )

    def list_namespaces(self) -> list[str]:
        return self._run(self._async_client.list_namespaces())
