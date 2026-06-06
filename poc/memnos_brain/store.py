"""B1 — storage backend for the brain-inspired schema.

One ACID Postgres engine. Writes raw_turns (verbatim), episodic events, entities/
mentions/edges (associative graph), and (for B2) semantic facts + provenance.
Schema identifiers are validated; values are parameterized.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

_IDENT = re.compile(r"^[a-z0-9_]+$")


def vlit(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class BrainStore:
    def __init__(self, dsn: str | None = None, conn=None):
        # Accept a pooled connection (production) or open one from a DSN (scripts/POC).
        if conn is not None:
            self.conn = conn
            self._owns = False
        else:
            self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            self._owns = True

    def _chk(self, s: str) -> None:
        if not _IDENT.match(s):
            raise ValueError(f"unsafe schema identifier: {s!r}")

    # --- provisioning -----------------------------------------------------
    def create_schema(self, tenant: str, dim: int = 1536) -> str:
        with self.conn.cursor() as c:
            c.execute("SELECT create_brain_schema(%s, %s)", (tenant, dim))
        return f"tenant_{tenant}"

    def drop_schema(self, tenant: str) -> None:
        s = f"tenant_{tenant}"; self._chk(s)
        with self.conn.cursor() as c:
            c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")

    # --- sensory / verbatim ----------------------------------------------
    def insert_raw_turn(self, schema, ns, session_id, speaker, text, observed_at, vec) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding) "
                f"VALUES(%s,%s,%s,%s,%s,%s::halfvec) RETURNING id",
                (ns, session_id, speaker, text, observed_at, vlit(vec)))
            return c.fetchone()["id"]

    # --- episodic ---------------------------------------------------------
    def insert_episodic(self, schema, ns, session_id, text, *, summary=None,
                        t_start=None, t_end=None, observed_at=None, salience=0.0,
                        source_turn_ids: Iterable[int] = (), vec=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.episodic"
                f"(namespace,session_id,text,summary,t_start,t_end,observed_at,salience,source_turn_ids,embedding) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::halfvec) RETURNING id",
                (ns, session_id, text, summary, t_start, t_end, observed_at, salience,
                 list(source_turn_ids), vlit(vec)))
            return c.fetchone()["id"]

    # --- associative graph ------------------------------------------------
    def upsert_entity(self, schema, ns, name, vec=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.entities(namespace,name,embedding) "
                f"VALUES(%s,%s,%s::halfvec) ON CONFLICT (namespace,name) DO UPDATE SET name=EXCLUDED.name "
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
                        salience=0.0, vec=None) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.semantic"
                f"(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,confidence,salience,embedding) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::halfvec) RETURNING id",
                (ns, kind, statement, subject, predicate, obj, valid_from, valid_to,
                 confidence, salience, vlit(vec) if vec is not None else None))
            return c.fetchone()["id"]

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
                f"AND (embedding <=> %s::halfvec) < %s RETURNING id",
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
                f"AND (embedding <=> %(v)s::halfvec) BETWEEN %(lo)s AND %(hi)s RETURNING id",
                {"vf": valid_from, "ns": ns, "sub": subject, "v": vlit(new_vec),
                 "lo": dist_lo, "hi": dist_hi})
            return len(c.fetchall())

    def supersede_predicate(self, schema, ns, subject, predicate, obj, valid_from) -> int:
        """ROBUST belief-change supersession: when a new (subject, predicate, object)
        fact arrives, close out the currently-valid facts with the SAME subject+predicate
        but a DIFFERENT object (e.g. lives-in Austin → lives-in Seattle). Sets valid_to;
        never deletes. This is what makes 'what is X's CURRENT y?' trustworthy. Returns #."""
        self._chk(schema)
        if not subject or not predicate:
            return 0
        with self.conn.cursor() as c:
            c.execute(
                f"UPDATE {schema}.semantic SET valid_to=%s "
                f"WHERE namespace=%s AND valid_to IS NULL AND expired_at IS NULL "
                f"AND lower(subject_entity)=lower(%s) AND lower(predicate)=lower(%s) "
                f"AND lower(coalesce(object,'')) <> lower(coalesce(%s,'')) "
                f"AND (valid_from IS NULL OR valid_from <= %s) RETURNING id",
                (valid_from, ns, subject, predicate, obj, valid_from))
            return len(c.fetchall())

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
        WITH vec AS (SELECT id, text, observed_at, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                     FROM {schema}.episodic WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.episodic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,rnk FROM vec UNION ALL SELECT id,text,observed_at,rnk FROM fts) r
                  GROUP BY id,text,observed_at)
        SELECT id, text AS content, observed_at, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qtext, "ns": ns, "k": k})
            return c.fetchall()

    def search_raw_turns(self, schema, ns, qvec, qtext, k=40) -> list[dict]:
        """Hybrid RRF (vector+FTS) over RAW TURNS — the strong open/single-hop layer."""
        self._chk(schema)
        sql = f"""
        WITH vec AS (SELECT id, text, observed_at, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                     FROM {schema}.raw_turns WHERE namespace=%(ns)s ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
        fts AS (SELECT id, text, observed_at, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.raw_turns, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, text, observed_at, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,text,observed_at,rnk FROM vec UNION ALL SELECT id,text,observed_at,rnk FROM fts) r
                  GROUP BY id,text,observed_at)
        SELECT id, text AS content, observed_at, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qtext, "ns": ns, "k": k})
            return c.fetchall()

    def search_semantic(self, schema, ns, qvec, qtext, k=40, current_only=False) -> list[dict]:
        """Hybrid RRF (vector+FTS) over SEMANTIC; current_only filters superseded facts."""
        self._chk(schema)
        valid = "AND valid_to IS NULL" if current_only else ""
        sql = f"""
        WITH vec AS (SELECT id, statement, valid_from, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {valid} ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {valid} AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,rnk FROM vec UNION ALL SELECT id,statement,valid_from,rnk FROM fts) r
                  GROUP BY id,statement,valid_from)
        SELECT id, statement AS content, valid_from, score FROM fused ORDER BY score DESC LIMIT %(k)s;"""
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": vlit(qvec), "qt": qtext, "ns": ns, "k": k})
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
        WITH vec AS (SELECT id, statement, valid_from, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) rnk
                     FROM {schema}.semantic WHERE namespace=%(ns)s AND expired_at IS NULL {cur} ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
        fts AS (SELECT id, statement, valid_from, row_number() OVER (ORDER BY ts_rank(fts,q) DESC) rnk
                FROM {schema}.semantic, websearch_to_tsquery('english',%(qt)s) q
                WHERE namespace=%(ns)s AND expired_at IS NULL {cur} AND fts @@ q ORDER BY ts_rank(fts,q) DESC LIMIT %(k)s),
        fused AS (SELECT id, statement, valid_from, SUM(1.0/(60+rnk)) score
                  FROM (SELECT id,statement,valid_from,rnk FROM vec UNION ALL SELECT id,statement,valid_from,rnk FROM fts) r
                  GROUP BY id,statement,valid_from)
        SELECT id, statement AS content, valid_from FROM fused ORDER BY score DESC LIMIT %(k)s"""
        params = {"qv": vlit(qvec), "qt": qtext, "ns": ns, "k": k}
        rows, seen = [], set()
        with self.conn.cursor() as c:
            c.execute(base, params)
            for r in c.fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"]); rows.append(r)
            # event-time window guarantee
            if start and end:
                c.execute(f"SELECT id, statement AS content, valid_from FROM {schema}.semantic "
                          f"WHERE namespace=%s AND expired_at IS NULL AND valid_from >= %s AND valid_from < %s "
                          f"ORDER BY valid_from LIMIT %s", (ns, start, end, k))
                for r in c.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"]); rows.append(r)
            # first/last boundary facts
            if order in ("asc", "desc"):
                c.execute(f"SELECT id, statement AS content, valid_from FROM {schema}.semantic "
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
            c.execute(f"SELECT id, statement AS content, valid_from FROM {schema}.semantic "
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

    def counts(self, schema) -> dict:
        self._chk(schema)
        out = {}
        with self.conn.cursor() as c:
            for t in ("raw_turns", "episodic", "semantic", "entities", "mentions", "edges", "provenance"):
                c.execute(f"SELECT count(*) AS n FROM {schema}.{t}")
                out[t] = c.fetchone()["n"]
        return out
