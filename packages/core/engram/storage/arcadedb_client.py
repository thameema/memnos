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
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

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
    Relation,
    SearchResult,
    Secret,
    VaultAuditLog,
)

logger = logging.getLogger(__name__)

_DB_NAME = "engram"
_VECTOR_DIM = 384          # all-MiniLM-L6-v2 and nomic-embed-text-v1.5 (truncated)
_RECENCY_HALF_LIFE = 90    # days — memory from 90 days ago gets 0.5x recency weight


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
            # Properties — Memory
            "CREATE PROPERTY Memory.id IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.content IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.namespace IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.created_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.superseded_at IF NOT EXISTS DATETIME",
            "CREATE PROPERTY Memory.tags IF NOT EXISTS LIST",
            "CREATE PROPERTY Memory.source IF NOT EXISTS STRING",
            "CREATE PROPERTY Memory.metadata IF NOT EXISTS MAP",
            f"CREATE PROPERTY Memory.content_embedding IF NOT EXISTS ARRAY OF FLOAT",
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
            f"CREATE PROPERTY Asset.content_embedding IF NOT EXISTS ARRAY OF FLOAT",
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
            # Indices for namespace filtering (common query pattern)
            "CREATE INDEX ON Memory (namespace) IF NOT EXISTS",
            "CREATE INDEX ON Entity (namespace, name) IF NOT EXISTS",
            "CREATE INDEX ON Fact (namespace) IF NOT EXISTS",
            "CREATE INDEX ON Asset (path, namespace) IF NOT EXISTS",
            "CREATE INDEX ON Secret (namespace, key_name) IF NOT EXISTS",
            "CREATE INDEX ON VaultAuditLog (namespace) IF NOT EXISTS",
            "CREATE INDEX ON Memory (id) IF NOT EXISTS",
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
        try:
            await self._command(
                f"CREATE INDEX ON {type_name} ({prop}) HNSW "
                f"{{\"vectorDimensions\": {self._vector_dim}, \"vectorSimilarityFunction\": \"COSINE\"}} "
                f"IF NOT EXISTS"
            )
        except Exception as exc:
            logger.debug("Vector index on %s.%s skipped: %s", type_name, prop, exc)

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
            "embedding": embedding,
        }
        await self._command(sql, params)
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
        return bool(rows)

    async def delete_memory(self, memory_id: str, namespace: str) -> bool:
        """Hard-delete a memory and its outgoing edges."""
        rows = await self._command(
            "DELETE VERTEX Memory WHERE id = :id AND namespace = :ns",
            {"id": memory_id, "ns": namespace},
        )
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
    ) -> list[SearchResult]:
        """Search Memory by vector similarity with recency weighting."""
        ns_filter = "all" if namespace in ("all", "", "*") else namespace
        superseded_clause = "" if include_superseded else "AND superseded_at IS NULL"

        # ArcadeDB HNSW vector search using @vectorNeighbors
        sql = (
            f"SELECT *, $score AS vec_score FROM Memory "
            f"WHERE @vectorNeighbors('content_embedding', :vec, :topK, 'COSINE') "
            f"AND (namespace = :ns OR :ns = 'all' OR namespace LIKE :ns_prefix) "
            f"{superseded_clause} "
            f"ORDER BY $score DESC LIMIT :topK"
        )
        try:
            rows = await self._query(
                sql,
                {
                    "vec": embedding,
                    "topK": top_k,
                    "ns": ns_filter,
                    "ns_prefix": f"{ns_filter}:%",
                },
            )
        except Exception as exc:
            logger.warning("Vector search failed, falling back to scan: %s", exc)
            rows = await self._fallback_scan(namespace, top_k, include_superseded)

        results: list[SearchResult] = []
        for row in rows:
            memory = _row_to_memory(row)
            vec_score = float(row.get("vec_score", row.get("$score", 0.5)))
            recency = _recency_score(memory.created_at)
            combined = _combined_score(vec_score, recency)
            results.append(SearchResult(
                memory=memory,
                score=combined,
                source="vector",
                is_current=memory.is_current,
                recency_score=recency,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

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
        self, namespace: str, top_k: int, include_superseded: bool
    ) -> list[dict]:
        """Fallback: return recent memories without vector scoring."""
        ns_filter = namespace if namespace not in ("all", "", "*") else None
        where_parts = []
        params: dict = {}
        if ns_filter:
            where_parts.append("(namespace = :ns OR namespace LIKE :ns_prefix)")
            params["ns"] = ns_filter
            params["ns_prefix"] = f"{ns_filter}:%"
        if not include_superseded:
            where_parts.append("superseded_at IS NULL")
        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        sql = f"SELECT * FROM Memory {where} ORDER BY created_at DESC LIMIT :topK"
        params["topK"] = top_k
        return await self._query(sql, params)

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
            "DELETE VERTEX Secret WHERE id = :id AND namespace = :ns",
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
    return MemoryEntry(
        id=row.get("id", row.get("@rid", "")),
        content=row.get("content", ""),
        namespace=row.get("namespace", ""),
        created_at=_parse_dt(row.get("created_at")) or _now(),
        superseded_at=_parse_dt(row.get("superseded_at")),
        tags=row.get("tags") or [],
        source=row.get("source", "agent"),
        metadata=row.get("metadata") or {},
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
