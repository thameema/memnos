"""
engram.corpus.connectors.base — Abstract base class for corpus source connectors.

A Connector is a pluggable source of typed knowledge nodes.  Each connector
implementation knows how to crawl a specific source (a git repo of docs, an
OpenAPI spec, a Confluence space, a JIRA epic) and emit ExtractedNode objects.

Implementing a new connector
-----------------------------
1. Subclass ConnectorBase
2. Implement extract() — yield ExtractedNode objects
3. Register the class in engram.corpus.connectors.REGISTRY

Example::

    from engram.corpus.connectors.base import ConnectorBase, ConnectorResult
    from engram.corpus.extractor import ExtractedNode

    class ConfluenceConnector(ConnectorBase):
        connector_type = "confluence"
        display_name   = "Confluence Space"
        description    = "Ingests Confluence pages as constraint/decision/fact nodes."

        def __init__(self, space_key: str, base_url: str, token: str, **kwargs):
            super().__init__(**kwargs)
            self.space_key = space_key
            ...

        async def extract(self) -> list[ExtractedNode]:
            # fetch pages, parse, return nodes
            ...

    # Register in __init__.py:
    REGISTRY["confluence"] = ConfluenceConnector
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ConnectorType:
    """String constants for built-in connector types."""
    GIT_DOC  = "git-doc"    # markdown files in a git repository
    OPENAPI  = "openapi"    # OpenAPI 3.x spec → contract fact nodes
    # Future: CONFLUENCE = "confluence", JIRA = "jira", NOTION = "notion"


@dataclass
class ConnectorResult:
    """Summary returned by ConnectorBase.sync()."""
    connector_type: str
    source: str                     # human-readable source identifier
    nodes_written: int = 0
    nodes_failed: int = 0
    git_sha: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors or self.nodes_written > 0


class ConnectorBase(ABC):
    """Abstract base for all engram corpus connectors.

    Subclasses implement ``extract()`` to yield ``ExtractedNode`` objects.
    ``sync()`` wraps extract with write logic and returns a ``ConnectorResult``.
    """

    connector_type: str = "unknown"
    display_name: str   = "Unknown Connector"
    description: str    = ""

    def __init__(self, corpus_id: str, namespace: str) -> None:
        self.corpus_id = corpus_id
        self.namespace = namespace

    @abstractmethod
    async def extract(self) -> list["ExtractedNode"]:  # type: ignore[name-defined]
        """Extract typed knowledge nodes from the source.

        Returns a list of ExtractedNode objects.  Implementations MUST be
        idempotent — calling extract() twice on the same unchanged source
        MUST return semantically equivalent nodes.
        """
        ...

    async def sync(self, client) -> ConnectorResult:
        """Extract nodes and write them to engram via client.add().

        This default implementation works for all connectors.  Override only
        if you need custom write logic (e.g. bulk upsert, deduplication).
        """
        from engram.models import MemoryType, MemoryStatus, Provenance

        result = ConnectorResult(
            connector_type=self.connector_type,
            source=self._source_label(),
        )

        try:
            nodes = await self.extract()
        except Exception as exc:
            logger.exception("connector extract failed: %s", exc)
            result.errors.append(str(exc))
            return result

        for node in nodes:
            try:
                await client.add(
                    content=node.content,
                    namespace=self.namespace,
                    tags=node.tags,
                    source=f"corpus:{self.connector_type}",
                    memory_type=MemoryType(node.memory_type),
                    status=MemoryStatus.active,
                    metadata=node.metadata,
                    provenance=Provenance(tool=f"corpus-{self.connector_type}"),
                )
                result.nodes_written += 1
            except Exception as exc:
                logger.warning("connector write failed (non-fatal): %s", exc)
                result.nodes_failed += 1

        return result

    def _source_label(self) -> str:
        return f"{self.connector_type}:{self.corpus_id}"
