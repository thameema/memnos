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
    ArcadeDBConfig,
    EmbeddingsConfig,
    EngramConfig,
    LearningConfig,
    NamespaceConfig,
    OpenRouterConfig,
    RuntimeConfig,
    ServerConfig,
    VaultConfig,
)
from engram.models import (
    DecayPolicy,
    Entity,
    Fact,
    Graph,
    MemoryEntry,
    MemoryStatus,
    MemoryType,
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
    "ArcadeDBConfig",
    "EmbeddingsConfig",
    "RuntimeConfig",
    "ApiRuntimeConfig",
    "OpenRouterConfig",
    "NamespaceConfig",
    "LearningConfig",
    "VaultConfig",
    # Models
    "MemoryEntry",
    "MemoryType",
    "MemoryStatus",
    "DecayPolicy",
    "Entity",
    "Relation",
    "Fact",
    "Graph",
    "SearchResult",
    "Namespace",
]

__version__ = "0.2.0"
