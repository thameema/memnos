"""
engram.community.detector — Louvain/greedy community detection on the entity graph.

Detects clusters of entities that co-appear frequently in the same memories,
even when no explicit relationships have been modelled.

Usage
-----
    from engram.community.detector import detect_communities, CommunityResult
    results = await detect_communities(arcadedb_client, namespace="org:my-team")
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CommunityResult:
    """Lightweight result object for a detected community."""

    community_id: str
    label: str
    namespace: str
    member_names: list[str] = field(default_factory=list)
    member_count: int = 0


async def detect_communities(
    arcadedb_client,
    namespace: str,
    min_size: int = 2,
    persist: bool = True,
) -> list[CommunityResult]:
    """Detect entity communities using greedy modularity maximisation.

    Parameters
    ----------
    arcadedb_client:
        An initialised ArcadeDBClient instance.
    namespace:
        Namespace to run detection on.  Pass "*" or "all" to run across all namespaces.
    min_size:
        Discard communities with fewer than *min_size* members.
    persist:
        When True, upsert Community vertices and BELONGS_TO edges in ArcadeDB.

    Returns
    -------
    list[CommunityResult]
        One result per detected community that satisfies *min_size*.
        Returns an empty list when networkx is unavailable or no co-occurrences exist.
    """
    # Lazy import — networkx is an optional dependency
    try:
        import networkx as nx
        from networkx.algorithms.community import greedy_modularity_communities
    except ImportError:
        logger.warning(
            "networkx is not installed; community detection skipped. "
            "Install it with: pip install 'engram-core[community]'"
        )
        return []

    # 1. Fetch co-occurrence pairs from the database
    try:
        pairs = await arcadedb_client.get_entity_cooccurrences(namespace)
    except Exception as exc:
        logger.warning("Failed to fetch entity co-occurrences: %s", exc)
        return []

    if not pairs:
        logger.debug("No entity co-occurrences found for namespace %r", namespace)
        return []

    # 2. Build undirected graph
    G: nx.Graph = nx.Graph()
    for a, b in pairs:
        if G.has_edge(a, b):
            G[a][b]["weight"] = G[a][b].get("weight", 1) + 1
        else:
            G.add_edge(a, b, weight=1)

    if G.number_of_nodes() < 2:
        return []

    # 3. Run greedy modularity community detection
    try:
        raw_communities = greedy_modularity_communities(G)
    except Exception as exc:
        logger.warning("Community detection algorithm failed: %s", exc)
        return []

    # 4. Build results
    results: list[CommunityResult] = []

    for community_set in raw_communities:
        members = sorted(community_set)
        if len(members) < min_size:
            continue

        # Stable deterministic ID based on sorted member names
        stable_id = hashlib.sha256(":".join(members).encode()).hexdigest()[:16]

        # Label: top-3 members by degree (most-connected first)
        try:
            members_by_degree = sorted(
                members,
                key=lambda n: G.degree(n),
                reverse=True,
            )
        except Exception:
            members_by_degree = members

        label = " / ".join(members_by_degree[:3])

        result = CommunityResult(
            community_id=stable_id,
            label=label,
            namespace=namespace,
            member_names=members,
            member_count=len(members),
        )
        results.append(result)

    # 5. Persist to ArcadeDB (non-fatal)
    if persist and results:
        from engram.models import Community
        from datetime import datetime, timezone

        for result in results:
            community_model = Community(
                id=result.community_id,
                label=result.label,
                namespace=namespace,
                member_names=result.member_names,
                member_count=result.member_count,
                detected_at=datetime.now(timezone.utc),
            )
            try:
                await arcadedb_client.upsert_community(community_model)
            except Exception as exc:
                logger.warning(
                    "Failed to persist community %s: %s — continuing",
                    result.community_id,
                    exc,
                )
                continue

            for member_name in result.member_names:
                try:
                    await arcadedb_client.create_belongs_to_edge(
                        entity_name=member_name,
                        community_id=result.community_id,
                        namespace=namespace,
                    )
                except Exception as exc:
                    logger.debug(
                        "BELONGS_TO edge skipped for %r → %s: %s",
                        member_name,
                        result.community_id,
                        exc,
                    )

    return results
