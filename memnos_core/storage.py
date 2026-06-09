"""POC StorageBackend interface + Postgres implementation.

Hybrid retrieval (vector + full-text + 1-hop graph) is fused with Reciprocal
Rank Fusion **inside Postgres** — one query, one round-trip. That co-location
(compute next to data) is what keeps the hot path under the latency budget,
and it's the whole reason a single ACID engine works here.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

_IDENT = re.compile(r"^[a-z0-9_]+$")


def _vlit(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class StorageBackend(ABC):
    """The seam. A native-graph or alternate vector backend could implement this
    later without touching the rest of memnos."""

    @abstractmethod
    def hybrid_search(self, schema, namespace, query_text, query_vec, k=20, top_k=10): ...


class PgStorage(StorageBackend):
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def _chk(self, s: str):
        if not _IDENT.match(s):
            raise ValueError(f"unsafe schema identifier: {s!r}")

    # --- provisioning / writes --------------------------------------------
    def create_tenant(self, tenant: str):
        self._chk(f"tenant_{tenant}")
        with self.conn.cursor() as c:
            c.execute("SELECT create_tenant_schema(%s)", (tenant,))

    def insert_entity(self, schema, namespace, name, etype="CONCEPT") -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.entity(namespace,name,entity_type) VALUES(%s,%s,%s) "
                f"ON CONFLICT (namespace,name) DO UPDATE SET entity_type=EXCLUDED.entity_type RETURNING id",
                (namespace, name, etype),
            )
            return c.fetchone()["id"]

    def insert_memory(self, schema, namespace, content, vec, entity_ids: Iterable[int] = ()) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.memory(namespace,content,embedding) "
                f"VALUES(%s,%s,%s::halfvec) RETURNING id",
                (namespace, content, _vlit(vec)),
            )
            mid = c.fetchone()["id"]
            for eid in entity_ids:
                c.execute(
                    f"INSERT INTO {schema}.mentions(memory_id,entity_id) VALUES(%s,%s) "
                    f"ON CONFLICT DO NOTHING",
                    (mid, eid),
                )
            return mid

    def insert_episode(self, schema, namespace, role, content) -> int:
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"INSERT INTO {schema}.episode(namespace,role,content) VALUES(%s,%s,%s) RETURNING id",
                      (namespace, role, content))
            return c.fetchone()["id"]

    def insert_fact(self, schema, namespace, subject, predicate, obj, valid_at=None, source_episode_id=None):
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(f"INSERT INTO {schema}.fact(namespace,subject,predicate,object,valid_at,source_episode_id) "
                      f"VALUES(%s,%s,%s,%s,%s,%s)",
                      (namespace, subject, predicate, obj, valid_at, source_episode_id))

    def insert_relation(self, schema, namespace, src, dst, rel_type):
        self._chk(schema)
        with self.conn.cursor() as c:
            c.execute(
                f"INSERT INTO {schema}.relation(namespace,src_entity,dst_entity,rel_type) "
                f"VALUES(%s,%s,%s,%s)",
                (namespace, src, dst, rel_type),
            )

    # --- the hot path: hybrid RRF in ONE query ----------------------------
    def hybrid_search(self, schema, namespace, query_text, query_vec, k=20, top_k=10):
        self._chk(schema)
        sql = f"""
        WITH vec AS (
          SELECT id, row_number() OVER (ORDER BY embedding <=> %(qv)s::halfvec) AS rnk
          FROM {schema}.memory
          WHERE namespace = %(ns)s AND superseded_at IS NULL
          ORDER BY embedding <=> %(qv)s::halfvec LIMIT %(k)s),
        fts AS (
          SELECT id, row_number() OVER (ORDER BY ts_rank(fts, q) DESC) AS rnk
          FROM {schema}.memory, websearch_to_tsquery('english', %(qt)s) q
          WHERE namespace = %(ns)s AND superseded_at IS NULL AND fts @@ q
          ORDER BY ts_rank(fts, q) DESC LIMIT %(k)s),
        seed AS (SELECT id FROM vec UNION SELECT id FROM fts),
        graph AS (
          SELECT m2.memory_id AS id, row_number() OVER () AS rnk
          FROM {schema}.mentions m1
          JOIN {schema}.mentions m2
            ON m1.entity_id = m2.entity_id AND m2.memory_id <> m1.memory_id
          WHERE m1.memory_id IN (SELECT id FROM seed) LIMIT %(k)s),
        fused AS (
          SELECT id, SUM(1.0/(60+rnk)) AS score FROM (
            SELECT id, rnk FROM vec
            UNION ALL SELECT id, rnk FROM fts
            UNION ALL SELECT id, rnk FROM graph
          ) r GROUP BY id)
        SELECT f.id, m.content, round(f.score::numeric, 5) AS score,
               EXISTS(SELECT 1 FROM vec   WHERE vec.id   = f.id) AS in_vec,
               EXISTS(SELECT 1 FROM fts   WHERE fts.id   = f.id) AS in_fts,
               EXISTS(SELECT 1 FROM graph WHERE graph.id = f.id) AS in_graph
        FROM fused f JOIN {schema}.memory m ON m.id = f.id
        ORDER BY f.score DESC LIMIT %(tk)s;
        """
        with self.conn.cursor() as c:
            c.execute(sql, {"qv": _vlit(query_vec), "qt": query_text,
                            "ns": namespace, "k": k, "tk": top_k})
            return c.fetchall()
