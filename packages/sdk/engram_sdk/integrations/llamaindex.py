from __future__ import annotations

from typing import Any

try:
    from llama_index.core.readers.base import BaseReader
    from llama_index.core.schema import Document
    _LLAMAINDEX_AVAILABLE = True
except ImportError:
    _LLAMAINDEX_AVAILABLE = False
    BaseReader = object  # type: ignore[assignment,misc]
    Document = None  # type: ignore[assignment]


class EngramReader(BaseReader):
    """LlamaIndex reader that loads engram memories as Documents.

    Usage:
        client = EngramClient(url="...", api_key="...")
        reader = EngramReader(client=client, namespace="org:acme:engineering")
        documents = reader.load_data(query="database decisions", top_k=10)
        index = VectorStoreIndex.from_documents(documents)
    """

    def __init__(self, client: Any, namespace: str) -> None:
        if not _LLAMAINDEX_AVAILABLE:
            raise ImportError(
                "llama-index-core is required for EngramReader. "
                "Install it with: pip install 'engram-sdk[llamaindex]'"
            )
        self.client = client
        self.namespace = namespace

    def load_data(self, query: str, top_k: int = 10) -> list:
        memories = self.client.search(query, self.namespace, top_k=top_k)
        documents = []
        for m in memories:
            doc = Document(
                text=m.content,
                metadata={
                    "id": m.id,
                    "namespace": m.namespace,
                    "memory_type": m.memory_type.value,
                    "tags": m.tags,
                    "affects": m.affects,
                    "created_at": m.created_at.isoformat(),
                    "score": m.score,
                },
            )
            documents.append(doc)
        return documents
