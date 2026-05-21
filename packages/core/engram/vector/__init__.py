"""engram.vector — Vector store sub-package."""

from engram.vector.embedder import Embedder, OpenAIEmbedder, LocalEmbedder, get_embedder
from engram.vector.qdrant_client import EngramQdrantClient

__all__ = [
    "Embedder",
    "OpenAIEmbedder",
    "LocalEmbedder",
    "get_embedder",
    "EngramQdrantClient",
]
