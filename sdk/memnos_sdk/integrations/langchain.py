"""LangChain adapter — memnos as a retriever (and a tiny save helper).

    pip install 'memnos-sdk[langchain]'
    from memnos_sdk import MemnosClient
    from memnos_sdk.integrations.langchain import MemnosRetriever

    mem = MemnosClient(token="mnk_...", namespace="org:acme")
    retriever = MemnosRetriever(client=mem)          # drop into any chain / RAG pipeline
    docs = retriever.invoke("what database did we choose?")

Built on `langchain_core` (stable) — no heavy langchain deps. Retrieval uses memnos's
hybrid + reranked recall (no LLM at query time); each memory becomes a Document.
"""
from __future__ import annotations

from typing import List

try:
    from langchain_core.callbacks import CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
except ImportError as e:  # pragma: no cover
    raise ImportError("LangChain integration needs langchain_core: pip install 'memnos-sdk[langchain]'") from e

from ..client import MemnosClient


class MemnosRetriever(BaseRetriever):
    """A LangChain retriever backed by memnos recall."""

    client: MemnosClient
    namespace: str | None = None
    k: int = 8

    class Config:
        arbitrary_types_allowed = True

    def _get_relevant_documents(self, query: str, *, run_manager: "CallbackManagerForRetrieverRun" = None
                                ) -> List[Document]:
        rows = self.client.recall(query, namespace=self.namespace).get("memories", [])
        docs = []
        for r in rows[: self.k]:
            md = {"kind": r.get("kind"), "score": r.get("score")}
            if r.get("date"):
                md["date"] = r["date"]
            docs.append(Document(page_content=r.get("content", ""), metadata=md))
        return docs

    def save(self, text: str, *, namespace: str | None = None) -> dict:
        """Convenience: write a memory (LangChain has no standard 'write' on retrievers)."""
        return self.client.remember(text, namespace=namespace or self.namespace)
