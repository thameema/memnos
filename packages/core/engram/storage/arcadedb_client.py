"""
engram.storage.arcadedb_client — Async ArcadeDB client for engram v0.2.

ArcadeDB is the single storage backend replacing Neo4j + Qdrant + Graphiti.
Provides: property graph, vector HNSW index, document store — all in one DB.

Connection: HTTP REST API at port 2480 using httpx.AsyncClient.
Auth: Basic auth (username:password).
Query language: SQL (ArcadeDB SQL dialect) via POST /api/v1/command/{db}

Schema (vertex types):
  Memory   — primary knowledge unit with vector embedding
  Entity   — named concept extracted by spaCy
  Fact     — subject-predicate-object assertion
  Asset    — binary file reference (path + hash + extracted content)

Schema (edge types):
  MENTIONS       — Memory → Entity  (spaCy extracted this entity)
  RELATED_TO     — Entity → Entity  (semantic relationship)
  DOCUMENTED_IN  — Memory → Asset   (memory illustrated by asset)
  SUPERSEDED_BY  — Memory → Memory  (explicit supersession lineage)
  AFFECTS        — Memory → Entity  (this decision/constraint governs this entity)
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Temporal query helpers
# ---------------------------------------------------------------------------

_TEMPORAL_TERMS = frozenset({"last", "latest", "recent", "newest", "recently", "current", "new"})

# Match ISO dates (2024-01-23) or verbose dates in content
_DATE_PAT = re.compile(r"\b(202[0-9])[-/](\d{2})[-/](\d{2})\b")


def _is_temporal_query(query: str) -> bool:
    """Return True if the query is asking for the most recent content."""
    words = set(query.lower().split())
    return bool(words & _TEMPORAL_TERMS)


def _extract_doc_date(content: str) -> float:
    """Extract the most recent ISO date from content as a 0-1 recency score.

    Scans up to the first 4000 chars. A score of 1.0 means very recent
    (2027-01-01), 0.0 means 2024-01-01 or earlier. Returns 0.0 if no
    date found.
    """
    matches = _DATE_PAT.findall(content[:4000])
    if not matches:
        return 0.0
    dates = []
    for year, month, day in matches:
        try:
            dates.append(datetime(int(year), int(month), int(day)))
        except ValueError:
            pass
    if not dates:
        return 0.0
    latest = max(dates)
    base = datetime(2024, 1, 1)
    horizon = datetime(2027, 6, 1)
    return max(0.0, min(1.0, (latest - base).days / (horizon - base).days))

import time as _time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engram.models import (
    AssetReference,
    Entity,
    Fact,
    Graph,
    MemoryEntry,
    Provenance,
    Relation,
    SearchResult,
    Secret,
    Subscription,
    VaultAuditLog,
)

logger = logging.getLogger(__name__)

_DB_NAME = "engram"
_VECTOR_DIM = 1536         # OpenAI text-embedding-3-small default; overridden per embedder
_RECENCY_HALF_LIFE = 90    # days — memory from 90 days ago gets 0.5x recency weight
_EMBED_CACHE_TTL = 300     # seconds — refresh embedding cache every 5 minutes


def _cosine_similarity_batch(query: list[float], embeddings: list[list[float]]) -> list[float]:
    """Batch cosine similarity between query and all embeddings.

    Uses numpy when available (fast, <1ms for 10K records).
    Falls back to pure-Python when numpy is absent (slower but always correct).
    """
    if not embeddings:
        return []
    try:
        import numpy as np  # type: ignore
        q = np.array(query, dtype=np.float32)
        E = np.array(embeddings, dtype=np.float32)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0:
            return [0.0] * len(embeddings)
        q_unit = q / q_norm
        row_norms = np.linalg.norm(E, axis=1)
        row_norms = np.where(row_norms == 0, 1.0, row_norms)
        E_unit = E / row_norms[:, None]
        return (E_unit @ q_unit).tolist()
    except ImportError:
        q_sq = sum(x * x for x in query)
        q_norm = q_sq ** 0.5
        if q_norm == 0:
            return [0.0] * len(embeddings)
        results: list[float] = []
        for emb in embeddings:
            dot = sum(a * b for a, b in zip(query, emb))
            emb_norm = sum(x * x for x in emb) ** 0.5
            results.append(dot / (q_norm * emb_norm) if emb_norm > 0 else 0.0)
        return results


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        # ArcadeDB may return naive datetimes; treat them as UTC
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return None


def _recency_score(created_at: datetime | None) -> float:
    """Score in [0,1]: 1.0 = today, 0.5 = 90 days ago, asymptotes to 0."""
    if created_at is None:
        return 0.5
    days = (datetime.now(timezone.utc) - created_at).days
    return 1.0 / (1.0 + days / _RECENCY_HALF_LIFE)


def _combined_score(semantic: float, recency: float) -> float:
    return 0.7 * semantic + 0.3 * recency


# ---------------------------------------------------------------------------
# ArcadeDB HTTP client
# ---------------------------------------------------------------------------

class ArcadeDBClient:
    """Async ArcadeDB REST client for engram."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 2480,
        username: str = "root",
        password: str = "engram",
        database: str = _DB_NAME,
        vector_dim: int = _VECTOR_DIM,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._db = database
        self._vector_dim = vector_dim
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client: httpx.AsyncClient | None = None
        # In-memory embedding cache for Python-layer vector search
        self._embed_cache: list[dict] = []
        self._embed_cache_ts: float = 0.0    # time.time() of last fill
        self._embed_cache_dirty: bool = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Connect, create database if needed, and init schema."""
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        logger.info("Connecting to ArcadeDB at %s", self._base_url)
        await self._ensure_database()
        await self._init_schema()
        logger.info("ArcadeDB ready (db=%s, vector_dim=%d)", self._db, self._vector_dim)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("ArcadeDB client closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_ready(self) -> None:
        if self._client is None:
            raise RuntimeError("ArcadeDBClient.init() must be called before use")

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _command(self, sql: str, params: dict | None = None) -> list[dict]:
        """Execute a SQL command (INSERT/CREATE/UPDATE) and return result records."""
        self._assert_ready()
        body: dict[str, Any] = {"language": "sql", "command": sql}
        if params:
            body["params"] = params
        resp = await self._client.post(  # type: ignore[union-attr]
            f"{self._base_url}/api/v1/command/{self._db}",
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        reraise=True,
    )
    async def _query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Execute a read-only SQL query and return result records."""
        self._assert_ready()
        body: dict[str, Any] = {"language": "sql", "command": sql}
        if params:
            body["params"] = params
        resp = await self._client.post(
            f"{self._base_url}/api/v1/query/{self._db}",
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])

    async def _ensure_database(self) -> None:
        """Create the engram database if it does not exist."""
        try:
            resp = await self._client.get(  # type: ignore[union-attr]
                f"{self._base_url}/api/v1/exists/{self._db}"
            )
            exists = resp.json().get("result", False)
        except Exception:
            exists = False

        if not exists:
            logger.info("Creating ArcadeDB database %r", self._db)
            resp = await self._client.post(  # type: ignore[union-attr]
                f"{self._base_url}/api/v1/server",
                content=json.dumps({"command": f"create database {self._db}"}),
            )
            resp.raise_for_status()

    async def _init_schema(self) -> None:
        """Create vertex types, edge types, and indices if they don't exist."""
        schema_cmds = [
            # Vertex types
            "CREATE VERTEX TYPE Memory IF NOT EXISTS",
            "CREATE VERTEX TYPE Entity IF NOT EXISTS",
            "CREATE VERTEX TYPE Fact IF NOT EXISTS",
            "CREATE VERTEX TYPE Asset IF NOT EXISTS",
            # Edge types
            "CREATE EDGE TYPE MENTIONS IF NOT EXISTS",
            "CREATE EDGE TYPE RELATED_TO IF NOT EXISTS",
            "CREATE EDGE TYPE DOCUMENTED_IN IF NOT EXISTS",
            "CREATE EDGE TYPE SUPERSEDED_BY IF NOT EXISTS",
            "CREATE EDGE TYPE AFFECTS IF NOT EXISTS",
            # Properties — Memory
            "CREATE PROPERTY Memory.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.content IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.superseded_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.tags IF NOT EXISTS LIST",
            "CREATE PROPERTY Memory.source IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.metadata IF NOT EXISTS MAP",
            "CREATE PROPERTY Memory.content_embedding IF NOT EXISTS LIST",
            # Tier 1 — typed memory fields
            "CREATE PROPERTY Memory.memory_type IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.status IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.author IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.affects IF NOT EXISTS LIST",
            "CREATE PROPERTY Memory.rationale IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.expires_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.review_by IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.provenance IF NOT EXISTS MAP",
            # Subscription vertex type (Feature 2.1)
            "CREATE VERTEX TYPE Subscription IF NOT EXISTS",
            "CREATE PROPERTY Subscription.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Subscription.subscriber_id IF NOT EXISTS STRING",
            "CREATE PROPERTY Subscription.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Subscription.filter_types IF NOT EXISTS LIST",
            "CREATE PROPERTY Subscription.last_seen_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Subscription.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Subscription.active IF NOT EXISTS BOOLEAN",
            "CREATE INDEX ON Subscription (subscriber_id, namespace) IF NOT EXISTS",
            # Properties — Entity
            "CREATE PROPERTY Entity.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Entity.name IF NOT EXISTS STRING",
            "CREATE PROPERTY Entity.entity_type IF NOT EXISTS STRING",
            "CREATE PROPERTY Entity.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Entity.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Entity.superseded_at IF NOT EXISTS DATETIME",
            # Properties — Fact
            "CREATE PROPERTY Fact.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Fact.subject IF NOT EXISTS STRING",
            "CREATE PROPERTY Fact.predicate IF NOT EXISTS STRING",
            "CREATE PROPERTY Fact.object IF NOT EXISTS STRING",
            "CREATE PROPERTY Fact.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Fact.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Fact.superseded_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Fact.source_memory_id IF NOT EXISTS STRING",
            # Properties — Asset
            "CREATE PROPERTY Asset.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.path IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.format IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.sha256 IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.extracted_content IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Asset.superseded_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Asset.created_by IF NOT EXISTS STRING",
            "CREATE PROPERTY Asset.content_embedding IF NOT EXISTS LIST",
            # Vault vertex types — value_enc and dek_enc hold ciphertexts, never plaintext
            "CREATE VERTEX TYPE Secret IF NOT EXISTS",
            "CREATE VERTEX TYPE VaultAuditLog IF NOT EXISTS",
            # Properties — Secret
            "CREATE PROPERTY Secret.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.key_name IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.note IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.secret_type IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.value_enc IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.dek_enc IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Secret.superseded_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Secret.created_by IF NOT EXISTS STRING",
            "CREATE PROPERTY Secret.tags IF NOT EXISTS LIST",
            # Properties — VaultAuditLog
            "CREATE PROPERTY VaultAuditLog.id IF NOT EXISTS STRING",
            "CREATE PROPERTY VaultAuditLog.secret_name IF NOT EXISTS STRING",
            "CREATE PROPERTY VaultAuditLog.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY VaultAuditLog.action IF NOT EXISTS STRING",
            "CREATE PROPERTY VaultAuditLog.accessed_by IF NOT EXISTS STRING",
            "CREATE PROPERTY VaultAuditLog.accessed_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY VaultAuditLog.ok IF NOT EXISTS BOOLEAN",
            "CREATE PROPERTY VaultAuditLog.err_msg IF NOT EXISTS STRING",
            # Indices for namespace filtering and id lookups
            # ArcadeDB 26.x syntax: IF NOT EXISTS precedes ON, type is required
            "CREATE INDEX IF NOT EXISTS ON Memory (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Entity (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Entity (name) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Fact (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Asset (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Secret (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Secret (key_name) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON VaultAuditLog (namespace) NOTUNIQUE",
            "CREATE INDEX IF NOT EXISTS ON Memory (id) NOTUNIQUE",
        ]
        for cmd in schema_cmds:
            try:
                await self._command(cmd)
            except Exception as exc:
                logger.debug("Schema init cmd skipped (may already exist): %s | %s", cmd[:60], exc)

        # Vector indices — separate try/except since syntax may vary by ArcadeDB version
        await self._ensure_vector_index("Memory", "content_embedding")
        await self._ensure_vector_index("Asset", "content_embedding")

    async def _ensure_vector_index(self, type_name: str, prop: str) -> None:
        # ArcadeDB 26.5.1 does not support HNSW vector indexes via SQL
        # (CREATE INDEX ON T (p) HNSW ... returns "Index type 'HNSW' is not supported").
        # Vector search is implemented in Python via _cosine_similarity_batch().
        logger.debug(
            "HNSW index on %s.%s skipped — not supported in ArcadeDB 26.x SQL; "
            "using Python-layer cosine similarity instead.",
            type_name,
            prop,
        )

    # ------------------------------------------------------------------
    # Memory CRUD
    # ------------------------------------------------------------------

    async def insert_memory(self, memory: MemoryEntry, embedding: list[float]) -> str:
        """Insert a Memory vertex. Returns the memory id."""
        sql = (
            "INSERT INTO Memory SET "
            "id = :id, content = :content, namespace = :namespace, "
            "created_at = :created_at, superseded_at = :superseded_at, "
            "tags = :tags, source = :source, metadata = :metadata, "
            "memory_type = :memory_type, status = :status, "
            "author = :author, affects = :affects, rationale = :rationale, "
            "expires_at = :expires_at, review_by = :review_by, "
            "provenance = :provenance, "
            "content_embedding = :embedding"
        )
        params = {
            "id": memory.id,
            "content": memory.content,
            "namespace": memory.namespace,
            "created_at": _dt_str(memory.created_at),
            "superseded_at": _dt_str(memory.superseded_at),
            "tags": memory.tags,
            "source": memory.source,
            "metadata": memory.metadata,
            "memory_type": memory.memory_type.value if hasattr(memory.memory_type, 'value') else str(memory.memory_type),
            "status": memory.status.value if hasattr(memory.status, 'value') else str(memory.status),
            "author": memory.author,
            "affects": memory.affects,
            "rationale": memory.rationale,
            "expires_at": _dt_str(memory.expires_at),
            "review_by": _dt_str(memory.review_by),
            "provenance": memory.provenance.model_dump() if memory.provenance else {},
            "embedding": embedding,
        }
        await self._command(sql, params)
        self._embed_cache_dirty = True
        logger.debug("Memory inserted: id=%s namespace=%s", memory.id, memory.namespace)
        return memory.id

    async def get_memory(self, memory_id: str, namespace: str) -> MemoryEntry | None:
        rows = await self._query(
            "SELECT * FROM Memory WHERE id = :id AND namespace = :ns LIMIT 1",
            {"id": memory_id, "ns": namespace},
        )
        if not rows:
            return None
        return _row_to_memory(rows[0])

    async def supersede_memory(self, memory_id: str, namespace: str) -> bool:
        """Set superseded_at = now() on a memory."""
        rows = await self._command(
            "UPDATE Memory SET superseded_at = :now WHERE id = :id AND namespace = :ns",
            {"now": _dt_str(_now()), "id": memory_id, "ns": namespace},
        )
        self._embed_cache_dirty = True
        return bool(rows)

    async def delete_memory(self, memory_id: str, namespace: str) -> bool:
        """Hard-delete a memory and its outgoing edges."""
        rows = await self._command(
            "DELETE VERTEX FROM Memory WHERE id = :id AND namespace = :ns",
            {"id": memory_id, "ns": namespace},
        )
        self._embed_cache_dirty = True
        return bool(rows)

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> str:
        """Insert or update an Entity vertex."""
        updated = await self._command(
            "UPDATE Entity SET entity_type = :etype, created_at = :created_at "
            "WHERE name = :name AND namespace = :ns",
            {
                "etype": entity.entity_type,
                "created_at": _dt_str(entity.created_at),
                "name": entity.name,
                "ns": entity.namespace,
            },
        )
        # ArcadeDB returns [{"count": 0}] when nothing matched — check the count
        matched = int(updated[0].get("count", 0)) if updated else 0
        if matched == 0:
            await self._command(
                "INSERT INTO Entity SET "
                "id = :id, name = :name, entity_type = :etype, "
                "namespace = :ns, created_at = :created_at",
                {
                    "id": entity.id,
                    "name": entity.name,
                    "etype": entity.entity_type,
                    "ns": entity.namespace,
                    "created_at": _dt_str(entity.created_at),
                },
            )
        return entity.id

    async def create_mentions_edge(self, memory_id: str, entity_name: str, namespace: str) -> None:
        """Create a MENTIONS edge from Memory to Entity."""
        try:
            await self._command(
                "CREATE EDGE MENTIONS "
                "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
                "TO (SELECT FROM Entity WHERE name = :ename AND namespace = :ns) "
                "IF NOT EXISTS",
                {"mid": memory_id, "ename": entity_name, "ns": namespace},
            )
        except Exception as exc:
            logger.debug("MENTIONS edge skipped: %s", exc)

    async def create_affects_edge(self, memory_id: str, entity_name: str, namespace: str) -> None:
        """Create an AFFECTS edge from a decision/constraint Memory to an Entity."""
        try:
            await self._command(
                "CREATE EDGE AFFECTS "
                "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
                "TO (SELECT FROM Entity WHERE name = :ename AND namespace = :ns)",
                {"mid": memory_id, "ename": entity_name.lower(), "ns": namespace},
            )
        except Exception as exc:
            logger.debug("AFFECTS edge skipped: %s", exc)

    async def get_constraints(self, namespace: str) -> list["MemoryEntry"]:
        """Return all active CONSTRAINT memories for *namespace* and its parents.

        These are injected at the top of every search result — they bypass the
        score threshold entirely and are always present in agent context.
        """
        parts = namespace.split(":")
        ns_list = [":".join(parts[:i+1]) for i in range(len(parts))]
        placeholders = ", ".join(f":ns{i}" for i in range(len(ns_list)))
        params = {f"ns{i}": ns for i, ns in enumerate(ns_list)}
        rows = await self._query(
            f"SELECT * FROM Memory WHERE memory_type = 'constraint' "
            f"AND status = 'active' "
            f"AND superseded_at IS NULL "
            f"AND expires_at IS NULL "
            f"AND namespace IN [{placeholders}] "
            f"ORDER BY created_at DESC LIMIT 20",
            params,
        )
        return [_row_to_memory(r) for r in rows]

    async def get_decisions_for_entities(
        self, entity_names: list[str], namespace: str
    ) -> list["MemoryEntry"]:
        """Return active decision/constraint/ADR memories whose affects list
        overlaps with any of the given entity names.

        These are pinned above top_k vector results — they surface regardless
        of semantic score because they explicitly govern the entities in the query.
        Checks the full namespace ancestry (org:acme:eng → org:acme → org).
        """
        if not entity_names:
            return []
        normalized = {n.lower().strip() for n in entity_names if n.strip()}
        if not normalized:
            return []

        # Build namespace ancestry list (same pattern as get_constraints)
        parts = namespace.split(":")
        ns_list = [":".join(parts[:i + 1]) for i in range(len(parts))]
        placeholders = ", ".join(f":ns{i}" for i in range(len(ns_list)))
        params: dict = {f"ns{i}": ns for i, ns in enumerate(ns_list)}

        rows = await self._query(
            f"SELECT * FROM Memory "
            f"WHERE memory_type IN ['decision', 'constraint', 'adr'] "
            f"AND status = 'active' "
            f"AND superseded_at IS NULL "
            f"AND namespace IN [{placeholders}] "
            f"LIMIT 500",
            params,
        )

        results = []
        seen: set[str] = set()
        for row in rows:
            affects = row.get("affects") or []
            if any(a.lower().strip() in normalized for a in affects):
                mem = _row_to_memory(row)
                if mem.id not in seen:
                    seen.add(mem.id)
                    results.append(mem)
        return results

    async def get_entity(self, name: str, namespace: str) -> Entity | None:
        rows = await self._query(
            "SELECT * FROM Entity WHERE name = :name AND namespace = :ns LIMIT 1",
            {"name": name.lower(), "ns": namespace},
        )
        if not rows:
            return None
        return _row_to_entity(rows[0])

    async def get_related(self, entity_name: str, namespace: str, depth: int = 2) -> Graph:
        """Return graph of entities connected within depth hops."""
        rows = await self._query(
            "SELECT expand(both('RELATED_TO', 'MENTIONS')) FROM Entity "
            "WHERE name = :name AND namespace = :ns LIMIT 100",
            {"name": entity_name.lower(), "ns": namespace},
        )
        entities = [_row_to_entity(r) for r in rows if r.get("@type") == "Entity"]
        return Graph(entities=entities, relations=[])

    # ------------------------------------------------------------------
    # Fact CRUD
    # ------------------------------------------------------------------

    async def insert_fact(self, fact: Fact) -> str:
        await self._command(
            "INSERT INTO Fact SET "
            "id = :id, subject = :subj, predicate = :pred, object = :obj, "
            "namespace = :ns, created_at = :created_at, superseded_at = :superseded_at, "
            "source_memory_id = :source_mid",
            {
                "id": fact.id,
                "subj": fact.subject,
                "pred": fact.predicate,
                "obj": fact.object,
                "ns": fact.namespace,
                "created_at": _dt_str(fact.created_at),
                "superseded_at": _dt_str(fact.superseded_at),
                "source_mid": fact.source_memory_id,
            },
        )
        return fact.id

    async def supersede_fact(self, fact_id: str, namespace: str) -> bool:
        rows = await self._command(
            "UPDATE Fact SET superseded_at = :now WHERE id = :id AND namespace = :ns",
            {"now": _dt_str(_now()), "id": fact_id, "ns": namespace},
        )
        return bool(rows)

    # ------------------------------------------------------------------
    # Asset CRUD
    # ------------------------------------------------------------------

    async def insert_asset(self, asset: AssetReference, embedding: list[float] | None = None) -> str:
        embed_val = embedding if embedding is not None else []
        await self._command(
            "INSERT INTO Asset SET "
            "id = :id, path = :path, format = :fmt, sha256 = :sha, "
            "extracted_content = :content, namespace = :ns, "
            "created_at = :created_at, superseded_at = :superseded_at, "
            "created_by = :created_by, content_embedding = :embedding",
            {
                "id": asset.id,
                "path": asset.path,
                "fmt": asset.format,
                "sha": asset.sha256,
                "content": asset.extracted_content,
                "ns": asset.namespace,
                "created_at": _dt_str(asset.created_at),
                "superseded_at": _dt_str(asset.superseded_at),
                "created_by": asset.created_by,
                "embedding": embed_val,
            },
        )
        return asset.id

    async def get_asset_by_path(self, path: str, namespace: str) -> AssetReference | None:
        rows = await self._query(
            "SELECT * FROM Asset WHERE path = :path AND namespace = :ns "
            "AND superseded_at IS NULL LIMIT 1",
            {"path": path, "ns": namespace},
        )
        if not rows:
            return None
        return _row_to_asset(rows[0])

    async def supersede_asset(self, asset_id: str, namespace: str) -> bool:
        rows = await self._command(
            "UPDATE Asset SET superseded_at = :now WHERE id = :id AND namespace = :ns",
            {"now": _dt_str(_now()), "id": asset_id, "ns": namespace},
        )
        return bool(rows)

    async def create_documented_in_edge(self, memory_id: str, asset_id: str, namespace: str) -> None:
        try:
            await self._command(
                "CREATE EDGE DOCUMENTED_IN "
                "FROM (SELECT FROM Memory WHERE id = :mid AND namespace = :ns) "
                "TO (SELECT FROM Asset WHERE id = :aid AND namespace = :ns)",
                {"mid": memory_id, "aid": asset_id, "ns": namespace},
            )
        except Exception as exc:
            logger.debug("DOCUMENTED_IN edge skipped: %s", exc)

    # ------------------------------------------------------------------
    # Vector + hybrid search
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        embedding: list[float],
        namespace: str,
        top_k: int = 10,
        include_superseded: bool = False,
        query: str = "",
    ) -> list[SearchResult]:
        """Search Memory by vector similarity with recency weighting.

        ArcadeDB 26.x does not support HNSW vector indexes via SQL.
        This method fetches candidate memories from ArcadeDB and computes
        cosine similarity in Python using numpy (fast: <5ms for 10K records)
        or pure-Python math as fallback when numpy is absent.

        An in-memory embedding cache (TTL = _EMBED_CACHE_TTL) is maintained
        so that repeated searches within the TTL window skip the ArcadeDB round-trip
        and run in <1ms.
        """
        ns_filter = "all" if namespace in ("all", "", "*") else namespace

        try:
            rows = await self._get_candidate_rows(namespace, include_superseded)
        except Exception as exc:
            logger.warning("Failed to fetch candidates for vector search: %s", exc)
            rows = []

        if not rows:
            logger.debug("No embedding candidates — falling back to keyword scan")
            return await self._keyword_scored(namespace, top_k, include_superseded, query)

        # Filter to same dimension as query
        q_dim = len(embedding)
        valid_rows = [r for r in rows if isinstance(r.get("content_embedding"), list)
                      and len(r["content_embedding"]) == q_dim]

        if not valid_rows:
            logger.warning(
                "All stored embeddings have dimension mismatch (expected %d) — "
                "run tools/reembed.py to re-embed with the current provider",
                q_dim,
            )
            return await self._keyword_scored(namespace, top_k, include_superseded, query)

        embs = [r["content_embedding"] for r in valid_rows]
        sims = _cosine_similarity_batch(embedding, embs)

        results: list[SearchResult] = []
        for row, sim in zip(valid_rows, sims):
            memory = _row_to_memory(row)
            recency = _recency_score(memory.created_at)
            combined = _combined_score(sim, recency)
            results.append(SearchResult(
                memory=memory,
                score=combined,
                source="vector",
                is_current=memory.is_current,
                recency_score=recency,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _get_candidate_rows(
        self,
        namespace: str,
        include_superseded: bool = False,
    ) -> list[dict]:
        """Return Memory rows with embeddings, using an in-memory TTL cache.

        The cache is invalidated on any insert/update/delete to Memory and
        expires automatically after _EMBED_CACHE_TTL seconds.
        """
        now = _time.monotonic()
        if not self._embed_cache_dirty and (now - self._embed_cache_ts) < _EMBED_CACHE_TTL:
            # Return from cache, filtered for namespace and supersession
            ns_filter = "all" if namespace in ("all", "", "*") else namespace
            if ns_filter == "all":
                return list(self._embed_cache)
            return [
                r for r in self._embed_cache
                if r.get("namespace", "") == ns_filter
                or (r.get("namespace") or "").startswith(f"{ns_filter}:")
            ]

        # Refresh cache — fetch ALL active records with embeddings
        rows = await self._query(
            "SELECT id, content, namespace, created_at, superseded_at, tags, "
            "source, metadata, memory_type, status, author, affects, rationale, "
            "expires_at, review_by, provenance, content_embedding "
            "FROM Memory WHERE content_embedding IS NOT NULL "
            "AND superseded_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > :now_dt) "
            "LIMIT 100000",
            {"now_dt": _dt_str(_now())},
        )
        self._embed_cache = rows
        self._embed_cache_ts = now
        self._embed_cache_dirty = False
        logger.debug("Embedding cache refreshed: %d records loaded", len(rows))

        ns_filter = "all" if namespace in ("all", "", "*") else namespace
        if include_superseded:
            # Don't use cache for superseded queries — do a fresh fetch
            extra = await self._query(
                "SELECT id, content, namespace, created_at, superseded_at, tags, "
                "source, metadata, memory_type, status, author, affects, rationale, "
                "expires_at, review_by, provenance, content_embedding "
                "FROM Memory WHERE content_embedding IS NOT NULL "
                "AND superseded_at IS NOT NULL "
                "LIMIT 10000",
                {},
            )
            all_rows = rows + extra
        else:
            all_rows = rows

        if ns_filter == "all":
            return all_rows
        return [
            r for r in all_rows
            if r.get("namespace", "") == ns_filter
            or (r.get("namespace") or "").startswith(f"{ns_filter}:")
        ]

    async def _keyword_scored(
        self,
        namespace: str,
        top_k: int,
        include_superseded: bool,
        query: str,
    ) -> list[SearchResult]:
        """Score fallback rows from _fallback_scan and return as SearchResults."""
        rows = await self._fallback_scan(namespace, top_k, include_superseded, query=query)
        temporal = _is_temporal_query(query)
        keywords = [w.lower() for w in query.split() if len(w) >= 3] if query else []
        _temporal_kw = frozenset({"last", "latest", "recent", "recently", "newest", "current", "new"})
        topic_kws = [kw for kw in keywords if kw not in _temporal_kw] if keywords else []

        scored: list[SearchResult] = []
        for row in rows:
            memory = _row_to_memory(row)
            text = (memory.content or "").lower()
            doc_recency = _extract_doc_date(memory.content or "")
            import_recency = _recency_score(memory.created_at)
            recency = doc_recency if doc_recency > 0 else import_recency
            if keywords:
                hits = sum(1 for kw in keywords if kw in text)
                topic_hits = sum(1 for kw in topic_kws if kw in text) if topic_kws else hits
                kw_score = min(0.95, 0.5 + hits * 0.1)
            else:
                hits = topic_hits = 0
                kw_score = 0.5
            if temporal and topic_kws:
                combined = 0.0 if topic_hits == 0 else (
                    0.8 * recency + 0.2 * min(1.0, topic_hits / max(len(topic_kws), 1))
                )
            else:
                combined = _combined_score(kw_score, recency)
            scored.append(SearchResult(
                memory=memory,
                score=combined,
                source="keyword",
                is_current=memory.is_current,
                recency_score=recency,
            ))
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    async def graph_search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        include_superseded: bool = False,
    ) -> list[SearchResult]:
        """Entity-traversal search: find memories that MENTION entities in query.

        Extracts entities from the query text using spaCy, then traverses
        MENTIONS edges to find all memories that reference those entities.
        Falls back to a full-text content match when no entities are found.
        """
        from engram.extraction.spacy_extractor import get_extractor

        ns_filter = "all" if namespace in ("all", "", "*") else namespace
        superseded_clause = "" if include_superseded else "AND m.superseded_at IS NULL"

        try:
            extracted = get_extractor().extract_sync(query)
            entity_names = [e.name for e in extracted] if extracted else []
        except Exception:
            entity_names = []

        rows: list[dict] = []

        if entity_names:
            # Traverse MENTIONS edges from matching Entity vertices
            ns_clause = "" if ns_filter == "all" else "AND m.namespace = :ns OR m.namespace LIKE :ns_prefix"
            sql = (
                f"SELECT EXPAND(IN('MENTIONS')) AS m "
                f"FROM Entity WHERE name IN :names AND (namespace = :ns OR namespace LIKE :ns_prefix) "
                f"LET m = IN('MENTIONS') "
                f"UNWIND m "
                f"WHERE 1=1 {superseded_clause} "
                f"LIMIT :topK"
            )
            try:
                rows = await self._query(
                    sql,
                    {
                        "names": entity_names,
                        "ns": ns_filter,
                        "ns_prefix": f"{ns_filter}:%",
                        "topK": top_k,
                    },
                )
            except Exception as exc:
                logger.debug("Entity graph search failed, falling back to text: %s", exc)

        if not rows:
            # Full-text content search fallback
            superseded_sql = "" if include_superseded else "AND superseded_at IS NULL"
            ns_sql = "" if ns_filter == "all" else "AND (namespace = :ns OR namespace LIKE :ns_prefix)"
            sql = (
                f"SELECT * FROM Memory "
                f"WHERE content LIKE :pattern {ns_sql} {superseded_sql} "
                f"LIMIT :topK"
            )
            # Build keyword pattern from first few words of query
            first_word = query.strip().split()[0] if query.strip() else query
            params: dict = {"pattern": f"%{first_word}%", "topK": top_k}
            if ns_filter != "all":
                params["ns"] = ns_filter
                params["ns_prefix"] = f"{ns_filter}:%"
            try:
                rows = await self._query(sql, params)
            except Exception as exc:
                logger.debug("Text fallback search failed: %s", exc)

        results: list[SearchResult] = []
        for row in rows:
            memory = _row_to_memory(row)
            recency = _recency_score(memory.created_at)
            results.append(SearchResult(
                memory=memory,
                score=recency,
                source="graph",
                is_current=memory.is_current,
                recency_score=recency,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _fallback_scan(
        self, namespace: str, top_k: int, include_superseded: bool,
        query: str = "",
    ) -> list[dict]:
        """Keyword-based fallback when vector search is unavailable.

        Searches for significant terms from the query using LIKE, fetches a
        broad candidate set, then re-ranks by keyword hit count so that the
        most relevant memories surface first.
        """
        ns_filter = namespace if namespace not in ("all", "", "*") else None

        # Detect if the query asks for most-recent content
        temporal = _is_temporal_query(query)

        # Extract meaningful keywords (skip stop words, require len >= 3)
        # Keep "last", "latest", etc. OUT of stop words — they carry temporal intent
        _stop = {"the", "was", "what", "about", "did", "are", "for",
                 "and", "that", "with", "this", "from", "have", "has", "had"}
        keywords = [
            w.lower() for w in query.split()
            if len(w) >= 3 and w.lower() not in _stop
        ] if query else []

        # Fetch a broad candidate set — 5× top_k so we can re-rank
        candidate_limit = max(top_k * 5, 50)
        where_parts = []
        params: dict = {}
        if ns_filter:
            where_parts.append("(namespace = :ns OR namespace LIKE :ns_prefix)")
            params["ns"] = ns_filter
            params["ns_prefix"] = f"{ns_filter}:%"
        if not include_superseded:
            where_parts.append("superseded_at IS NULL")
        where_parts.append("(expires_at IS NULL OR expires_at > :now_dt)")
        params["now_dt"] = _dt_str(_now())
        if keywords:
            # Match any memory containing at least one keyword
            kw_clauses = " OR ".join(
                f"content.toLowerCase() LIKE :kw{i}" for i in range(len(keywords))
            )
            where_parts.append(f"({kw_clauses})")
            for i, kw in enumerate(keywords):
                params[f"kw{i}"] = f"%{kw}%"
        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        sql = f"SELECT * FROM Memory {where} ORDER BY created_at DESC LIMIT :topK"
        params["topK"] = candidate_limit
        rows = await self._query(sql, params)

        if not rows:
            return rows[:top_k]

        # Re-rank: keyword hit count + document date extracted from content
        def _hits(row: dict) -> int:
            text = (row.get("content") or "").lower()
            return sum(1 for kw in keywords if kw in text)

        max_hits = max((_hits(r) for r in rows), default=1) or 1

        def _rank_key(row: dict) -> float:
            h = _hits(row) / max_hits          # 0–1 normalised hits
            d = _extract_doc_date(row.get("content", ""))  # 0–1 recency from content
            if temporal:
                # Temporal queries: document date drives ranking (70 %), hits secondary
                return 0.7 * d + 0.3 * h
            else:
                # Regular queries: keyword relevance drives (70 %), date secondary
                return 0.3 * d + 0.7 * h

        rows.sort(key=_rank_key, reverse=True)
        # Return more than top_k so the caller can re-score and pick the best;
        # the final slice to top_k happens in vector_search after rescoring.
        return rows[:max(top_k * 3, top_k)]

    # ------------------------------------------------------------------
    # Graph stats (for dashboard)
    # ------------------------------------------------------------------

    async def count_memories(self, namespace: str) -> int:
        ns_filter = namespace if namespace not in ("all", "", "*") else None
        if ns_filter:
            rows = await self._query(
                "SELECT count(*) AS cnt FROM Memory "
                "WHERE namespace = :ns OR namespace LIKE :prefix",
                {"ns": ns_filter, "prefix": f"{ns_filter}:%"},
            )
        else:
            rows = await self._query("SELECT count(*) AS cnt FROM Memory")
        return int(rows[0].get("cnt", 0)) if rows else 0

    async def count_edges(self, namespace: str) -> int:
        ns_filter = namespace if namespace not in ("all", "", "*") else None
        try:
            if ns_filter:
                rows = await self._query(
                    "SELECT count(*) AS cnt FROM MENTIONS "
                    "WHERE @out.namespace = :ns OR @out.namespace LIKE :prefix",
                    {"ns": ns_filter, "prefix": f"{ns_filter}:%"},
                )
            else:
                rows = await self._query("SELECT count(*) AS cnt FROM MENTIONS")
            return int(rows[0].get("cnt", 0)) if rows else 0
        except Exception:
            return 0

    async def namespace_distribution(self, base_ns: str, limit: int = 30) -> dict[str, int]:
        """Return {namespace: count} map."""
        ns_filter = base_ns if base_ns not in ("all", "", "*") else None
        if ns_filter:
            rows = await self._query(
                "SELECT namespace, count(*) AS cnt FROM Memory "
                "WHERE namespace = :ns OR namespace LIKE :prefix "
                "GROUP BY namespace ORDER BY cnt DESC LIMIT :limit",
                {"ns": ns_filter, "prefix": f"{ns_filter}:%", "limit": limit},
            )
        else:
            rows = await self._query(
                "SELECT namespace, count(*) AS cnt FROM Memory "
                "GROUP BY namespace ORDER BY cnt DESC LIMIT :limit",
                {"limit": limit},
            )
        return {str(r.get("namespace", "")): int(r.get("cnt", 0)) for r in rows if r.get("namespace")}

    async def visualize(self, namespace: str, limit: int = 100) -> dict:
        """Return nodes + edges payload for dashboard graph visualization."""
        ns_filter = namespace if namespace not in ("all", "", "*") else None

        if ns_filter:
            mem_rows = await self._query(
                "SELECT id, content, namespace, created_at, superseded_at, tags "
                "FROM Memory WHERE (namespace = :ns OR namespace LIKE :prefix) "
                "AND superseded_at IS NULL LIMIT :lim",
                {"ns": ns_filter, "prefix": f"{ns_filter}:%", "lim": limit},
            )
        else:
            mem_rows = await self._query(
                "SELECT id, content, namespace, created_at, superseded_at, tags "
                "FROM Memory WHERE superseded_at IS NULL LIMIT :lim",
                {"lim": limit},
            )

        # Edge query — ArcadeDB uses @out.id / @in.id for edge endpoint properties
        edge_rows: list[dict] = []
        try:
            if ns_filter:
                edge_rows = await self._query(
                    "SELECT @out.id AS src_id, @in.id AS tgt_id, @type AS rel_type "
                    "FROM MENTIONS WHERE @out.namespace = :ns OR @out.namespace LIKE :prefix "
                    "LIMIT :lim",
                    {"ns": ns_filter, "prefix": f"{ns_filter}:%", "lim": limit * 3},
                )
            else:
                edge_rows = await self._query(
                    "SELECT @out.id AS src_id, @in.id AS tgt_id, @type AS rel_type "
                    "FROM MENTIONS LIMIT :lim",
                    {"lim": limit * 3},
                )
        except Exception:
            pass

        mem_node_ids: set[str] = set()
        nodes = []
        for row in mem_rows:
            content = str(row.get("content", ""))
            label = content[:80] + ("…" if len(content) > 80 else "")
            node_id = row.get("id", "")
            mem_node_ids.add(node_id)
            nodes.append({
                "id": node_id,
                "label": label,
                "namespace": row.get("namespace", ""),
                "type": "Memory",
                "created_at": str(row.get("created_at", "")),
                "is_current": row.get("superseded_at") is None,
            })

        # Build edges and collect entity node IDs that are actually referenced
        edges = []
        entity_ids_needed: set[str] = set()
        for r in edge_rows:
            src, tgt = r.get("src_id", ""), r.get("tgt_id", "")
            if src and tgt and src in mem_node_ids:
                edges.append({
                    "source": src,
                    "target": tgt,
                    "type": r.get("rel_type", "MENTIONS"),
                    "weight": 1.0,
                })
                entity_ids_needed.add(tgt)

        # Fetch ALL Entity nodes referenced by the edges so D3 can render them
        fetched_entity_ids: set[str] = set()
        if entity_ids_needed:
            try:
                id_list = list(entity_ids_needed)
                placeholders = ", ".join(f":eid{i}" for i in range(len(id_list)))
                params = {f"eid{i}": v for i, v in enumerate(id_list)}
                ent_rows = await self._query(
                    f"SELECT id, name, namespace FROM Entity WHERE id IN [{placeholders}]",
                    params,
                )
                for row in ent_rows:
                    eid = row.get("id", "")
                    fetched_entity_ids.add(eid)
                    nodes.append({
                        "id": eid,
                        "label": row.get("name", ""),
                        "namespace": row.get("namespace", ""),
                        "type": "Entity",
                        "created_at": "",
                        "is_current": True,
                    })
            except Exception:
                pass

        # Only include edges where both endpoints are in the node set
        all_node_ids = mem_node_ids | fetched_entity_ids
        edges = [e for e in edges if e["source"] in all_node_ids and e["target"] in all_node_ids]

        return {"nodes": nodes, "edges": edges, "truncated": len(mem_rows) >= limit}

    # ------------------------------------------------------------------
    # Vault — Secret CRUD
    # ------------------------------------------------------------------

    async def insert_secret(self, secret: Secret) -> str:
        """Store an encrypted secret. Never logs the value fields."""
        await self._command(
            "INSERT INTO Secret SET "
            "id = :id, key_name = :key_name, note = :note, "
            "secret_type = :stype, namespace = :ns, "
            "value_enc = :value_enc, dek_enc = :dek_enc, "
            "created_at = :created_at, superseded_at = :superseded_at, "
            "created_by = :created_by, tags = :tags",
            {
                "id": secret.id,
                "key_name": secret.key_name,
                "note": secret.note,
                "stype": secret.secret_type,
                "ns": secret.namespace,
                "value_enc": secret.value_enc,
                "dek_enc": secret.dek_enc,
                "created_at": _dt_str(secret.created_at),
                "superseded_at": _dt_str(secret.superseded_at),
                "created_by": secret.created_by,
                "tags": secret.tags,
            },
        )
        logger.debug("Secret inserted: key_name=%s namespace=%s", secret.key_name, secret.namespace)
        return secret.id

    async def get_secret(self, key_name: str, namespace: str) -> Secret | None:
        """Retrieve the current (non-superseded) Secret by name."""
        rows = await self._query(
            "SELECT * FROM Secret "
            "WHERE key_name = :name AND namespace = :ns AND superseded_at IS NULL "
            "LIMIT 1",
            {"name": key_name, "ns": namespace},
        )
        return _row_to_secret(rows[0]) if rows else None

    async def list_secrets(self, namespace: str) -> list[dict]:
        """Return metadata for all current secrets — NO ciphertext fields."""
        ns_filter = namespace if namespace not in ("all", "", "*") else None
        if ns_filter:
            rows = await self._query(
                "SELECT id, key_name, note, secret_type, namespace, "
                "created_at, superseded_at, created_by, tags "
                "FROM Secret "
                "WHERE (namespace = :ns OR namespace LIKE :prefix) "
                "AND superseded_at IS NULL "
                "ORDER BY key_name ASC",
                {"ns": ns_filter, "prefix": f"{ns_filter}:%"},
            )
        else:
            rows = await self._query(
                "SELECT id, key_name, note, secret_type, namespace, "
                "created_at, superseded_at, created_by, tags "
                "FROM Secret WHERE superseded_at IS NULL "
                "ORDER BY key_name ASC"
            )
        # Deliberately strip value_enc / dek_enc even if present
        return [
            {
                "id": r.get("id", ""),
                "key_name": r.get("key_name", ""),
                "note": r.get("note", ""),
                "secret_type": r.get("secret_type", ""),
                "namespace": r.get("namespace", ""),
                "created_at": str(r.get("created_at", "")),
                "created_by": r.get("created_by", ""),
                "tags": r.get("tags") or [],
                "is_current": True,
            }
            for r in rows
        ]

    async def supersede_secret(self, secret_id: str, namespace: str) -> bool:
        rows = await self._command(
            "UPDATE Secret SET superseded_at = :now WHERE id = :id AND namespace = :ns",
            {"now": _dt_str(_now()), "id": secret_id, "ns": namespace},
        )
        return bool(rows)

    async def delete_secret(self, secret_id: str, namespace: str) -> bool:
        """Hard-delete a secret vertex (prefer supersede for audit trail)."""
        rows = await self._command(
            "DELETE VERTEX FROM Secret WHERE id = :id AND namespace = :ns",
            {"id": secret_id, "ns": namespace},
        )
        return bool(rows)

    # ------------------------------------------------------------------
    # Vault — Audit Log
    # ------------------------------------------------------------------

    async def insert_audit_log(self, log: VaultAuditLog) -> str:
        await self._command(
            "INSERT INTO VaultAuditLog SET "
            "id = :log_id, secret_name = :secret_name, namespace = :namespace, "
            "action = :action, accessed_by = :accessed_by, accessed_at = :accessed_at, "
            "ok = :ok, err_msg = :err_msg",
            {
                "log_id": log.id,
                "secret_name": log.secret_name,
                "namespace": log.namespace,
                "action": log.action,
                "accessed_by": log.accessed_by,
                "accessed_at": _dt_str(log.accessed_at),
                "ok": log.ok,
                "err_msg": log.err_msg,
            },
        )
        return log.id

    async def get_audit_logs(self, namespace: str, limit: int = 100) -> list[dict]:
        ns_filter = namespace if namespace not in ("all", "", "*") else None
        if ns_filter:
            rows = await self._query(
                "SELECT * FROM VaultAuditLog "
                "WHERE namespace = :ns OR namespace LIKE :prefix "
                "ORDER BY accessed_at DESC LIMIT :lim",
                {"ns": ns_filter, "prefix": f"{ns_filter}:%", "lim": limit},
            )
        else:
            rows = await self._query(
                "SELECT * FROM VaultAuditLog ORDER BY accessed_at DESC LIMIT :lim",
                {"lim": limit},
            )
        return rows

    # ------------------------------------------------------------------
    # Review due (Feature 2.4)
    # ------------------------------------------------------------------

    async def get_review_due(
        self, namespace: str, limit: int = 50
    ) -> list["MemoryEntry"]:
        """Return memories whose review_by date has passed and are still active."""
        parts = namespace.split(":")
        ns_list = [":".join(parts[:i+1]) for i in range(len(parts))]
        placeholders = ", ".join(f":ns{i}" for i in range(len(ns_list)))
        params = {f"ns{i}": ns for i, ns in enumerate(ns_list)}
        params["now_dt"] = _dt_str(_now())
        params["limit"] = limit
        rows = await self._query(
            f"SELECT * FROM Memory "
            f"WHERE review_by IS NOT NULL "
            f"AND review_by < :now_dt "
            f"AND superseded_at IS NULL "
            f"AND status = 'active' "
            f"AND namespace IN [{placeholders}] "
            f"ORDER BY review_by ASC LIMIT :limit",
            params,
        )
        return [_row_to_memory(r) for r in rows]

    # ------------------------------------------------------------------
    # Namespace subscriptions (Feature 2.1)
    # ------------------------------------------------------------------

    async def upsert_subscription(self, sub: "Subscription") -> str:
        """Create or activate a subscription. One per (subscriber_id, namespace)."""
        updated = await self._command(
            "UPDATE Subscription SET last_seen_at = :last_seen, active = true "
            "WHERE subscriber_id = :sid AND namespace = :ns",
            {"last_seen": _dt_str(sub.last_seen_at), "sid": sub.subscriber_id, "ns": sub.namespace},
        )
        if not (updated and int(updated[0].get("count", 0)) > 0):
            await self._command(
                "INSERT INTO Subscription SET "
                "id = :id, subscriber_id = :sid, namespace = :ns, "
                "filter_types = :types, last_seen_at = :last_seen, "
                "created_at = :created_at, active = true",
                {
                    "id": sub.id, "sid": sub.subscriber_id, "ns": sub.namespace,
                    "types": sub.filter_types,
                    "last_seen": _dt_str(sub.last_seen_at),
                    "created_at": _dt_str(sub.created_at),
                },
            )
        return sub.id

    async def get_feed(
        self, subscriber_id: str, namespace: str, limit: int = 50
    ) -> tuple[list["MemoryEntry"], str]:
        """Return new memories since last_seen for subscriber. Updates high-water mark.

        Returns (memories, new_cursor_iso) where new_cursor_iso is the ISO timestamp
        to use as the next poll cursor.
        """
        # Get current high-water mark
        sub_rows = await self._query(
            "SELECT last_seen_at FROM Subscription "
            "WHERE subscriber_id = :sid AND namespace = :ns AND active = true LIMIT 1",
            {"sid": subscriber_id, "ns": namespace},
        )
        if not sub_rows:
            return [], _dt_str(_now())

        last_seen = _parse_dt(sub_rows[0].get("last_seen_at")) or _now()
        now = _now()

        parts = namespace.split(":")
        ns_list = [":".join(parts[:i+1]) for i in range(len(parts))]
        placeholders = ", ".join(f":ns{i}" for i in range(len(ns_list)))
        params = {f"ns{i}": ns for i, ns in enumerate(ns_list)}
        params.update({"last_seen": _dt_str(last_seen), "limit": limit})
        rows = await self._query(
            f"SELECT * FROM Memory "
            f"WHERE created_at > :last_seen "
            f"AND superseded_at IS NULL "
            f"AND namespace IN [{placeholders}] "
            f"ORDER BY created_at ASC LIMIT :limit",
            params,
        )
        memories = [_row_to_memory(r) for r in rows]

        # Advance high-water mark
        if memories:
            new_cursor = max(m.created_at for m in memories)
            await self._command(
                "UPDATE Subscription SET last_seen_at = :cursor "
                "WHERE subscriber_id = :sid AND namespace = :ns",
                {"cursor": _dt_str(new_cursor), "sid": subscriber_id, "ns": namespace},
            )
        else:
            new_cursor = now

        return memories, _dt_str(new_cursor)

    async def delete_subscription(self, subscriber_id: str, namespace: str) -> bool:
        rows = await self._command(
            "UPDATE Subscription SET active = false "
            "WHERE subscriber_id = :sid AND namespace = :ns",
            {"sid": subscriber_id, "ns": namespace},
        )
        return bool(rows and int(rows[0].get("count", 0)) > 0)

    # ------------------------------------------------------------------
    # Raw query (for MCP graph_query tool)
    # ------------------------------------------------------------------

    async def raw_query(self, sql: str, namespace: str, params: dict | None = None) -> list[dict]:
        """Execute a read-only SQL query. Namespace is injected as :namespace param."""
        full_params = {"namespace": namespace, **(params or {})}
        return await self._query(sql, full_params)


# ---------------------------------------------------------------------------
# Row → model converters
# ---------------------------------------------------------------------------

def _row_to_memory(row: dict) -> MemoryEntry:
    from engram.models import MemoryType, MemoryStatus
    raw_type = row.get("memory_type", "fact")
    raw_status = row.get("status", "active")
    try:
        mem_type = MemoryType(raw_type)
    except ValueError:
        mem_type = MemoryType.fact
    try:
        mem_status = MemoryStatus(raw_status)
    except ValueError:
        mem_status = MemoryStatus.active
    return MemoryEntry(
        id=row.get("id", row.get("@rid", "")),
        content=row.get("content", ""),
        namespace=row.get("namespace", ""),
        created_at=_parse_dt(row.get("created_at")) or _now(),
        superseded_at=_parse_dt(row.get("superseded_at")),
        tags=row.get("tags") or [],
        source=row.get("source", "agent"),
        metadata=row.get("metadata") or {},
        memory_type=mem_type,
        status=mem_status,
        author=row.get("author", ""),
        affects=row.get("affects") or [],
        rationale=row.get("rationale", ""),
        expires_at=_parse_dt(row.get("expires_at")),
        review_by=_parse_dt(row.get("review_by")),
        provenance=Provenance(**(row.get("provenance") or {})) if row.get("provenance") else Provenance(),
    )


def _row_to_entity(row: dict) -> Entity:
    return Entity(
        id=row.get("id", row.get("@rid", "")),
        name=row.get("name", ""),
        entity_type=row.get("entity_type", "CONCEPT"),
        namespace=row.get("namespace", ""),
        created_at=_parse_dt(row.get("created_at")) or _now(),
        superseded_at=_parse_dt(row.get("superseded_at")),
    )


def _row_to_asset(row: dict) -> AssetReference:
    return AssetReference(
        id=row.get("id", row.get("@rid", "")),
        path=row.get("path", ""),
        format=row.get("format", "unknown"),
        sha256=row.get("sha256", ""),
        extracted_content=row.get("extracted_content", ""),
        namespace=row.get("namespace", ""),
        created_at=_parse_dt(row.get("created_at")) or _now(),
        superseded_at=_parse_dt(row.get("superseded_at")),
        created_by=row.get("created_by", "agent"),
    )


def _row_to_secret(row: dict) -> Secret:
    return Secret(
        id=row.get("id", row.get("@rid", "")),
        key_name=row.get("key_name", ""),
        note=row.get("note", ""),
        secret_type=row.get("secret_type", "api_key"),
        namespace=row.get("namespace", ""),
        value_enc=row.get("value_enc", ""),
        dek_enc=row.get("dek_enc", ""),
        created_at=_parse_dt(row.get("created_at")) or _now(),
        superseded_at=_parse_dt(row.get("superseded_at")),
        created_by=row.get("created_by", "unknown"),
        tags=row.get("tags") or [],
    )
