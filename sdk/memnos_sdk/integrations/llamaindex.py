"""LlamaIndex adapter — memnos as a retriever (and a tiny save helper).

    uv pip install 'memnos-sdk[llamaindex]'    # (or: pip install 'memnos-sdk[llamaindex]')
    from memnos_sdk import MemnosClient
    from memnos_sdk.integrations.llamaindex import MemnosRetriever

    mem = MemnosClient(token="mnk_...", namespace="org:acme")
    retriever = MemnosRetriever(client=mem)            # drop into any LlamaIndex query engine
    nodes = retriever.retrieve("what database did we choose?")

Built on `llama_index.core` (stable). Retrieval uses memnos's hybrid + reranked recall
(no LLM at query time); each memory becomes a TextNode with its score + metadata.
"""
from __future__ import annotations

from typing import List, Optional

try:
    from llama_index.core.retrievers import BaseRetriever
    from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
except ImportError as e:  # pragma: no cover
    raise ImportError("LlamaIndex integration needs llama-index-core: "
                      "uv pip install 'memnos-sdk[llamaindex]' "
                      "(or: pip install 'memnos-sdk[llamaindex]')") from e

from ..client import MemnosClient


class MemnosRetriever(BaseRetriever):
    """A LlamaIndex retriever backed by memnos recall."""

    def __init__(self, client: MemnosClient, namespace: Optional[str] = None, k: int = 8,
                 callback_manager=None):
        self.client = client
        self.namespace = namespace
        self.k = k
        super().__init__(callback_manager=callback_manager)

    def _retrieve(self, query_bundle: "QueryBundle") -> List["NodeWithScore"]:
        q = query_bundle.query_str if hasattr(query_bundle, "query_str") else str(query_bundle)
        rows = self.client.recall(q, namespace=self.namespace).get("memories", [])
        nodes: List[NodeWithScore] = []
        for r in rows[: self.k]:
            md = {"kind": r.get("kind")}
            if r.get("date"):
                md["date"] = r["date"]
            node = TextNode(text=r.get("content", ""), metadata=md)
            nodes.append(NodeWithScore(node=node, score=r.get("score")))
        return nodes

    def save(self, text: str, *, namespace: Optional[str] = None) -> dict:
        """Convenience: write a memory (LlamaIndex retrievers have no standard 'write')."""
        return self.client.remember(text, namespace=namespace or self.namespace)
