"""engram-sdk — memory layer for AI agents."""
from engram_sdk.client import AsyncEngramClient, EngramClient
from engram_sdk.models import Memory, MemoryType, SearchResult, HealthStatus
from engram_sdk.exceptions import (
    EngramError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    ServerError,
    ConnectionError,
)

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0"

__all__ = [
    "EngramClient",
    "AsyncEngramClient",
    "Memory",
    "MemoryType",
    "SearchResult",
    "HealthStatus",
    "EngramError",
    "AuthenticationError",
    "NotFoundError",
    "ValidationError",
    "ServerError",
    "ConnectionError",
    "__version__",
    "SCHEMA_VERSION",
]
