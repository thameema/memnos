"""engram-sdk — memory layer for AI agents."""
from engram_sdk.client import AsyncEngramClient, EngramClient
from engram_sdk.models import (
    Memory,
    MemoryType,
    SearchResult,
    HealthStatus,
    CorpusInfo,
    CorpusStatus,
    ConstraintHit,
    CheckResult,
)
from engram_sdk.corpus import AsyncCorpusClient, SyncCorpusClient
from engram_sdk.exceptions import (
    EngramError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    ServerError,
    ConnectionError,
)

__version__ = "1.1.0"
SCHEMA_VERSION = "1.0"

__all__ = [
    # Clients
    "EngramClient",
    "AsyncEngramClient",
    # Memory models
    "Memory",
    "MemoryType",
    "SearchResult",
    "HealthStatus",
    # Corpus models
    "CorpusInfo",
    "CorpusStatus",
    "ConstraintHit",
    "CheckResult",
    # Corpus sub-clients (advanced use)
    "AsyncCorpusClient",
    "SyncCorpusClient",
    # Exceptions
    "EngramError",
    "AuthenticationError",
    "NotFoundError",
    "ValidationError",
    "ServerError",
    "ConnectionError",
    "__version__",
    "SCHEMA_VERSION",
]
