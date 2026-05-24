"""
engram.client — EngramClient: the primary public API for engram.

Usage (async context manager):

    async with EngramClient(config) as client:
        entry = await client.add("Alice is a software engineer", namespace="org:acme")
        results = await client.search("software engineer", namespace="org:acme")

Usage (manual lifecycle):

    client = EngramClient(config)
    await client.start()
    ...
    await client.stop()
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

from engram.config import EngramConfig
from engram.extraction.spacy_extractor import get_extractor
from engram.models import (
    AssetReference, DecayPolicy, Entity, Fact, Graph, MemoryEntry, MemoryStatus, MemoryType,
    Provenance, SearchResult, Secret, VaultAuditLog,
)
from engram.storage.arcadedb_client import ArcadeDBClient
from engram.vault.secret_detector import detect as _detect_secrets, redact as _redact_secrets
from engram.vault.vault_client import get_vault_client
from engram.vector.embedder import get_embedder

logger = logging.getLogger(__name__)

# Decay half-lives: time_weighted = 90 days, access_weighted = 30 days
_DECAY_K_TIME   = math.log(2) / 90
_DECAY_K_ACCESS = math.log(2) / 30


def _apply_decay_score(result: SearchResult, now: datetime) -> SearchResult:
    """Multiply search score by a time-decay factor if the memory has a decay policy."""
    mem = result.memory
    policy = mem.decay_policy.value if hasattr(mem.decay_policy, "value") else str(mem.decay_policy or "none")
    if policy in ("none", "DecayPolicy.none", ""):
        return result
    if policy == "time_weighted":
        age_sec = (now - mem.created_at.replace(tzinfo=timezone.utc) if mem.created_at.tzinfo is None else now - mem.created_at).total_seconds()
        factor = math.exp(-_DECAY_K_TIME * max(age_sec / 86400, 0))
    elif policy == "access_weighted":
        ref = mem.last_accessed_at or mem.created_at
        ref = ref.replace(tzinfo=timezone.utc) if ref.tzinfo is None else ref
        idle_sec = (now - ref).total_seconds()
        factor = math.exp(-_DECAY_K_ACCESS * max(idle_sec / 86400, 0))
    else:
        return result
    return result.model_copy(update={"score": result.score * factor})


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Patterns for extracting technical entity names from free-text queries.
# spaCy misses CamelCase service names and snake_case identifiers, so we
# supplement with these patterns to drive decision pinning.
_CAMEL_CASE   = re.compile(r'\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b')   # PaymentService
_ALL_CAPS     = re.compile(r'\b[A-Z]{2,}\b')                          # JWT, API, RCA
_SNAKE_CASE   = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b')  # payment_service
_KEBAB_CASE   = re.compile(r'\b[a-z][a-z0-9]*(?:-[a-z0-9]+){1,}\b')  # payment-service


def _query_entity_names(query: str) -> list[str]:
    """Extract candidate entity names from a query string for decision pinning.

    Returns lowercase names. These are matched against the `affects` list on
    decision/constraint/ADR memories to find governance rules for those entities.
    """
    names: set[str] = set()
    for m in _CAMEL_CASE.finditer(query):
        names.add(m.group(0).lower())
    for m in _ALL_CAPS.finditer(query):
        names.add(m.group(0).lower())
    for m in _SNAKE_CASE.finditer(query):
        names.add(m.group(0))
    for m in _KEBAB_CASE.finditer(query):
        names.add(m.group(0))
    return list(names)


class EngramClient:
    """Primary public API for engram persistent memory.

    All methods are async. Use as an async context manager for automatic
    lifecycle management, or call :meth:`start` / :meth:`stop` manually.
    """

    def __init__(self, config: EngramConfig) -> None:
        self._config = config
        self._embedder = get_embedder(config.embeddings)
        self._arcadedb = ArcadeDBClient(
            host=config.arcadedb.host,
            port=config.arcadedb.port,
            username=config.arcadedb.username,
            password=config.arcadedb.password,
            database=config.arcadedb.database,
            vector_dim=self._embedder.vector_size,
        )
        self._extractor = get_extractor()
        self._vault = get_vault_client(config.vault) if config.vault.enabled else None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise ArcadeDB schema. Must be called before any other method."""
        if self._started:
            logger.debug("EngramClient.start() called but already started — skipping")
            return
        logger.info("Starting EngramClient")
        await self._arcadedb.init()
        self._started = True
        logger.info("EngramClient ready")

    async def stop(self) -> None:
        """Close all backend connections."""
        logger.info("Stopping EngramClient")
        await self._arcadedb.close()
        self._started = False
        logger.info("EngramClient stopped")

    # ------------------------------------------------------------------
    # Async context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "EngramClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_started(self) -> None:
        if not self._started:
            raise RuntimeError(
                "EngramClient must be started before use. "
                "Call `await client.start()` or use it as `async with EngramClient(...) as client:`."
            )

    # ------------------------------------------------------------------
    # Core memory operations
    # ------------------------------------------------------------------

    async def add(
        self,
        content: str,
        namespace: str,
        tags: list[str] | None = None,
        source: str = "agent",
        metadata: dict | None = None,
        memory_type: MemoryType = MemoryType.fact,
        status: MemoryStatus = MemoryStatus.active,
        author: str = "",
        affects: list[str] | None = None,
        rationale: str = "",
        expires_at=None,
        review_by=None,
        provenance: "Provenance | None" = None,
    ) -> MemoryEntry:
        """Persist a new memory entry and extract knowledge-graph edges.

        Steps:
          1. Create a ``MemoryEntry`` with a fresh uuid4 ID.
          2. Embed ``content`` via the configured embedder.
          3. Insert into ArcadeDB (Memory vertex + HNSW vector).
          4. Extract named entities via spaCy (no LLM needed).
          5. Upsert Entity vertices and create MENTIONS + AFFECTS edges.
          6. Return the completed ``MemoryEntry``.
        """
        self._assert_started()

        # Scan for and redact credentials before storage
        if self._config.vault.detect_in_memory:
            detected = _detect_secrets(content)
            if detected:
                names = [d.pattern_name for d in detected]
                logger.warning(
                    "Credential pattern(s) detected in memory write — redacting: %s", names
                )
                content = _redact_secrets(content, detected)

        memory = MemoryEntry(
            content=content,
            namespace=namespace,
            tags=tags or [],
            source=source,
            metadata=metadata or {},
            memory_type=memory_type,
            status=status,
            author=author,
            affects=affects or [],
            rationale=rationale,
            expires_at=expires_at,
            review_by=review_by,
            provenance=provenance or Provenance(),
        )
        logger.debug("add: memory_id=%s namespace=%s type=%s", memory.id, namespace, memory_type)

        embedding = await self._embedder.embed(content)
        await self._arcadedb.insert_memory(memory, embedding)

        # Entity extraction + graph edges (best-effort, never blocks write)
        try:
            extracted = await self._extractor.extract(content)
            for ent in extracted:
                entity_model = Entity(
                    name=ent.name,
                    entity_type=ent.entity_type,
                    namespace=namespace,
                )
                await self._arcadedb.upsert_entity(entity_model)
                await self._arcadedb.create_mentions_edge(memory.id, ent.name, namespace)

            # AFFECTS edges — connect decision/constraint memories to the entities they govern
            for entity_name in (affects or []):
                entity_model = Entity(
                    name=entity_name.lower(),
                    entity_type="DECISION",
                    namespace=namespace,
                )
                await self._arcadedb.upsert_entity(entity_model)
                await self._arcadedb.create_affects_edge(memory.id, entity_name, namespace)

            if extracted:
                logger.debug(
                    "Extracted %d entities for memory %s", len(extracted), memory.id
                )
        except Exception as exc:
            logger.warning("Entity extraction failed (non-fatal): %s", exc)

        logger.info("Memory stored: id=%s namespace=%s type=%s", memory.id, namespace, memory_type.value)

        # Fan-out: push copies to subscribers who requested delivery_namespace
        try:
            await self._fanout_memory(memory, namespace, embedding)
        except Exception as exc:
            logger.debug("fan-out skipped (non-fatal): %s", exc)

        return memory

    async def _fanout_memory(
        self, original: MemoryEntry, source_ns: str, embedding: list[float]
    ) -> None:
        """Copy original memory into each subscriber's delivery_namespace (fire-and-forget)."""
        subscribers = await self._arcadedb.get_fanout_subscribers(source_ns)
        if not subscribers:
            return

        for sub in subscribers:
            delivery_ns = sub["delivery_namespace"]
            filter_types = sub["filter_types"]

            # Apply filter_types check before copying
            if filter_types:
                mtype = original.memory_type.value if hasattr(original.memory_type, "value") else str(original.memory_type)
                tag_match = any(t.lower() in filter_types for t in (original.tags or []))
                if mtype.lower() not in filter_types and not tag_match:
                    continue  # subscriber's filter excludes this memory type

            from engram.models import MemoryEntry as _ME
            copy = _ME(
                content=original.content,
                namespace=delivery_ns,
                tags=list(original.tags),
                source="fanout",
                metadata={
                    **original.metadata,
                    "fanout_source": source_ns,
                    "original_id": str(original.id),
                },
                memory_type=original.memory_type,
                status=original.status,
                author=original.author,
                affects=list(original.affects or []),
                rationale=original.rationale,
                provenance=original.provenance,
            )
            try:
                await self._arcadedb.insert_memory(copy, embedding)
                logger.debug(
                    "fan-out: copied memory %s → %s (subscriber: %s)",
                    original.id, delivery_ns, sub["subscriber_id"],
                )
            except Exception as exc:
                logger.warning("fan-out insert failed for %s → %s: %s", original.id, delivery_ns, exc)

    # ------------------------------------------------------------------
    # Typed write convenience methods (Tier 1)
    # ------------------------------------------------------------------

    async def write_decision(
        self,
        content: str,
        namespace: str,
        rationale: str,
        affects: list[str] | None = None,
        author: str = "",
        tags: list[str] | None = None,
        status: MemoryStatus = MemoryStatus.active,
        review_by=None,
    ) -> MemoryEntry:
        """Record an architectural or technical decision with rationale.

        The decision memory is linked via AFFECTS edges to the entities it
        governs (service names, file patterns, tech choices). When an agent
        later touches those entities, this decision surfaces automatically.
        """
        return await self.add(
            content=content,
            namespace=namespace,
            tags=(tags or []) + ["decision"],
            source="decision",
            memory_type=MemoryType.decision,
            status=status,
            author=author,
            affects=affects,
            rationale=rationale,
            review_by=review_by,
        )

    async def write_constraint(
        self,
        content: str,
        namespace: str,
        rationale: str,
        affects: list[str] | None = None,
        author: str = "",
        tags: list[str] | None = None,
        expires_at=None,
    ) -> MemoryEntry:
        """Record a constraint that AI agents must always respect.

        CONSTRAINT memories bypass score thresholds and are injected at the
        top of every search result for matching namespaces — they are never
        silently filtered out by top_k competition.

        Use for: approved library lists, banned patterns, security rules,
        compliance requirements, architecture invariants.
        """
        return await self.add(
            content=content,
            namespace=namespace,
            tags=(tags or []) + ["constraint"],
            source="constraint",
            memory_type=MemoryType.constraint,
            status=MemoryStatus.active,
            author=author,
            affects=affects,
            rationale=rationale,
            expires_at=expires_at,
        )

    async def write_incident(
        self,
        content: str,
        namespace: str,
        rationale: str = "",
        affects: list[str] | None = None,
        author: str = "",
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Record a production incident for future oncall retrieval.

        Incident memories are searchable by symptom description. When a
        similar incident occurs, past incidents with their resolution steps
        surface automatically.
        """
        return await self.add(
            content=content,
            namespace=namespace,
            tags=(tags or []) + ["incident"],
            source="incident",
            memory_type=MemoryType.incident,
            status=MemoryStatus.active,
            author=author,
            affects=affects,
            rationale=rationale,
        )

    # ------------------------------------------------------------------
    # Constraint retrieval (Tier 1 — AI governance)
    # ------------------------------------------------------------------

    async def get_constraints(self, namespace: str) -> list[MemoryEntry]:
        """Return all active CONSTRAINT memories for *namespace* and its parents.

        These should be injected at the top of every agent context for the
        namespace — they represent non-negotiable rules that must never be
        filtered out by score competition.
        """
        self._assert_started()
        return await self._arcadedb.get_constraints(namespace)

    async def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        include_historical: bool = False,
        mode: str = "hybrid",
    ) -> list[SearchResult]:
        """Search for memories matching *query* within *namespace*.

        Uses ArcadeDB hybrid search — HNSW vector similarity with recency
        weighting (0.7 * semantic + 0.3 * recency).

        Parameters
        ----------
        query:
            Natural-language query string.
        namespace:
            Restrict results to this namespace (prefix-matched).
        top_k:
            Maximum number of results (default 10).
        include_historical:
            When True, also return superseded memories tagged [HISTORICAL].
        mode:
            ``"vector"`` / ``"hybrid"`` → HNSW search.
            ``"graph"`` → entity-based graph traversal.
        """
        self._assert_started()
        logger.debug(
            "search: query=%r namespace=%s top_k=%d historical=%s mode=%s",
            query, namespace, top_k, include_historical, mode,
        )
        if mode == "graph":
            return await self._arcadedb.graph_search(
                query=query, namespace=namespace, top_k=top_k,
                include_superseded=include_historical,
            )
        embedding = await self._embedder.embed(query)
        results = await self._arcadedb.vector_search(
            embedding=embedding,
            namespace=namespace,
            top_k=top_k,
            include_superseded=include_historical,
            query=query,
        )

        # Namespace expansion: if specific namespace returned nothing, widen to parent.
        # e.g. "org:acme:private:customers:client-a" → "org:acme:private:customers" → "org:acme"
        if not results and namespace not in ("all", "*", ""):
            parts = namespace.split(":")
            while len(parts) > 1 and not results:
                parts = parts[:-1]
                parent_ns = ":".join(parts)
                logger.debug("search: no results in %r, expanding to %r", namespace, parent_ns)
                results = await self._arcadedb.vector_search(
                    embedding=embedding,
                    namespace=parent_ns,
                    top_k=top_k,
                    include_superseded=include_historical,
                    query=query,
                )

        # Decision pinning — find decision/constraint/ADR memories that explicitly
        # govern entities mentioned in the query and prepend them above top_k results.
        # These memories are always relevant regardless of semantic score.
        try:
            # Collect candidate entity names: regex extraction + spaCy
            entity_names = _query_entity_names(query)
            spacy_entities = await self._extractor.extract(query)
            entity_names += [e.name for e in spacy_entities]
            # Also include entity names from the affects lists already in results
            for r in results:
                entity_names += list(r.memory.affects or [])

            if entity_names:
                pinned_memories = await self._arcadedb.get_decisions_for_entities(
                    entity_names, namespace
                )
                # Deduplicate: remove from vector results any ID already pinned
                pinned_ids = {m.id for m in pinned_memories}
                deduped_results = [r for r in results if r.memory.id not in pinned_ids]
                # Build pinned SearchResults with score=2.0 (always above natural 0-1 range)
                pinned_results = [
                    SearchResult(
                        memory=m,
                        score=2.0,
                        source="pinned",
                        is_current=True,
                        recency_score=1.0,
                    )
                    for m in pinned_memories
                ]
                if pinned_results:
                    logger.debug(
                        "search: pinned %d decision(s) for entities %s",
                        len(pinned_results),
                        list({e for e in entity_names})[:5],
                    )
                results = pinned_results + deduped_results
        except Exception as exc:
            logger.debug("Decision pinning skipped (non-fatal): %s", exc)

        # Apply decay score modifiers (non-pinned results only)
        now_utc = datetime.now(timezone.utc)
        results = [
            _apply_decay_score(r, now_utc) if getattr(r, "source", "") != "pinned" else r
            for r in results
        ]

        # Update last_accessed_at for access_weighted memories (fire-and-forget)
        access_ids = [
            r.memory.id for r in results
            if getattr(r.memory.decay_policy, "value", str(r.memory.decay_policy)) == "access_weighted"
        ]
        if access_ids:
            try:
                await self._arcadedb.update_last_accessed(access_ids, namespace)
            except Exception as exc:
                logger.debug("update_last_accessed skipped: %s", exc)

        return results

    async def supersede(self, memory_id: str, namespace: str) -> bool:
        """Mark an existing memory as superseded (soft-delete — history preserved).

        Sets ``superseded_at = now(UTC)`` on the Memory vertex. The memory
        remains in ArcadeDB and is returned in searches only when
        ``include_historical=True``.

        Returns ``True`` if the memory was found and superseded, ``False`` if
        not found.
        """
        self._assert_started()
        return await self._arcadedb.supersede_memory(memory_id, namespace)

    async def delete(self, memory_id: str, namespace: str) -> bool:
        """Hard-delete a memory by ID (use supersede() to preserve history).

        Returns ``True`` if deleted, ``False`` if not found.
        """
        self._assert_started()
        return await self._arcadedb.delete_memory(memory_id, namespace)

    async def get_memory(self, memory_id: str, namespace: str) -> MemoryEntry | None:
        """Retrieve a single memory by ID."""
        self._assert_started()
        return await self._arcadedb.get_memory(memory_id, namespace)

    # ------------------------------------------------------------------
    # Knowledge graph operations
    # ------------------------------------------------------------------

    async def get_entity(self, name: str, namespace: str) -> Entity | None:
        """Look up a named entity by (normalized) name."""
        self._assert_started()
        return await self._arcadedb.get_entity(name=name, namespace=namespace)

    async def get_related(
        self, entity_name: str, namespace: str, depth: int = 2
    ) -> Graph:
        """Return a sub-graph of entities near *entity_name*.

        Traverses RELATED_TO edges up to *depth* hops.
        """
        self._assert_started()
        return await self._arcadedb.get_related(
            entity_name=entity_name, namespace=namespace, depth=depth
        )

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        namespace: str,
        source_memory_id: str | None = None,
    ) -> Fact:
        """Record a subject-predicate-object triple in the knowledge graph."""
        self._assert_started()
        fact = Fact(
            subject=subject,
            predicate=predicate,
            object=object,
            namespace=namespace,
            source_memory_id=source_memory_id,
        )
        await self._arcadedb.insert_fact(fact)
        logger.info(
            "Fact stored: id=%s %s %s %s namespace=%s",
            fact.id, subject, predicate, object, namespace,
        )
        return fact

    async def supersede_fact(self, fact_id: str, namespace: str) -> bool:
        """Supersede a fact, recording when it stopped being true."""
        self._assert_started()
        return await self._arcadedb.supersede_fact(fact_id, namespace)

    async def query_graph(
        self,
        sql: str,
        namespace: str,
        params: dict | None = None,
    ) -> list[dict]:
        """Execute a read-only ArcadeDB SQL query.

        ``$namespace`` is automatically injected into params.
        """
        self._assert_started()
        return await self._arcadedb.raw_query(sql, namespace, params)

    # ------------------------------------------------------------------
    # Binary asset operations
    # ------------------------------------------------------------------

    async def add_asset(
        self,
        path: str,
        format: str,
        sha256: str,
        extracted_content: str,
        namespace: str,
        created_by: str = "agent",
        related_memory_ids: list[str] | None = None,
    ) -> AssetReference:
        """Register a binary asset reference (draw.io, PDF, PNG, etc.).

        The binary file is never stored in ArcadeDB — only metadata and
        extracted text. If an asset with the same path already exists and its
        SHA-256 has changed, the old reference is superseded and a new one is
        created.
        """
        self._assert_started()
        existing = await self._arcadedb.get_asset_by_path(path, namespace)
        if existing and existing.sha256 == sha256:
            logger.debug("Asset %r unchanged (same SHA-256), skipping", path)
            return existing

        if existing:
            await self._arcadedb.supersede_asset(existing.id, namespace)
            logger.debug("Asset %r changed — superseded old record", path)

        asset = AssetReference(
            path=path,
            format=format,
            sha256=sha256,
            extracted_content=extracted_content,
            namespace=namespace,
            created_by=created_by,
            related_memory_ids=related_memory_ids or [],
        )

        embedding: list[float] | None = None
        if extracted_content:
            embedding = await self._embedder.embed(extracted_content[:4000])

        await self._arcadedb.insert_asset(asset, embedding)

        # Link asset to related memories
        for mem_id in asset.related_memory_ids:
            try:
                await self._arcadedb.create_documented_in_edge(mem_id, asset.id, namespace)
            except Exception as exc:
                logger.debug("create_documented_in_edge failed (non-fatal): %s", exc)

        logger.info("Asset registered: id=%s path=%r format=%s", asset.id, path, format)
        return asset

    # ------------------------------------------------------------------
    # Review due (Feature 2.4)
    # ------------------------------------------------------------------

    async def get_review_due(self, namespace: str, limit: int = 50) -> list[MemoryEntry]:
        """Return memories past their review_by date — need human confirmation or deprecation."""
        self._assert_started()
        return await self._arcadedb.get_review_due(namespace, limit)

    # ------------------------------------------------------------------
    # Namespace subscriptions (Feature 2.1)
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        subscriber_id: str,
        namespace: str,
        filter_types: list[str] | None = None,
        delivery_namespace: str = "",
    ) -> str:
        """Subscribe subscriber_id to new memories in namespace.

        If delivery_namespace is set, new memories written to namespace are
        automatically copied (fanned out) to delivery_namespace.
        """
        from engram.models import Subscription
        self._assert_started()
        sub = Subscription(
            subscriber_id=subscriber_id,
            namespace=namespace,
            filter_types=filter_types or [],
            delivery_namespace=delivery_namespace,
        )
        return await self._arcadedb.upsert_subscription(sub)

    async def get_feed(
        self, subscriber_id: str, namespace: str, limit: int = 50
    ) -> tuple[list[MemoryEntry], str]:
        """Poll for new memories since last seen. Returns (memories, cursor)."""
        self._assert_started()
        return await self._arcadedb.get_feed(subscriber_id, namespace, limit)

    async def unsubscribe(self, subscriber_id: str, namespace: str) -> bool:
        self._assert_started()
        return await self._arcadedb.delete_subscription(subscriber_id, namespace)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def stats(self, namespace: str = "all") -> dict[str, Any]:
        """Return counts and distribution info for observability."""
        self._assert_started()
        return {
            "memories": await self._arcadedb.count_memories(namespace),
            "edges": await self._arcadedb.count_edges(namespace),
            "namespace_distribution": await self._arcadedb.namespace_distribution(namespace),
        }

    async def visualize(self, namespace: str, limit: int = 100) -> dict:
        """Return graph data suitable for rendering (nodes + edges)."""
        self._assert_started()
        return await self._arcadedb.visualize(namespace=namespace, limit=limit)

    # ------------------------------------------------------------------
    # Vault — encrypted secrets
    # ------------------------------------------------------------------

    def _require_vault(self) -> None:
        if self._vault is None:
            raise RuntimeError("Vault is not enabled. Set vault.enabled: true in engram.yaml")

    async def secret_set(
        self,
        key_name: str,
        value: str,
        namespace: str,
        secret_type: str = "api_key",
        note: str = "",
        created_by: str = "unknown",
        tags: list[str] | None = None,
        _audit_action: str = "set",
    ) -> dict:
        """Encrypt and store a secret. Supersedes any existing secret with the same name."""
        self._assert_started()
        self._require_vault()
        value_enc, dek_enc = await self._vault.encrypt(value)

        # Supersede existing secret with same name if present
        existing = await self._arcadedb.get_secret(key_name, namespace)
        if existing:
            await self._arcadedb.supersede_secret(existing.id, namespace)

        secret = Secret(
            key_name=key_name,
            note=note,
            secret_type=secret_type,
            namespace=namespace,
            value_enc=value_enc,
            dek_enc=dek_enc,
            created_by=created_by,
            tags=tags or [],
        )
        secret_id = await self._arcadedb.insert_secret(secret)

        if self._config.vault.audit_log and _audit_action:
            log = VaultAuditLog(
                secret_name=key_name,
                namespace=namespace,
                action=_audit_action,
                accessed_by=created_by,
                ok=True,
            )
            await self._arcadedb.insert_audit_log(log)

        logger.info("Secret stored: key_name=%s namespace=%s id=%s", key_name, namespace, secret_id)
        return {"id": secret_id, "key_name": key_name, "namespace": namespace}

    async def secret_get(
        self,
        key_name: str,
        namespace: str,
        accessed_by: str = "unknown",
    ) -> str:
        """Decrypt and return the plaintext value of a secret."""
        self._assert_started()
        self._require_vault()
        secret = await self._arcadedb.get_secret(key_name, namespace)

        if self._config.vault.audit_log:
            log = VaultAuditLog(
                secret_name=key_name,
                namespace=namespace,
                action="get",
                accessed_by=accessed_by,
                ok=secret is not None,
                err_msg=None if secret else "not_found",
            )
            await self._arcadedb.insert_audit_log(log)

        if secret is None:
            raise KeyError(f"Secret '{key_name}' not found in namespace '{namespace}'")

        return await self._vault.decrypt(secret.value_enc, secret.dek_enc)

    async def secret_list(
        self,
        namespace: str,
        accessed_by: str = "unknown",
    ) -> list[dict]:
        """Return metadata for all current secrets — never returns plaintext values."""
        self._assert_started()
        self._require_vault()

        if self._config.vault.audit_log:
            log = VaultAuditLog(
                secret_name="*",
                namespace=namespace,
                action="list",
                accessed_by=accessed_by,
                ok=True,
            )
            await self._arcadedb.insert_audit_log(log)

        return await self._arcadedb.list_secrets(namespace)

    async def secret_rotate(
        self,
        key_name: str,
        new_value: str,
        namespace: str,
        accessed_by: str = "unknown",
    ) -> dict:
        """Re-encrypt a secret with a fresh DEK (effectively replaces it)."""
        self._assert_started()
        self._require_vault()
        return await self.secret_set(
            key_name=key_name,
            value=new_value,
            namespace=namespace,
            created_by=accessed_by,
            _audit_action="rotate",
        )

    async def secret_audit(
        self,
        namespace: str,
        limit: int = 100,
    ) -> list[dict]:
        """Return audit log entries for vault access in *namespace*."""
        self._assert_started()
        self._require_vault()
        return await self._arcadedb.get_audit_logs(namespace, limit)
