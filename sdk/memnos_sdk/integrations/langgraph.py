"""LangGraph adapter — memnos as a long-term-memory `BaseStore`.

    pip install 'memnos-sdk[langgraph]'
    from memnos_sdk import MemnosClient
    from memnos_sdk.integrations.langgraph import MemnosStore

    store = MemnosStore(MemnosClient(token="mnk_..."))
    graph = builder.compile(store=store)     # nodes get long-term memory via store.search/put

Mapping (memnos is SEMANTIC memory, not a KV store):
  • Put(namespace, key, value)      -> remember(value)     (key kept inline so it's searchable)
  • Search(namespace, query)        -> recall(query)       (hybrid + reranked, no LLM at query time)
  • Get(namespace, key)             -> best-effort via search; None if not found
  • ListNamespaces                  -> [] (memnos doesn't enumerate per-key namespaces here)

LangGraph namespace tuples are joined with ':' into a memnos namespace.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

try:
    from langgraph.store.base import (BaseStore, GetOp, Item, ListNamespacesOp, PutOp,
                                      SearchItem, SearchOp)
except ImportError as e:  # pragma: no cover
    raise ImportError("LangGraph integration needs langgraph: pip install 'memnos-sdk[langgraph]'") from e

from ..client import AsyncMemnosClient, MemnosClient


def _ns(tup):
    return ":".join(str(p) for p in tup) if tup else "default"


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, default=str)


class MemnosStore(BaseStore):
    """A LangGraph BaseStore backed by memnos semantic memory."""

    def __init__(self, client: MemnosClient, async_client: AsyncMemnosClient | None = None):
        self.client = client
        self.aclient = async_client

    # --- sync ---
    def batch(self, ops):
        out = []
        for op in ops:
            if isinstance(op, PutOp):
                if op.value is None:                       # delete — memnos has no key delete; no-op
                    out.append(None)
                else:
                    self.client.remember(f"[{op.key}] {_text(op.value)}", namespace=_ns(op.namespace))
                    out.append(None)
            elif isinstance(op, SearchOp):
                rows = self.client.recall(op.query or "", namespace=_ns(op.namespace_prefix)).get("memories", [])
                out.append(self._to_items(op.namespace_prefix, rows, getattr(op, "limit", 10)))
            elif isinstance(op, GetOp):
                rows = self.client.recall(str(op.key), namespace=_ns(op.namespace)).get("memories", [])
                out.append(self._to_items(op.namespace, rows, 1)[0] if rows else None)
            elif isinstance(op, ListNamespacesOp):
                out.append([])
            else:
                out.append(None)
        return out

    # --- async ---
    async def abatch(self, ops):
        if self.aclient is None:
            return self.batch(ops)
        out = []
        for op in ops:
            if isinstance(op, PutOp):
                if op.value is not None:
                    await self.aclient.remember(f"[{op.key}] {_text(op.value)}", namespace=_ns(op.namespace))
                out.append(None)
            elif isinstance(op, SearchOp):
                r = await self.aclient.recall(op.query or "", namespace=_ns(op.namespace_prefix))
                out.append(self._to_items(op.namespace_prefix, r.get("memories", []), getattr(op, "limit", 10)))
            elif isinstance(op, GetOp):
                r = await self.aclient.recall(str(op.key), namespace=_ns(op.namespace))
                rows = r.get("memories", [])
                out.append(self._to_items(op.namespace, rows, 1)[0] if rows else None)
            elif isinstance(op, ListNamespacesOp):
                out.append([])
            else:
                out.append(None)
        return out

    def _to_items(self, namespace, rows, limit):
        now = datetime.now(timezone.utc)
        items = []
        for i, r in enumerate(rows[: (limit or 10)]):
            try:
                items.append(SearchItem(namespace=tuple(namespace), key=str(i),
                                        value={"content": r.get("content", ""), "kind": r.get("kind")},
                                        created_at=now, updated_at=now, score=r.get("score")))
            except TypeError:   # SearchItem signature drift across langgraph versions
                items.append(Item(namespace=tuple(namespace), key=str(i),
                                  value={"content": r.get("content", "")}, created_at=now, updated_at=now))
        return items
