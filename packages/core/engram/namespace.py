"""
engram.namespace — Namespace access control manager.

Namespace definitions are loaded from ``EngramConfig`` and evaluated at
runtime.  The wildcard value ``"*"`` in any reader/writer/owner list means
"all authenticated users".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.config import EngramConfig, NamespaceDefinition

logger = logging.getLogger(__name__)

_WILDCARD = "*"


class NamespaceManager:
    """Evaluate read/write permissions for (user_id, namespace) pairs.

    Rules (evaluated in order):
    1. Owners implicitly have both read and write access.
    2. Wildcard ``"*"`` in any list grants that permission to everyone.
    3. Explicit user ID membership grants the specific permission.
    4. If the namespace is not defined in config, access is denied.
    """

    def __init__(self, config: "EngramConfig") -> None:
        self._config = config
        self._definitions = config.namespaces.definitions  # dict[str, NamespaceDefinition]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, namespace: str) -> "NamespaceDefinition | None":
        """
        Resolve a namespace definition with prefix inheritance.

        Exact match is checked first. If not found, walk up the colon-separated
        hierarchy looking for the longest matching prefix. This lets a single
        definition for ``obsidian:hc`` cover all sub-namespaces like
        ``obsidian:hc:projects:p2p`` without enumerating every folder.
        """
        if namespace in self._definitions:
            return self._definitions[namespace]

        # Walk up: "a:b:c" → try "a:b", then "a"
        parts = namespace.split(":")
        for depth in range(len(parts) - 1, 0, -1):
            parent = ":".join(parts[:depth])
            if parent in self._definitions:
                logger.debug("Namespace %r resolved via parent prefix %r", namespace, parent)
                return self._definitions[parent]

        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_read(self, user_id: str, namespace: str) -> bool:
        """Return ``True`` if *user_id* may read from *namespace*."""
        defn = self._resolve(namespace)
        if defn is None:
            logger.debug(
                "Namespace %r not defined — denying read for user %r", namespace, user_id
            )
            return False
        if _WILDCARD in defn.readers or _WILDCARD in defn.owners:
            return True
        allowed = set(defn.owners) | set(defn.readers)
        result = user_id in allowed
        if not result:
            logger.debug(
                "User %r denied read on namespace %r (allowed: %s)",
                user_id,
                namespace,
                sorted(allowed),
            )
        return result

    def can_write(self, user_id: str, namespace: str) -> bool:
        """Return ``True`` if *user_id* may write to *namespace*."""
        defn = self._resolve(namespace)
        if defn is None:
            logger.debug(
                "Namespace %r not defined — denying write for user %r", namespace, user_id
            )
            return False
        if _WILDCARD in defn.writers or _WILDCARD in defn.owners:
            return True
        allowed = set(defn.owners) | set(defn.writers)
        result = user_id in allowed
        if not result:
            logger.debug(
                "User %r denied write on namespace %r (allowed: %s)",
                user_id,
                namespace,
                sorted(allowed),
            )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def list_readable(self, user_id: str) -> list[str]:
        """Return all namespaces readable by *user_id*."""
        return [ns for ns in self._definitions if self.can_read(user_id, ns)]

    def list_writable(self, user_id: str) -> list[str]:
        """Return all namespaces writable by *user_id*."""
        return [ns for ns in self._definitions if self.can_write(user_id, ns)]

    def is_defined(self, namespace: str) -> bool:
        """Return ``True`` if *namespace* or any of its parents is defined."""
        return self._resolve(namespace) is not None
