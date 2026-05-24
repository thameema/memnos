"""
engram.subscription_bus — In-process event bus for delivery_mode=immediate subscribers.

Keeps an asyncio.Queue per (subscriber_id, namespace) for connected SSE clients.
publish() fans out to every registered queue whose namespace matches (exact or prefix).

Lifetime: queues are registered when a client opens the SSE stream and removed when
the connection closes.  Only clients in the same OS process receive events — for
multi-process deployments use delivery_mode=webhook instead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Type for filter_types — empty list means "accept everything"
_FilterTypes = list[str]


class ImmediateSubscriptionBus:
    """Thread-safe (asyncio-safe) registry of SSE subscriber queues."""

    def __init__(self) -> None:
        # key: (subscriber_id, namespace) → (queue, filter_types)
        self._queues: dict[tuple[str, str], tuple[asyncio.Queue, _FilterTypes]] = {}

    def register(
        self,
        subscriber_id: str,
        namespace: str,
        filter_types: list[str] | None = None,
    ) -> asyncio.Queue:
        """Register a new SSE connection. Returns the queue to drain for events."""
        key = (subscriber_id, namespace)
        q: asyncio.Queue = asyncio.Queue()
        self._queues[key] = (q, filter_types or [])
        logger.debug("immediate-bus: registered %s / %s", subscriber_id, namespace)
        return q

    def unregister(self, subscriber_id: str, namespace: str) -> None:
        """Remove a subscriber's queue (called when SSE connection closes)."""
        self._queues.pop((subscriber_id, namespace), None)
        logger.debug("immediate-bus: unregistered %s / %s", subscriber_id, namespace)

    def publish(self, namespace: str, event: dict[str, Any]) -> int:
        """Push event to all registered queues whose namespace matches.

        Namespace matching: exact match OR the registered namespace is a prefix
        (e.g. subscriber on "org:acme" receives events for "org:acme:eng").

        Returns the number of queues the event was delivered to.
        """
        delivered = 0
        mtype = (event.get("memory") or {}).get("memory_type", "")
        tags = (event.get("memory") or {}).get("tags", [])

        for (sid, sub_ns), (queue, filter_types) in list(self._queues.items()):
            # Namespace match: exact or prefix
            if sub_ns != namespace and not namespace.startswith(sub_ns + ":"):
                continue

            # filter_types check — same logic as webhook dispatch
            if filter_types:
                tags_lower = [t.lower() for t in tags]
                if mtype.lower() not in filter_types and not any(t in filter_types for t in tags_lower):
                    continue

            try:
                queue.put_nowait(event)
                delivered += 1
                logger.debug("immediate-bus: delivered to %s / %s", sid, sub_ns)
            except Exception as exc:
                logger.warning("immediate-bus: queue put failed for %s: %s", sid, exc)

        return delivered

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)


# Module-level singleton — shared within the same OS process
_bus = ImmediateSubscriptionBus()


def register(
    subscriber_id: str,
    namespace: str,
    filter_types: list[str] | None = None,
) -> asyncio.Queue:
    return _bus.register(subscriber_id, namespace, filter_types)


def unregister(subscriber_id: str, namespace: str) -> None:
    _bus.unregister(subscriber_id, namespace)


def publish(namespace: str, event: dict[str, Any]) -> int:
    return _bus.publish(namespace, event)


def subscriber_count() -> int:
    return _bus.subscriber_count
