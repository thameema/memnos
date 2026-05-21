"""
engram — Persistent memory and multi-agent orchestration layer for LLM workflows.

Primary exports
---------------
EngramClient  — high-level async client (start/stop, add, search, delete, ...)
EngramConfig  — configuration object loaded from engram.yaml
Models        — MemoryEntry, Entity, Relation, Fact, Graph, SearchResult, Namespace
"""

from engram.client import EngramClient
from engram.config import (
    ApiRuntimeConfig,
    EmbeddingsConfig,
    EngramConfig,
    LearningConfig,
    NamespaceConfig,
    Neo4jConfig,
    OpenRouterConfig,
    QdrantConfig,
    RuntimeConfig,
    ServerConfig,
)
from engram.models import (
    Entity,
    Fact,
    Graph,
    MemoryEntry,
    Namespace,
    Relation,
    SearchResult,
)

__all__ = [
    # Client
    "EngramClient",
    # Config
    "EngramConfig",
    "ServerConfig",
    "Neo4jConfig",
    "QdrantConfig",
    "EmbeddingsConfig",
    "RuntimeConfig",
    "ApiRuntimeConfig",
    "OpenRouterConfig",
    "NamespaceConfig",
    "LearningConfig",
    # Models
    "MemoryEntry",
    "Entity",
    "Relation",
    "Fact",
    "Graph",
    "SearchResult",
    "Namespace",
]

__version__ = "0.1.0"
