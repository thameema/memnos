"""engram.corpus.connectors — pluggable source connectors for corpus ingestion."""

from engram.corpus.connectors.base import ConnectorBase, ConnectorResult, ConnectorType
from engram.corpus.connectors.git_doc import GitDocConnector

REGISTRY: dict[str, type[ConnectorBase]] = {
    ConnectorType.GIT_DOC: GitDocConnector,
}


def get_connector(connector_type: str, **kwargs) -> ConnectorBase:
    """Instantiate a connector by type name. Raises ValueError for unknown types."""
    cls = REGISTRY.get(connector_type)
    if cls is None:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(
            f"Unknown connector type {connector_type!r}. Known types: {known}"
        )
    return cls(**kwargs)


__all__ = [
    "ConnectorBase",
    "ConnectorResult",
    "ConnectorType",
    "GitDocConnector",
    "REGISTRY",
    "get_connector",
]
