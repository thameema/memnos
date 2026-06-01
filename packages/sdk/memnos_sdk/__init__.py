"""memnos-sdk — memory layer for AI agents."""
from memnos_sdk.client import AsyncMemnosClient, MemnosClient
from memnos_sdk.models import (
    Memory,
    MemoryType,
    SearchResult,
    HealthStatus,
    CorpusInfo,
    CorpusStatus,
    ConstraintHit,
    CheckResult,
)
from memnos_sdk.corpus import AsyncCorpusClient, SyncCorpusClient
from memnos_sdk.exceptions import (
    MemnosError,
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
    "MemnosClient",
    "AsyncMemnosClient",
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
    "MemnosError",
    "AuthenticationError",
    "NotFoundError",
    "ValidationError",
    "ServerError",
    "ConnectionError",
    "__version__",
    "SCHEMA_VERSION",
]
