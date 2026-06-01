"""
memnos — Persistent memory and multi-agent orchestration layer for LLM workflows.

Primary exports
---------------
MemnosClient  — high-level async client (start/stop, add, search, delete, ...)
MemnosConfig  — configuration object loaded from memnos.yaml
Models        — MemoryEntry, Entity, Relation, Fact, Graph, SearchResult, Namespace
"""

from memnos.client import MemnosClient
from memnos.config import (
    ApiRuntimeConfig,
    ArcadeDBConfig,
    EmbeddingsConfig,
    MemnosConfig,
    LearningConfig,
    NamespaceConfig,
    OpenRouterConfig,
    RuntimeConfig,
    ServerConfig,
    VaultConfig,
)
from memnos.models import (
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
    "MemnosClient",
    # Config
    "MemnosConfig",
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
