"""
engram.corpus.connectors.openapi — Connector for OpenAPI 3.x specifications.

Ingests an OpenAPI spec (YAML or JSON) and extracts:
  - fact nodes     for every endpoint (path + method + summary + parameters)
  - constraint nodes for required fields, auth schemes, and response contracts

This connector is a stub — the extraction logic is not yet implemented.
It is included to illustrate the connector extension pattern and to serve
as a scaffold for the community.

To implement::

    class OpenAPIConnector(ConnectorBase):
        async def extract(self) -> list[ExtractedNode]:
            spec = yaml.safe_load(Path(self.spec_path).read_text())
            nodes = []
            for path, methods in spec.get("paths", {}).items():
                for method, op in methods.items():
                    nodes.append(ExtractedNode(
                        content=f"[CONTRACT] {method.upper()} {path}: {op.get('summary','')}",
                        memory_type="fact",
                        ...
                    ))
            return nodes
"""

from __future__ import annotations

import logging

from engram.corpus.connectors.base import ConnectorBase, ConnectorType
from engram.corpus.extractor import ExtractedNode

logger = logging.getLogger(__name__)


class OpenAPIConnector(ConnectorBase):
    """Extracts contract fact nodes from an OpenAPI 3.x specification file.

    Status: stub — extract() raises NotImplementedError until implemented.
    Register your own subclass in REGISTRY to override.
    """

    connector_type = ConnectorType.OPENAPI
    display_name   = "OpenAPI Specification"
    description    = (
        "Extracts endpoint contract nodes and security constraint nodes "
        "from an OpenAPI 3.x YAML or JSON specification."
    )

    def __init__(
        self,
        corpus_id: str,
        namespace: str,
        spec_path: str,
    ) -> None:
        super().__init__(corpus_id=corpus_id, namespace=namespace)
        self.spec_path = spec_path

    async def extract(self) -> list[ExtractedNode]:
        raise NotImplementedError(
            "OpenAPIConnector.extract() is not yet implemented. "
            "Subclass OpenAPIConnector and implement extract() to add OpenAPI support."
        )
