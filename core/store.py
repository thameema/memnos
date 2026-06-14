"""B1 — storage backend for the brain-inspired schema.

One ACID Postgres engine. Writes raw_turns (verbatim), episodic events, entities/
mentions/edges (associative graph), and (for B2) semantic facts + provenance.
Schema identifiers are validated; values are parameterized.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

_IDENT = re.compile(r"^[a-z0-9_]+$")


def vlit(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# Postgres builds an in-memory parse tree for websearch_to_tsquery whose depth scales with
# the number of lexemes; a very long recall query (thousands of tokens) overflows the
# backend stack — "tsquery stack too small" / "stack depth limit exceeded" — and a recall
# that should return 200 instead crashes (issue #15). The query's discriminative signal
# lives in its first words anyway, so clamp the text fed to FTS to a sane token cap. The
# FULL query is still used for the vector arm (the embedding is order-insensitive and
# fixed-size) and for the cross-encoder, so retrieval quality on normal queries is
# unchanged — only pathological queries are bounded. MEMNOS_FTS_MAX_TOKENS tunes the cap.
def _fts_max_tokens() -> int:
    try:
        return max(1, int(os.environ.get("MEMNOS_FTS_MAX_TOKENS", "200")))
    except (TypeError, ValueError):
        return 200


def fts_clamp(qtext: str) -> str:
    """Clamp the text passed to websearch_to_tsquery to the first N whitespace tokens so a
    pathologically long query can never overflow the Postgres tsquery parser stack."""
    if not qtext:
        return qtext
    parts = qtext.split()
    cap = _fts_max_tokens()
    if len(parts) <= cap:
        return qtext
    return " ".join(parts[:cap])


# The #15 fix clamped only the FTS arm; the EMBEDDING and the cross-encoder RERANKER still
# saw the full query (up to MEMNOS_QUERY_MAX_CHARS=20000 chars). An 8000-word / ~40KB query
# then embedded + reranked the whole thing — ~5s of pure clamp-able overhead, even though no
# legitimate recall is thousands of words and both models cap their own input length anyway
# (a sentence-transformer cross-encoder truncates past ~512 tokens; the embedder past its own
# limit). Clamp the query that reaches the embedder + reranker to a sane token prefix: its
# discriminative signal lives in the first few hundred tokens, so normal queries (well under
# the cap) are byte-for-byte untouched and only pathological ones are bounded.
# MEMNOS_QUERY_RERANK_MAX_TOKENS tunes the cap (default 384 tokens).
def _query_max_tokens() -> int:
    try:
        return max(1, int(os.environ.get("MEMNOS_QUERY_RERANK_MAX_TOKENS", "384")))
    except (TypeError, ValueError):
        return 384


def query_clamp(qtext: str) -> str:
    """Clamp the query text fed to the embedding model and the cross-encoder reranker to the
    first N whitespace tokens. Returns the input UNCHANGED when at/under the cap, so normal
    queries embed + rerank identically to before — only pathological long queries are bounded.
    """
    if not qtext:
        return qtext
    parts = qtext.split()
    cap = _query_max_tokens()
    if len(parts) <= cap:
        return qtext
    return " ".join(parts[:cap])


# pgvector >= 0.7 ships the half-precision `halfvec` type (half the storage). pgvector 0.6
# (the version Debian/Ubuntu ship in apt) does not — only the full-precision `vector` type.
# memnos feature-detects which is available and uses halfvec when it can, vector otherwise,
# so a clean apt install of pgvector 0.6 works with no source build. The two are wire- and
# query-compatible for everything memnos does (cosine distance, HNSW); halfvec is purely a
# storage optimization. The chosen type is consistent within one database.
MIN_PGVECTOR_HALFVEC = (0, 7, 0)


def _vtuple(ver: str) -> tuple:
    parts = []
    for p in str(ver).split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def detect_vector_type(conn) -> str:
    """Return the embedding column type to use ('halfvec' or 'vector') for this database.
    Prefers the type already baked into an existing schema (so we never cast to a type the
    columns aren't); falls back to the installed pgvector version (halfvec needs >= 0.7)."""
    with conn.cursor() as c:
        # 1) If a memnos schema already exists, mirror its actual column type — authoritative.
        c.execute(
            "SELECT t.typname FROM pg_attribute a "
            "JOIN pg_class cl ON cl.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE n.nspname LIKE 'tenant_%' AND cl.relname = 'raw_turns' "
            "AND a.attname = 'embedding' LIMIT 1")
        row = c.fetchone()
        if row:
            name = row["typname"] if isinstance(row, dict) else row[0]
            if name in ("halfvec", "vector"):
                return name
        # 2) No schema yet — pick by installed pgvector version.
        c.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        r = c.fetchone()
        if r:
            ver = r["extversion"] if isinstance(r, dict) else r[0]
            if _vtuple(ver) >= MIN_PGVECTOR_HALFVEC:
                return "halfvec"
        return "vector"


class BrainStore:
    def __init__(self, dsn: str | None = None, conn=None):
        # Accept a pooled connection (production) or open one from a DSN (scripts/tests).
        if conn is not None:
            self.conn = conn
            self._owns = False
        else:
            self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            self._owns = True
        self._vtype = None

    @property
    def vtype(self) -> str:
        """Embedding column/cast type for this DB: 'halfvec' (pgvector>=0.7) or 'vector' (0.6).
        Detected once from the live database and cached. Value is from a fixed safe set, so it
        is safe to interpolate into SQL."""
        if self._vtype is None:
            self._vtype = detect_vector_type(self.conn)
        return self._vtype

    @property
    def vops(self) -> str:
        """HNSW cosine ops class matching the vector type."""
        return "halfvec_cosine_ops" if self.vtype == "halfvec" else "vector_cosine_ops"

    def _chk(self, s: str) -> None:
        if not _IDENT.match(s):
            raise ValueError(f"unsafe schema identifier: {s!r}")

    # --- provisioning -----------------------------------------------------
    def create_schema(self, tenant: str, dim: int = 1536) -> str:
        # (Re)load the schema DDL function from schema.sql first, so additive schema
        # changes (e.g. new columns via ALTER ... ADD COLUMN IF NOT EXISTS) deploy on every
        # boot — then materialise/upgrade the tenant schema. Rolling, additive-only.
        import os
        sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(sql_path) as fh:
            ddl = fh.read()
        vtype, vops = self.vtype, self.vops
        with self.conn.cursor() as c:
            c.execute(ddl)                                   # CREATE OR REPLACE FUNCTION (idempotent)
            c.execute("SELECT create_brain_schema(%s, %s, %s, %s)", (tenant, dim, vtype, vops))
        return f"tenant_{tenant}"

    def drop_schema(self, tenant: str) -> None:
        s = f"tenant_{tenant}"; self._chk(s)
        with self.conn.cursor() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")

    # --- sensory / verbatim ----------------------------------------------
    def insert_raw_turn(self, schema, ns, session_id, speaker, text, observed_at, vec,
                        author=None, memory_type=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding,author_principal,memory_type) "
                f"VALUES(%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s) RETURNING id",
                (ns, session_id, speaker, text, observed_at,
                 vlit(vec) if vec is not None else None, author, memory_type))
            return c.fetchone()["id"]

    # --- episodic ---------------------------------------------------------
    def insert_episodic(self, schema, ns, session_id, text, *, summary=None,
                        t_start=None, t_end=None, observed_at=None, salience=0.0,
                        source_turn_ids: Iterable[int] = (), vec=None, author=None,
                        memory_type=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.episodic"
                f"(namespace,session_id,text,summary,t_start,t_end,observed_at,salience,source_turn_ids,embedding,author_principal,memory_type) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s) RETURNING id",
                (ns, session_id, text, summary, t_start, t_end, observed_at, salience,
                 list(source_turn_ids), vlit(vec) if vec is not None else None, author,
                 memory_type))
            return c.fetchone()["id"]

    def uncovered_raw_turns(self, schema, ns) -> list[dict]:
        """Raw turns not yet assigned to any episode (for incremental segmentation)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT id, speaker, text, observed_at, session_id, memory_type
                FROM {schema}.raw_turns
                WHERE namespace=%s AND id NOT IN (
                    SELECT unnest(source_turn_ids) FROM {schema}.episodic
                    WHERE namespace=%s AND source_turn_ids IS NOT NULL)
                ORDER BY observed_at, id""", (ns, ns))
            return c.fetchall()

    def link_episode_provenance(self, schema, ns, episode_id, source_turn_ids) -> int:
        """Two-level provenance: link semantic facts whose source turns overlap this episode
        (fact → episode → turn), populating the provenance table as the schema intends."""
        self._chk(schema)
        if not source_turn_ids:
            return 0
        with self.conn.cursor() as c:
            c.execute(f"""
                INSERT INTO {schema}.provenance(semantic_id, episodic_id)
                SELECT s.id, %s FROM {schema}.semantic s
                WHERE s.namespace=%s AND s.source_turn_ids && %s::bigint[]
                ON CONFLICT DO NOTHING""", (episode_id, ns, list(source_turn_ids)))
            return c.rowcount

    def get_episode(self, schema, ns, episode_id) -> dict | None:
        """An episode + its verbatim turns + the facts derived from it (via provenance)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, session_id, summary, text, t_start, t_end, salience, "
                      f"access_count, source_turn_ids, memory_type FROM {schema}.episodic WHERE id=%s AND namespace=%s",
                      (episode_id, ns))
            ep = c.fetchone()
            if not ep:
                return None
            sids = ep.get("source_turn_ids") or []
            turns = []
            if sids:
                c.execute(f"SELECT id, speaker, text AS content, observed_at FROM {schema}.raw_turns "
                          f"WHERE id = ANY(%s) AND namespace=%s ORDER BY id", (list(sids), ns))
                turns = c.fetchall()
            c.execute(f"SELECT s.id, s.statement FROM {schema}.provenance p "
                      f"JOIN {schema}.semantic s ON s.id=p.semantic_id "
                      f"WHERE p.episodic_id=%s AND s.expired_at IS NULL", (episode_id,))
            facts = c.fetchall()
        return {"episode": ep, "turns": turns, "facts": facts}

    def touch_episodes(self, schema, episode_ids) -> None:
        """Record access (recency/frequency signal for decay)."""
        self._chk(schema)
        if not episode_ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.episodic SET last_access=now(), access_count=access_count+1 "
                      f"WHERE id = ANY(%s)", (list(episode_ids),))

    def decay_episodes(self, schema, ns, *, half_life_days=30) -> int:
        """DECAY pass: recompute episodic salience as time-weighted recency (half-life) plus
        an access-frequency boost. Recent/often-recalled episodes stay salient; old untouched
        ones fade. Semantic facts are untouched (they persist). Returns # episodes updated."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                UPDATE {schema}.episodic SET salience = LEAST(1.0,
                    exp(-ln(2.0) * (EXTRACT(EPOCH FROM (now() - COALESCE(last_access, observed_at)))
                        / 86400.0) / %s)
                    + 0.05 * LEAST(access_count, 10))
                WHERE namespace=%s RETURNING id""", (float(half_life_days), ns))
            return len(c.fetchall())

    # --- associative graph ------------------------------------------------
    def upsert_entity(self, schema, ns, name, vec=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.entities(namespace,name,embedding) "
                f"VALUES(%s,%s,%s::{self.vtype}) ON CONFLICT (namespace,name) DO UPDATE SET name=EXCLUDED.name "
                f"RETURNING id",
                (ns, name, vlit(vec) if vec is not None else None))
            return c.fetchone()["id"]

    def add_mention(self, schema, entity_id, memory_id, memory_kind) -> None:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.mentions(entity_id,memory_id,memory_kind) "
                f"VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (entity_id, memory_id, memory_kind))

    def bump_edge(self, schema, ns, src, dst, w=1.0) -> None:
        """Co-mention edge between two entities; weight accumulates (Hebbian-ish)."""
        self._chk(schema)
        if src == dst:
            return
        a, b = sorted((src, dst))
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.edges(namespace,src_entity,dst_entity,weight) "
                f"VALUES(%s,%s,%s,%s) ON CONFLICT (namespace,src_entity,dst_entity) "
                f"DO UPDATE SET weight = {schema}.edges.weight + EXCLUDED.weight",
                (ns, a, b, w))

    # --- semantic + provenance (used by B2 consolidation) -----------------
    def insert_semantic(self, schema, ns, kind, statement, *, subject=None, predicate=None,
                        obj=None, valid_from=None, valid_to=None, confidence=1.0,
                        salience=0.0, vec=None, source_turn_ids: Iterable[int] = (),
                        author=None, memory_type=None, observed_at=None) -> int:
        # observed_at = the OBSERVATION (knowledge) axis used by bi-temporal supersession:
        # when this fact was learned (server: now; session ingest: session date). None →
        # column default now() (legacy callers unchanged).
        self._chk(schema)
        src = list(source_turn_ids) if source_turn_ids else None
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.semantic"
                f"(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding,source_turn_ids,author_principal,memory_type,observed_at) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::{self.vtype},%s,%s,%s,COALESCE(%s,now())) RETURNING id",
                (ns, kind, statement, subject, predicate, obj, valid_from, valid_to,
                 confidence, salience, vlit(vec) if vec is not None else None, src, author,
                 memory_type, observed_at))
            return c.fetchone()["id"]

    def provenance_of(self, schema, ns, semantic_id) -> dict | None:
        """Evidence chain for a fact: the fact + the verbatim raw_turn(s) it was extracted
        from (or, for a dossier, the turns its source facts derived from)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, kind, statement, valid_from, valid_to, source_turn_ids "
                      f"FROM {schema}.semantic WHERE id=%s AND namespace=%s", (semantic_id, ns))
            fact = c.fetchone()
            if not fact:
                return None
            srcs = fact.get("source_turn_ids") or []
            sources = []
            if srcs:
                c.execute(f"SELECT id, speaker, text AS content, observed_at "
                          f"FROM {schema}.raw_turns WHERE id = ANY(%s) AND namespace=%s ORDER BY id",
                          (list(srcs), ns))
                sources = c.fetchall()
        return {"fact": {"id": fact["id"], "kind": fact["kind"], "statement": fact["statement"],
                         "valid_from": fact["valid_from"], "valid_to": fact["valid_to"]},
                "source_turn_ids": srcs, "sources": sources}

    def add_provenance(self, schema, semantic_id, episodic_ids: Iterable[int]) -> None:
        self._chk(schema)
        with self.conn.cursor() as c:
            for eid in episodic_ids:
                c.execute(f"INSERT INTO {schema}.provenance(semantic_id,episodic_id) "
                          f"VALUES(%s,%s) ON CONFLICT DO NOTHING", (semantic_id, eid))

    # --- reads for consolidation (B2) -------------------------------------
    def fetch_episodes(self, schema, ns, only_unconsolidated=True) -> list[dict]:
        self._chk(schema)
        where = "AND consolidated = false" if only_unconsolidated else ""
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, session_id, text, t_start, t_end, salience "
                      f"FROM {schema}.episodic WHERE namespace=%s {where} ORDER BY id", (ns,))
            return c.fetchall()

    def entity_episodes(self, schema, ns, min_episodes=2) -> list[dict]:
        """Entities and the episodic events that mention them (the cluster for a dossier)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT e.name, array_agg(DISTINCT m.memory_id) AS ep_ids "
                f"FROM {schema}.entities e JOIN {schema}.mentions m ON m.entity_id = e.id "
                f"WHERE e.namespace=%s AND m.memory_kind='episodic' "
                f"GROUP BY e.name HAVING count(DISTINCT m.memory_id) >= %s "
                f"ORDER BY count(DISTINCT m.memory_id) DESC", (ns, min_episodes))
            return c.fetchall()

    def mark_consolidated(self, schema, episodic_ids) -> None:
        self._chk(schema)
        if not episodic_ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.episodic SET consolidated=true WHERE id = ANY(%s)",
                      (list(episodic_ids),))

    def supersede_similar(self, schema, ns, new_vec, subject, valid_from, thresh=0.12) -> int:
        """Dedup-style: close out near-IDENTICAL currently-valid facts (distance < thresh)."""
        self._chk(schema)
        if not subject:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%s "
                f"WHERE namespace=%s AND subject_entity=%s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND (embedding <=> %s::{self.vtype}) < %s RETURNING id",
                (valid_from, ns, subject, vlit(new_vec), thresh))
            return len(c.fetchall())

    def supersede_subject(self, schema, ns, subject, new_vec, valid_from,
                          dist_lo=0.05, dist_hi=0.50) -> int:
        """BELIEF-CHANGE supersession (the 'never serve stale as current' guarantee):
        when a new fact about `subject` arrives, close out the PRIOR currently-valid facts
        about that same subject that are TOPICALLY similar but not identical (cosine
        distance in [dist_lo, dist_hi]) and started earlier — e.g. 'lives in Austin' is
        superseded by 'lives in Seattle'. Sets valid_to (valid time);
        never deletes. Returns # superseded."""
        self._chk(schema)
        if not subject or valid_from is None:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%(vf)s "
                f"WHERE namespace=%(ns)s AND subject_entity=%(sub)s AND valid_to IS NULL "
                f"AND expired_at IS NULL AND (valid_from IS NULL OR valid_from < %(vf)s) "
                f"AND (embedding <=> %(v)s::{self.vtype}) BETWEEN %(lo)s AND %(hi)s RETURNING id",
                {"vf": valid_from, "ns": ns, "sub": subject, "v": vlit(new_vec),
                 "lo": dist_lo, "hi": dist_hi})
            return len(c.fetchall())

    def supersede_predicate(self, schema, ns, subject, predicate, obj, valid_from,
                            observed_at=None, historical=False, event_date=None) -> list[int]:
        """ROBUST belief-change supersession: when a new (subject, predicate, object)
        fact arrives, close out the currently-valid facts with the SAME subject+predicate
        but a DIFFERENT object (e.g. lives-in Austin → lives-in Seattle). Sets valid_to;
        never deletes. This is what makes 'what is X's CURRENT y?' trustworthy.

        BI-TEMPORAL guard (see service._write_fact): belief change is keyed on the
        OBSERVATION axis — the old fact must have been observed no later than the new
        one (a fact learned later is newer knowledge even when its EVENT date backdates,
        e.g. "moved last week"). Only when the new statement is flagged `historical`
        (past-state wording) do we additionally require event order: the old fact's
        valid_from must be <= `event_date` (the EXPLICIT in-statement date; the caller
        skips the call entirely when a historical statement has none) — a backdated
        historical statement must not displace the current value. valid_to = the new
        fact's event date, clamped to never precede the closed fact's own valid_from.
        Returns the superseded ids (callers stamp superseded_by on them)."""
        self._chk(schema)
        if not subject or not predicate:
            return []
        obs = observed_at if observed_at is not None else valid_from
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic "
                f"SET valid_to=GREATEST(coalesce(valid_from, %(vt)s), %(vt)s) "
                f"WHERE namespace=%(ns)s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND lower(subject_entity)=lower(%(sub)s) AND lower(predicate)=lower(%(pred)s) "
                f"AND lower(coalesce(object,'')) <> lower(coalesce(%(obj)s,'')) "
                f"AND (%(obs)s::timestamptz IS NULL OR observed_at <= %(obs)s) "
                f"AND (NOT %(hist)s OR valid_from IS NULL "
                f"     OR (%(ev)s::timestamptz IS NOT NULL AND valid_from <= %(ev)s)) "
                f"RETURNING id",
                {"vt": valid_from, "ns": ns, "sub": subject, "pred": predicate, "obj": obj,
                 "obs": obs, "hist": bool(historical), "ev": event_date})
            return [r["id"] for r in c.fetchall()]

    def mark_superseded_by(self, schema, ids, new_id) -> None:
        """Stamp the supersession LINK (additive column): which fact replaced these."""
        self._chk(schema)
        if not ids:
            return
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.semantic SET superseded_by=%s WHERE id = ANY(%s)",
                      (new_id, list(ids)))

    def nearest_live_facts(self, schema, ns, vec, *, k=8, exclude_id=None,
                           observed_before=None) -> list[dict]:
        """Top-k semantically nearest LIVE extracted facts (HNSW) in a namespace — the
        candidate set for the reversal/negation close-out. kind='fact' only (dossiers/
        constraints are consolidation-owned), optional knowledge-axis cutoff."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, subject_entity, (embedding <=> %(v)s::{self.vtype}) AS dist "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL AND embedding IS NOT NULL "
                f"AND (%(ex)s::bigint IS NULL OR id <> %(ex)s) "
                f"AND (%(obs)s::timestamptz IS NULL OR observed_at <= %(obs)s) "
                f"ORDER BY embedding <=> %(v)s::{self.vtype} LIMIT %(k)s",
                {"v": vlit(vec), "ns": ns, "ex": exclude_id, "obs": observed_before, "k": k})
            return c.fetchall()

    def close_out(self, schema, ns, fact_id, *, valid_to, superseded_by=None) -> int:
        """Close ONE live fact (belief change): set valid_to (clamped to its own
        valid_from) + the superseded_by link. Never deletes. Returns 0/1."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic "
                f"SET valid_to=GREATEST(coalesce(valid_from, %(vt)s), %(vt)s), superseded_by=%(by)s "
                f"WHERE id=%(id)s AND namespace=%(ns)s AND valid_to IS NULL AND expired_at IS NULL "
                f"RETURNING id",
                {"vt": valid_to, "by": superseded_by, "id": fact_id, "ns": ns})
            return len(c.fetchall())

    def find_near_duplicate(self, schema, ns, vec, subject, thresh) -> dict | None:
        """Nearest LIVE extracted fact within `thresh` cosine distance (write-path dedupe).
        Subject agreement is required only when BOTH sides carry a subject."""
        self._chk(schema)
        if vec is None or thresh <= 0:
            return None
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, (embedding <=> %(v)s::{self.vtype}) AS dist "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL AND embedding IS NOT NULL "
                f"AND (%(sub)s::text IS NULL OR subject_entity IS NULL "
                f"     OR lower(subject_entity)=lower(%(sub)s)) "
                f"AND (embedding <=> %(v)s::{self.vtype}) < %(t)s "
                f"ORDER BY embedding <=> %(v)s::{self.vtype} LIMIT 1",
                {"v": vlit(vec), "ns": ns, "sub": subject, "t": thresh})
            return c.fetchone()

    def near_duplicate_pairs(self, schema, ids, thresh) -> list[tuple]:
        """RECALL-PATH dedupe (issue #2): among the GIVEN candidate raw-turn ids, return
        (a, b) pairs whose embeddings are within `thresh` cosine distance (a<b). A single
        self-join over the small candidate set (k<=~80) — NOT a namespace scan — so it is
        cheap and bounded. Reuses the write-path dedupe threshold (MEMNOS_DEDUPE_THRESHOLD,
        0.03). The caller collapses the resulting groups, keeping one survivor."""
        self._chk(schema)
        ids = [i for i in (ids or ()) if i is not None]
        if len(ids) < 2 or thresh <= 0:
            return []
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT a.id AS a, b.id AS b "
                f"FROM {schema}.raw_turns a JOIN {schema}.raw_turns b "
                f"  ON a.id < b.id "
                f"WHERE a.id = ANY(%(ids)s) AND b.id = ANY(%(ids)s) "
                f"  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
                f"  AND (a.embedding <=> b.embedding) < %(t)s",
                {"ids": ids, "t": thresh})
            return [(r["a"], r["b"]) for r in c.fetchall()]

    def bump_restatement(self, schema, fact_id, source_turn_ids=()) -> None:
        """Reinforce an existing live fact instead of inserting a near-duplicate:
        restatements counter + salience bump + provenance union (additive columns)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET restatements = restatements + 1, "
                f"salience = LEAST(1.0, salience + 0.1), "
                f"source_turn_ids = (SELECT ARRAY(SELECT DISTINCT t FROM "
                f"unnest(coalesce(source_turn_ids,'{{}}'::bigint[]) || %s::bigint[]) AS t ORDER BY t)) "
                f"WHERE id=%s", (list(source_turn_ids or ()), fact_id))

    # --- namespace reconcile (issue #10 residual C: pre-fix contradiction debt) ------
    def live_facts_newest_first(self, schema, ns, limit=None) -> list[dict]:
        """The namespace's LIVE extracted facts, newest-first on the observation axis —
        the walk order for `memnos namespace reconcile` (newer knowledge closes older)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
                f"observed_at, source_turn_ids FROM {schema}.semantic "
                f"WHERE namespace=%(ns)s AND kind='fact' AND valid_to IS NULL "
                f"AND expired_at IS NULL "
                f"ORDER BY observed_at DESC NULLS LAST, id DESC "
                f"LIMIT %(lim)s", {"ns": ns, "lim": limit})
            return c.fetchall()

    def is_live(self, schema, ns, fact_id) -> bool:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT 1 FROM {schema}.semantic WHERE id=%s AND namespace=%s "
                      f"AND valid_to IS NULL AND expired_at IS NULL", (fact_id, ns))
            return c.fetchone() is not None

    def older_near_duplicate(self, schema, ns, fact_id, thresh) -> dict | None:
        """Reconcile twin of find_near_duplicate, against STORED embeddings: the nearest
        OLDER live fact within `thresh` cosine distance of fact `fact_id` (same subject
        agreement rule: required only when both sides carry one). No new embeddings."""
        self._chk(schema)
        if thresh <= 0:
            return None
        with self.conn.cursor() as c:
            c.execute(
                f"WITH a AS (SELECT id, embedding, subject_entity, observed_at "
                f"           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
                f"SELECT s.id, s.statement, (s.embedding <=> a.embedding) AS dist "
                f"FROM {schema}.semantic s, a "
                f"WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
                f"AND s.expired_at IS NULL AND s.embedding IS NOT NULL "
                f"AND (coalesce(s.observed_at,'epoch'), s.id) < (coalesce(a.observed_at,'epoch'), a.id) "
                f"AND (a.subject_entity IS NULL OR s.subject_entity IS NULL "
                f"     OR lower(s.subject_entity)=lower(a.subject_entity)) "
                f"AND (s.embedding <=> a.embedding) < %(t)s "
                f"ORDER BY s.embedding <=> a.embedding LIMIT 1",
                {"id": fact_id, "ns": ns, "t": thresh})
            return c.fetchone()

    def nearest_live_facts_to(self, schema, ns, fact_id, *, k=8) -> list[dict]:
        """Reconcile twin of nearest_live_facts, against STORED embeddings: top-k live
        facts nearest to fact `fact_id`, restricted to the SAME observation cutoff the
        write path uses (observed no later than the anchor), excluding the anchor."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"WITH a AS (SELECT id, embedding, observed_at "
                f"           FROM {schema}.semantic WHERE id=%(id)s AND namespace=%(ns)s) "
                f"SELECT s.id, s.statement, s.subject_entity, "
                f"       (s.embedding <=> a.embedding) AS dist "
                f"FROM {schema}.semantic s, a "
                f"WHERE s.namespace=%(ns)s AND s.kind='fact' AND s.valid_to IS NULL "
                f"AND s.expired_at IS NULL AND s.embedding IS NOT NULL AND s.id <> a.id "
                f"AND (a.observed_at IS NULL OR s.observed_at <= a.observed_at) "
                f"ORDER BY s.embedding <=> a.embedding LIMIT %(k)s",
                {"id": fact_id, "ns": ns, "k": k})
            return c.fetchall()

    def turn_supersession(self, schema, turn_ids) -> dict:
        """STALE-TURN lookup for recall (issue #10 residual B): for the RETRIEVED turn
        ids only (one batched query — O(retrieved), never O(namespace); GIN index on
        semantic.source_turn_ids), return {turn_id: close_date} for turns whose derived
        semantic facts exist AND are ALL superseded (valid_to set or superseded_by set).
        Turns with no derived facts, or with at least one still-live fact, are absent —
        they stay untouched in ranking/rendering. close_date = the latest valid_to of
        the closed facts (None only in the superseded_by-without-valid_to edge case)."""
        self._chk(schema)
        ids = [int(t) for t in turn_ids if t is not None]
        if not ids:
            return {}
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT t.tid AS turn_id, max(s.valid_to) AS closed_at "
                f"FROM unnest(%(ids)s::bigint[]) AS t(tid) "
                f"JOIN {schema}.semantic s ON s.source_turn_ids @> ARRAY[t.tid] "
                f"  AND s.kind='fact' AND s.expired_at IS NULL "
                f"GROUP BY t.tid "
                f"HAVING bool_and(s.valid_to IS NOT NULL OR s.superseded_by IS NOT NULL)",
                {"ids": ids})
            return {r["turn_id"]: r["closed_at"] for r in c.fetchall()}

    def expire(self, schema, ns, semantic_id) -> None:
        """System-time invalidation (CORRECTION, not belief change): mark a fact as
        system-removed (expired_at). Excluded from all retrieval; history preserved."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"UPDATE {schema}.semantic SET expired_at=now() "
                      f"WHERE id=%s AND namespace=%s", (semantic_id, ns))

    # --- dual hybrid search (B3 retrieval) --------------------------------
    def max_observed_at(self, schema, ns):
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT max(observed_at) AS m FROM {schema}.episodic WHERE namespace=%s", (ns,))
            return c.fetchone()["m"]

    def search_episodic(self, schema, ns, qvec, qtext, k=40) -> list[dict]:
        """Hybrid RRF (vector+FTS) over EPISODIC; returns observed_at for recency."""
        self._chk(schema)
        sql = f"""
        WITH vec AS (SELECT id, text, observed_at, memory_type, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}) rnk
                     FROM {schema}.episodic WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::{self.vtype} LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, memory_type, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.episodic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, memory_type, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,memory_type,rnk FROM vec UNION ALL SELECT id,text,observed_at,memory_type,rnk FROM fts) r
                  GROUP BY id,text,observed_at,memory_type)
        SELECT id, text AS content, observed_at, memory_type, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": fts_clamp(qtext), "ns": ns, "k": k})
            return c.fetchall()

    def search_raw_turns(self, schema, ns, qvec, qtext, k=40) -> list[dict]:
        """Hybrid RRF (vector+FTS) over RAW TURNS — the strong open/single-hop layer."""
        self._chk(schema)
        sql = f"""
        WITH vec AS (SELECT id, text, observed_at, author_principal, memory_type, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}) rnk
                     FROM {schema}.raw_turns WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::{self.vtype} LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, author_principal, memory_type, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.raw_turns, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, author_principal, memory_type, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,author_principal,memory_type,rnk FROM vec UNION ALL SELECT id,text,observed_at,author_principal,memory_type,rnk FROM fts) r
                  GROUP BY id,text,observed_at,author_principal,memory_type)
        SELECT id, text AS content, observed_at, author_principal AS author, memory_type, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": fts_clamp(qtext), "ns": ns, "k": k})
            return c.fetchall()

    def search_semantic(self, schema, ns, qvec, qtext, k=40, current_only=False) -> list[dict]:
        """Hybrid RRF (vector+FTS) over SEMANTIC; current_only filters superseded facts.
        Returns restatements + salience too — rank-time reinforcement signals for the
        fact arm (issue #11). ADDITIVE columns only; fetch semantics unchanged."""
        self._chk(schema)
        valid = "AND valid_to IS NULL" if current_only else ""
        sql = f"""
        WITH vec AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {valid} ORDER BY embedding <=> %(qv)s::{self.vtype} LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {valid} AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM vec UNION ALL SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM fts) r
                  GROUP BY id,statement,valid_from,author_principal,memory_type,restatements,salience)
        SELECT f.id, f.statement AS content, f.valid_from, f.author_principal AS author, f.memory_type, f.restatements, f.salience, f.score, s.subject_entity
        FROM fused f JOIN {schema}.semantic s ON s.id=f.id ORDER BY f.score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": fts_clamp(qtext), "ns": ns, "k": k})
            return c.fetchall()

    def search_semantic_temporal(self, schema, ns, qvec, qtext, k=40, *, start=None, end=None,
                                 current_only=False, order=None) -> list[dict]:
        """Temporal semantic retrieval: hybrid relevance (current_only → valid_to IS NULL)
        UNION-ed with event-time matches — facts inside [start,end] and, for first/last
        questions, the earliest/latest facts — so time-scoped evidence is guaranteed present.
        Returns id, content, valid_from. (Pure SQL; no LLM.)"""
        self._chk(schema)
        cur = "AND valid_to IS NULL" if current_only else ""
        base = f"""
        WITH vec AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY embedding <=> %(qv)s::{self.vtype}) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {cur} ORDER BY embedding <=> %(qv)s::{self.vtype} LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {cur} AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, author_principal, memory_type, restatements, salience, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM vec UNION ALL SELECT id,statement,valid_from,author_principal,memory_type,restatements,salience,rnk FROM fts) r
                  GROUP BY id,statement,valid_from,author_principal,memory_type,restatements,salience)
        SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience FROM fused ORDER BY score DESC LIMIT %(k)s"""
        params = {"qv": vlit(qvec), "qt": fts_clamp(qtext), "ns": ns, "k": k}
        rows, seen = [], set()
        with self.conn.cursor() as c:
            c.execute(base, params)
            for r in c.fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"]); rows.append(r)
            # event-time window guarantee
            if start and end:
                c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience "
                          f"FROM {schema}.semantic "
                          f"WHERE namespace=%s AND expired_at IS NULL AND valid_from >= %s AND valid_from < %s "
                          f"ORDER BY valid_from LIMIT %s", (ns, start, end, k))
                for r in c.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"]); rows.append(r)
            # first/last boundary facts
            if order in ("asc", "desc"):
                c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type, restatements, salience "
                          f"FROM {schema}.semantic "
                          f"WHERE namespace=%s AND expired_at IS NULL AND valid_from IS NOT NULL "
                          f"ORDER BY valid_from {('ASC' if order=='asc' else 'DESC')} LIMIT 6", (ns,))
                for r in c.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"]); rows.append(r)
        return rows

    def timeline(self, schema, ns, entities, *, start=None, end=None, order="asc", limit=20) -> list[dict]:
        """TIMELINE retrieval — the fix for 'vector can't find dated evidence'. Pull all
        facts about the query's entities, SORTED by event time (valid_from), optionally
        range-filtered (valid_from BETWEEN start AND end). A JOIN/range, not a cosine bet,
        so 'when did X happen' / 'what did X do in May 2023' surface the dated fact even
        though the question doesn't lexically match it. Pure SQL, no LLM."""
        self._chk(schema)
        where = ["namespace=%s", "expired_at IS NULL", "valid_from IS NOT NULL"]
        params = [ns]
        if entities:
            ors = []
            for e in entities:
                ors.append("(subject_entity = %s OR statement ILIKE %s)")
                params += [e, f"%{e}%"]
            where.append("(" + " OR ".join(ors) + ")")
        if start and end:
            where.append("valid_from >= %s AND valid_from < %s")
            params += [start, end]
        direction = "ASC" if order != "desc" else "DESC"
        params.append(limit)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, statement AS content, valid_from, author_principal AS author, memory_type "
                      f"FROM {schema}.semantic "
                      f"WHERE {' AND '.join(where)} ORDER BY valid_from {direction} LIMIT %s", params)
            return c.fetchall()

    def graph_expand(self, schema, ns, entity_names, *, hops=2, limit=20) -> list[dict]:
        """GRAPH TRAVERSAL (recursive CTE, no graph DB) — the relationship-reasoning a
        graph gives you. Seed from the query's entities, expand N hops over `edges`, then
        pull facts mentioned by the reachable entity set. Tests whether query-time graph
        traversal adds anything beyond the offline dossier pre-joining."""
        self._chk(schema)
        if not entity_names:
            return []
        names = [n.lower() for n in entity_names]
        sql = f"""
        WITH RECURSIVE seeds AS (
            SELECT id FROM {schema}.entities WHERE namespace=%(ns)s AND lower(name) = ANY(%(names)s)
        ),
        reach(id, hop) AS (
            SELECT id, 0 FROM seeds
            UNION
            SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1
            FROM reach r JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
            WHERE r.hop < %(hops)s
        )
        SELECT DISTINCT s.id, s.statement AS content, s.valid_from
        FROM {schema}.mentions m
        JOIN {schema}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic'
        WHERE m.entity_id IN (SELECT id FROM reach) AND s.namespace=%(ns)s AND s.expired_at IS NULL
        LIMIT %(lim)s"""
        with self.conn.cursor() as c:
            c.execute(sql, {"ns": ns, "names": names, "hops": hops, "lim": limit})
            return c.fetchall()

    def get_entity(self, schema, ns, name, *, depth=1, fact_limit=20) -> dict | None:
        """Entity lookup + its graph neighbourhood + the facts that mention it.
        depth=1 returns direct neighbours; depth>=2 expands over `edges` (recursive CTE).
        Pure SQL over the associative graph — no LLM, no graph DB."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, name FROM {schema}.entities WHERE namespace=%s AND lower(name)=lower(%s)",
                      (ns, name))
            ent = c.fetchone()
            if not ent:
                return None
            eid = ent["id"]
            # neighbours up to `depth` hops, with the edge weight of the first hop
            c.execute(f"""
                WITH RECURSIVE reach(id, hop) AS (
                    SELECT %(eid)s::bigint, 0
                    UNION
                    SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1
                    FROM reach r JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
                    WHERE r.hop < %(depth)s
                )
                SELECT DISTINCT e.name, max(g.weight) AS weight
                FROM reach r
                JOIN {schema}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id)
                JOIN {schema}.entities e ON e.id = CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END
                WHERE e.id <> %(eid)s AND e.namespace=%(ns)s
                GROUP BY e.name ORDER BY weight DESC LIMIT 50
            """, {"eid": eid, "depth": depth, "ns": ns})
            related = [{"name": r["name"], "weight": float(r["weight"] or 0)} for r in c.fetchall()]
            c.execute(f"""
                SELECT DISTINCT s.id, s.statement AS content, s.valid_from, s.valid_to
                FROM {schema}.mentions m
                JOIN {schema}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic'
                WHERE m.entity_id=%s AND s.namespace=%s AND s.expired_at IS NULL
                ORDER BY s.valid_from DESC NULLS LAST LIMIT %s
            """, (eid, ns, fact_limit))
            facts = c.fetchall()
        return {"entity": {"id": eid, "name": ent["name"]}, "related": related, "facts": facts}

    def get_related(self, schema, ns, name, *, limit=50) -> list[dict]:
        """Adjacency list for an entity — direct neighbours over `edges`, weight-ranked."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT e2.name, g.weight
                FROM {schema}.entities e1
                JOIN {schema}.edges g ON (g.src_entity=e1.id OR g.dst_entity=e1.id)
                JOIN {schema}.entities e2 ON e2.id = CASE WHEN g.src_entity=e1.id THEN g.dst_entity ELSE g.src_entity END
                WHERE e1.namespace=%s AND lower(e1.name)=lower(%s) AND e2.id<>e1.id
                ORDER BY g.weight DESC LIMIT %s
            """, (ns, name, limit))
            return [{"name": r["name"], "weight": float(r["weight"] or 0)} for r in c.fetchall()]

    def community(self, schema, ns, name, *, max_nodes=200) -> dict | None:
        """COMMUNITY (connected component) for an entity — the cluster it belongs to,
        found by expanding the co-mention `edges` graph to convergence (recursive CTE,
        UNION dedups → terminates). A dependency-free stand-in for Louvain: members of
        the same densely-connected neighbourhood surface together."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, name FROM {schema}.entities WHERE namespace=%s AND lower(name)=lower(%s)",
                      (ns, name))
            seed = c.fetchone()
            if not seed:
                return None
            c.execute(f"""
                WITH RECURSIVE comp(id) AS (
                    SELECT %(eid)s::bigint
                    UNION
                    SELECT CASE WHEN g.src_entity=comp.id THEN g.dst_entity ELSE g.src_entity END
                    FROM comp JOIN {schema}.edges g ON (g.src_entity=comp.id OR g.dst_entity=comp.id)
                    WHERE g.namespace=%(ns)s
                )
                SELECT e.name FROM comp JOIN {schema}.entities e ON e.id=comp.id
                WHERE e.id <> %(eid)s ORDER BY e.name LIMIT %(lim)s
            """, {"eid": seed["id"], "ns": ns, "lim": max_nodes})
            members = [r["name"] for r in c.fetchall()]
        return {"entity": seed["name"], "community": members, "size": len(members) + 1}

    def contradictions(self, schema, ns, *, limit=50) -> list[dict]:
        """POTENTIAL CONTRADICTIONS — currently-valid facts where the SAME subject+predicate
        carries MORE THAN ONE distinct object (e.g. lives_in Austin AND lives_in Seattle,
        both un-superseded). Deterministic SQL, no LLM. Non-blocking signal: multi-valued
        predicates (visited, did) legitimately appear here too — surfaces for review."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"""
                SELECT subject_entity, predicate,
                       array_agg(DISTINCT object) AS objects,
                       array_agg(id ORDER BY id) AS ids,
                       count(DISTINCT object) AS n
                FROM {schema}.semantic
                WHERE namespace=%s AND expired_at IS NULL AND valid_to IS NULL
                  AND subject_entity IS NOT NULL AND predicate IS NOT NULL AND object IS NOT NULL
                GROUP BY subject_entity, predicate
                HAVING count(DISTINCT object) > 1
                ORDER BY count(DISTINCT object) DESC LIMIT %s
            """, (ns, limit))
            return [{"subject": r["subject_entity"], "predicate": r["predicate"],
                     "objects": r["objects"], "ids": r["ids"]} for r in c.fetchall()]

    def health(self, schema, ns) -> dict:
        """KNOWLEDGE HEALTH — a 0-100 score from structural signals over one namespace:
        contradictions, orphan entities (no edges), and the superseded ratio. Pure SQL."""
        self._chk(schema)
        with self.conn.cursor() as c:
            def one(sql, *p):
                c.execute(sql, p); return c.fetchone()["n"]
            facts_current = one(f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL", ns)
            facts_super = one(f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NOT NULL", ns)
            facts_expired = one(f"SELECT count(*) n FROM {schema}.semantic WHERE namespace=%s AND expired_at IS NOT NULL", ns)
            ent_total = one(f"SELECT count(*) n FROM {schema}.entities WHERE namespace=%s", ns)
            orphans = one(f"""SELECT count(*) n FROM {schema}.entities e WHERE e.namespace=%s
                              AND NOT EXISTS (SELECT 1 FROM {schema}.edges g WHERE g.src_entity=e.id OR g.dst_entity=e.id)""", ns)
            contra = len(self.contradictions(schema, ns, limit=1000))
        orphan_ratio = (orphans / ent_total) if ent_total else 0.0
        score = 100
        score -= min(40, contra * 5)                       # contradictions hurt most
        score -= int(min(30, orphan_ratio * 30))           # disconnected entities
        score = max(0, score)
        return {"score": score, "facts_current": facts_current, "facts_superseded": facts_super,
                "facts_expired": facts_expired, "entities": ent_total, "orphan_entities": orphans,
                "contradiction_groups": contra}

    def get_semantic(self, schema, ns, semantic_id) -> dict | None:
        """Fetch a single semantic fact by id (for memory_delete confirmation)."""
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"SELECT id, statement, expired_at FROM {schema}.semantic WHERE id=%s AND namespace=%s",
                      (semantic_id, ns))
            return c.fetchone()

    def pinned_constraints(self, schema, namespaces, *, cap=10) -> list[dict]:
        """PINNED CONSTRAINT INJECTION (0.1.6): every LIVE memory typed 'constraint' in the
        given namespaces — regardless of query similarity. Covers ALL THREE stores:
        semantic facts (extraction inheritance / direct fact writes), raw turns (local
        mode has no extraction, so the verbatim typed turn IS the constraint), and
        episodic events (an episode inherits 'constraint' only when its source turns are
        UNANIMOUSLY that type — so the episode body is constraint material). Oldest-first
        (constraints are durable ground rules — earliest laid down come first), deduped on
        content, capped. Pure SQL, no embedding involved."""
        self._chk(schema)
        nss = [ns for ns in namespaces if ns]
        if not nss or cap <= 0:
            return []
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT content, kind, ts, author, namespace FROM ("
                f"  SELECT statement AS content, 'fact'::text AS kind,"
                f"         COALESCE(valid_from, created_at) AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.semantic WHERE namespace = ANY(%(nss)s)"
                f"    AND memory_type='constraint' AND valid_to IS NULL AND expired_at IS NULL"
                f"  UNION ALL"
                f"  SELECT text AS content, 'turn'::text AS kind, observed_at AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.raw_turns WHERE namespace = ANY(%(nss)s) AND memory_type='constraint'"
                f"  UNION ALL"
                f"  SELECT text AS content, 'episode'::text AS kind,"
                f"         COALESCE(t_start, observed_at) AS ts,"
                f"         author_principal AS author, namespace"
                f"  FROM {schema}.episodic WHERE namespace = ANY(%(nss)s) AND memory_type='constraint'"
                f") u ORDER BY ts, content LIMIT %(lim)s",
                {"nss": nss, "lim": cap * 3})        # over-fetch: dedupe may drop rows
            # dedupe on content (a turn + its identical extracted fact), keep oldest, cap
            rows, seen = [], set()
            for r in c.fetchall():
                if r["content"] in seen:
                    continue
                seen.add(r["content"]); rows.append(r)
                if len(rows) >= cap:
                    break
        return rows

    _CONSTRAINT_RE = re.compile(
        r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|SHALL|MUST|REQUIRED|SHOULD|PROHIBITED|FORBIDDEN)\b")

    def ingest_constraints(self, schema, ns, source, text, author=None) -> list[int]:
        """Parse normative constraints (RFC-2119 keywords) out of an architecture doc and
        store each as a kind='constraint' semantic fact tagged with the source. FTS-searchable
        immediately (embedding optional). Returns the inserted fact ids."""
        self._chk(schema)
        cands = []
        for raw in re.split(r"\n+", text or ""):
            line = raw.strip().lstrip("#-*>| ").strip()
            if not line:
                continue
            for sent in re.split(r"(?<=[.!?])\s+", line):
                sent = sent.strip()
                if len(sent) >= 8 and self._CONSTRAINT_RE.search(sent.upper()):
                    cands.append(sent[:1000])
        ids = []
        with self.conn.cursor() as c:
            # idempotent re-ingest: drop this source's prior constraints first
            c.execute(f"DELETE FROM {schema}.semantic WHERE namespace=%s AND kind='constraint' "
                      f"AND subject_entity=%s", (ns, source))
            for sent in cands:
                c.execute(
                    f"INSERT INTO {schema}.semantic(namespace,kind,statement,subject_entity,predicate,object,author_principal) "
                    f"VALUES(%s,'constraint',%s,%s,'constraint_of',%s,%s) RETURNING id",
                    (ns, sent, source, source, author))
                ids.append(c.fetchone()["id"])
        return ids

    def corpus_check(self, schema, ns, snippet, *, k=10) -> list[dict]:
        """Return the architecture constraints most relevant to a code snippet — FTS over
        the kind='constraint' facts (shared keywords, ranked). Pure SQL, no LLM."""
        self._chk(schema)
        words = list(dict.fromkeys(w.lower() for w in re.findall(r"[A-Za-z]{4,}", snippet or "")))
        if not words:
            return []
        q = " or ".join(words[:40])
        with self.conn.cursor() as c:
            c.execute(
                f"SELECT id, statement AS content, subject_entity AS source, "
                f"ts_rank(fts, websearch_to_tsquery('english',%s)) AS score "
                f"FROM {schema}.semantic "
                f"WHERE namespace=%s AND kind='constraint' AND expired_at IS NULL "
                f"AND fts @@ websearch_to_tsquery('english',%s) "
                f"ORDER BY score DESC LIMIT %s", (q, ns, q, k))
            return c.fetchall()

    def migrate_namespace(self, schema, src, dst, *, mode="copy", like=None) -> dict:
        """Copy or MOVE memories from one namespace to another (same tenant schema).
        `copy` (default) duplicates raw turns + facts (optional `like` substring filter on
        the text) and rebuilds the entity graph in the destination from the facts' SPO —
        no LLM. `move` relocates the WHOLE namespace (raw turns + facts + episodes), rebuilds
        the destination graph, and drops the now-orphaned source graph. Returns counts."""
        self._chk(schema)
        if mode not in ("copy", "move"):
            raise ValueError("mode must be 'copy' or 'move'")
        likeval = f"%{like}%" if like else None
        with self.conn.cursor() as c:
            if mode == "move":
                c.execute(f"UPDATE {schema}.raw_turns SET namespace=%s WHERE namespace=%s", (dst, src))
                n_rt = c.rowcount
                c.execute(f"UPDATE {schema}.semantic SET namespace=%s WHERE namespace=%s "
                          f"RETURNING id, subject_entity, object", (dst, src))
                moved = c.fetchall(); n_sem = len(moved)
                c.execute(f"UPDATE {schema}.episodic SET namespace=%s WHERE namespace=%s", (dst, src))
                n_epi = c.rowcount
                # drop the now-orphaned source graph (facts moved out)
                c.execute(f"DELETE FROM {schema}.mentions m USING {schema}.entities e "
                          f"WHERE m.entity_id=e.id AND e.namespace=%s", (src,))
                c.execute(f"DELETE FROM {schema}.edges WHERE namespace=%s", (src,))
                c.execute(f"DELETE FROM {schema}.entities WHERE namespace=%s", (src,))
            else:  # copy
                rt_filter = " AND text ILIKE %s" if like else ""
                c.execute(f"INSERT INTO {schema}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding) "
                          f"SELECT %s,session_id,speaker,text,observed_at,embedding FROM {schema}.raw_turns "
                          f"WHERE namespace=%s{rt_filter}",
                          ([dst, src] + ([likeval] if like else [])))
                n_rt = c.rowcount
                sem_filter = " AND statement ILIKE %s" if like else ""
                # copied facts lose source_turn_ids (raw-turn ids differ in the copy)
                c.execute(f"INSERT INTO {schema}.semantic"
                          f"(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding) "
                          f"SELECT %s,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding "
                          f"FROM {schema}.semantic WHERE namespace=%s{sem_filter} "
                          f"RETURNING id, subject_entity, object",
                          ([dst, src] + ([likeval] if like else [])))
                moved = c.fetchall(); n_sem = len(moved); n_epi = 0
        # rebuild the destination graph from the (moved/copied) facts' SPO — idempotent, no LLM
        for r in moved:
            subj = r.get("subject_entity")
            if not subj:
                continue
            se = self.upsert_entity(schema, dst, subj[:100])
            self.add_mention(schema, se, r["id"], "semantic")
            obj = r.get("object")
            if obj:
                oe = self.upsert_entity(schema, dst, obj[:100])
                self.add_mention(schema, oe, r["id"], "semantic")
                self.bump_edge(schema, dst, se, oe)
        return {"mode": mode, "src": src, "dst": dst, "raw_turns": n_rt, "facts": n_sem, "episodes": n_epi}

    def reconcile(self, schema, ns, statement, qvec=None, *, subject=None, predicate=None, k=8) -> dict:
        """Reconcile an EXTERNAL claim (e.g. from a local note the agent trusts) against
        memnos: does memnos hold a CURRENT fact about the same subject whose value is NOT
        reflected in the claim? Surfaces staleness/contradiction so the agent can tell the
        user 'your local memory is stale; memnos has a newer value (as of <date>)'.
        Deterministic — the caller supplies the parsed subject/predicate; no LLM here."""
        self._chk(schema)
        claim_l = (statement or "").lower()
        found, seen = [], set()

        def add(r, conflict):
            if r["id"] in seen:
                return
            seen.add(r["id"])
            found.append({"id": r["id"], "statement": r["statement"], "subject": r["subject_entity"],
                          "predicate": r["predicate"], "object": r["object"],
                          "valid_from": r["valid_from"], "conflict": conflict})

        with self.conn.cursor() as c:
            # SUBJECT arm — deterministic: current facts about the same subject (+predicate)
            if subject:
                pred_clause = " AND predicate ILIKE %s" if predicate else ""
                params = [ns, subject] + ([predicate] if predicate else [])
                c.execute(f"SELECT id, statement, subject_entity, predicate, object, valid_from "
                          f"FROM {schema}.semantic WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL "
                          f"AND subject_entity ILIKE %s{pred_clause} ORDER BY valid_from DESC NULLS LAST LIMIT 20",
                          params)
                for r in c.fetchall():
                    obj = (r["object"] or "").strip()
                    add(r, bool(obj) and obj.lower() not in claim_l)
            # VECTOR arm — catch paraphrases / when no subject given: near-but-different facts
            if qvec is not None:
                c.execute(f"SELECT id, statement, subject_entity, predicate, object, valid_from, "
                          f"(embedding <=> %s::{self.vtype}) AS dist FROM {schema}.semantic "
                          f"WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL AND embedding IS NOT NULL "
                          f"ORDER BY embedding <=> %s::{self.vtype} LIMIT %s", (vlit(qvec), ns, vlit(qvec), k))
                for r in c.fetchall():
                    near = r["dist"] is not None and r["dist"] < 0.45
                    if not near:
                        continue
                    obj = (r["object"] or "").strip()
                    differs = r["statement"].lower().strip() != claim_l.strip()
                    add(r, bool(obj) and obj.lower() not in claim_l and differs)
        conflicts = [f for f in found if f["conflict"]]
        return {"claim": statement, "matches": found, "conflicts": conflicts, "stale": bool(conflicts)}

    def counts(self, schema) -> dict:
        self._chk(schema)
        out = {}
        with self.conn.cursor() as c:
            for t in ("raw_turns", "episodic", "semantic", "entities", "mentions", "edges", "provenance"):
                c.execute(f"SELECT count(*) AS n FROM {schema}.{t}")
                out[t] = c.fetchone()["n"]
        return out
