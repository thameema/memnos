"""
engram.corpus.connectors.git_doc — Connector for git repositories containing docs.

Crawls a local directory (typically a cloned git repo) matching a glob pattern
and extracts typed constraint/decision/fact nodes from markdown files.

Usage::

    connector = GitDocConnector(
        corpus_id="hdig-platform",
        namespace="org:hc:hdig:architecture",
        source_path="/path/to/hdig-platform/docs",
        path_pattern="**/*.md",
    )
    result = await connector.sync(engram_client)
    print(f"Synced {result.nodes_written} nodes (sha={result.git_sha})")

Source tracking
---------------
Detects the current git HEAD SHA from ``source_path`` and stores it in each
node's metadata.  On re-sync, the caller can compare last_sync_sha against the
current SHA to decide whether to skip (no-op) or proceed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from engram.corpus.connectors.base import ConnectorBase, ConnectorResult, ConnectorType
from engram.corpus.extractor import ExtractedNode, extract_corpus

logger = logging.getLogger(__name__)


class GitDocConnector(ConnectorBase):
    """Extracts typed knowledge nodes from markdown docs in a git repository.

    Supports any local directory; git integration is best-effort (falls back
    gracefully when git is not available or the path is not a git repo).
    """

    connector_type = ConnectorType.GIT_DOC
    display_name   = "Git Documentation"
    description    = (
        "Extracts constraint/decision/fact nodes from markdown files in a "
        "local git repository.  Auto-detects the current HEAD SHA for "
        "staleness tracking."
    )

    def __init__(
        self,
        corpus_id: str,
        namespace: str,
        source_path: str,
        path_pattern: str = "**/*.md",
    ) -> None:
        super().__init__(corpus_id=corpus_id, namespace=namespace)
        self.source_path = Path(source_path)
        self.path_pattern = path_pattern
        self._current_sha: str = ""

    def detect_sha(self) -> str:
        """Return the current git HEAD short SHA for source_path (best-effort)."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.source_path), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    async def extract(self) -> list[ExtractedNode]:
        if not self.source_path.exists():
            raise FileNotFoundError(
                f"GitDocConnector: source_path does not exist: {self.source_path}"
            )
        self._current_sha = self.detect_sha()
        return extract_corpus(
            source_path=self.source_path,
            path_pattern=self.path_pattern,
            corpus_id=self.corpus_id,
            namespace=self.namespace,
            git_sha=self._current_sha,
        )

    async def sync(self, client) -> ConnectorResult:
        result = await super().sync(client)
        result.git_sha = self._current_sha
        return result

    def _source_label(self) -> str:
        return str(self.source_path)
