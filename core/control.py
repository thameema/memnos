"""Production control plane: identity, token auth, namespace ACL, audit, usage ledger.

Server-side identity (never client-trusted): a Bearer token resolves to a principal,
whose namespace GRANTS clamp every read/write. Tokens are stored as SHA-256 hashes
(high-entropy random tokens — no bcrypt needed). Every operation is audited; every
LLM op is metered. All in the same Postgres (one ACID engine = the governance moat).
"""
from __future__ import annotations

import hashlib
import json
import secrets

CONTROL_DDL = """
CREATE SCHEMA IF NOT EXISTS memnos_control;
CREATE TABLE IF NOT EXISTS memnos_control.principals(
    id bigserial PRIMARY KEY, name text UNIQUE NOT NULL, kind text NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS memnos_control.api_tokens(
    id bigserial PRIMARY KEY, principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    token_hash text UNIQUE NOT NULL, label text, created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz, revoked boolean NOT NULL DEFAULT false);
ALTER TABLE memnos_control.api_tokens ADD COLUMN IF NOT EXISTS hint text;  -- 'mnk_XXXX…YYYY' (non-secret) so tokens are identifiable
CREATE INDEX IF NOT EXISTS tok_hash ON memnos_control.api_tokens(token_hash);
CREATE TABLE IF NOT EXISTS memnos_control.grants(
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    namespace text NOT NULL, can_read boolean NOT NULL DEFAULT true, can_write boolean NOT NULL DEFAULT true,
    UNIQUE(principal_id, namespace));
CREATE TABLE IF NOT EXISTS memnos_control.audit_log(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), principal_id bigint,
    action text NOT NULL, namespace text, ok boolean NOT NULL, detail jsonb);
ALTER TABLE memnos_control.audit_log ADD COLUMN IF NOT EXISTS latency_ms int;
ALTER TABLE memnos_control.audit_log ADD COLUMN IF NOT EXISTS result_count int;
ALTER TABLE memnos_control.audit_log ADD COLUMN IF NOT EXISTS status int;
CREATE INDEX IF NOT EXISTS audit_ts ON memnos_control.audit_log(ts);
CREATE TABLE IF NOT EXISTS memnos_control.usage_ledger(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), principal_id bigint,
    namespace text, op text, model text, tokens_in int DEFAULT 0, tokens_out int DEFAULT 0,
    cost_usd numeric(12,6) DEFAULT 0);
CREATE INDEX IF NOT EXISTS usage_ts ON memnos_control.usage_ledger(ts);
-- quality canary: track recall accuracy over time (does it stay good as data grows?)
CREATE TABLE IF NOT EXISTS memnos_control.eval_runs(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), eval text NOT NULL,
    metric text NOT NULL, value numeric, n int, detail jsonb);
CREATE INDEX IF NOT EXISTS eval_ts ON memnos_control.eval_runs(ts);
-- user/agent feedback: was a recalled memory actually helpful? (the true quality signal)
CREATE TABLE IF NOT EXISTS memnos_control.feedback(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), principal_id bigint,
    namespace text, query text, helpful boolean, note text);
-- namespace registry: namespaces are explicit, user-created objects (via the UI/CLI),
-- not implicit-on-write. Lets the console list/create/delete them.
CREATE TABLE IF NOT EXISTS memnos_control.namespaces(
    name text PRIMARY KEY, created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(), description text);
-- namespace KIND (0.1.6): 'memory' (default, conversational) or 'knowledge' (curated
-- reference corpus meant to GROUND other namespaces' recall via namespace_links).
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'memory';
-- GROUNDED RECALL links (0.1.6): recall on src_ns ALSO searches dst_ns — but only if the
-- CALLING principal holds a read grant on dst_ns (link = policy, grant = permission;
-- BOTH required). Skipped links are surfaced in the /recall response (links_skipped).
CREATE TABLE IF NOT EXISTS memnos_control.namespace_links(
    src_ns text NOT NULL, dst_ns text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(src_ns, dst_ns));
-- encrypted secret vault: AES-256-GCM ciphertext only (plaintext NEVER stored).
-- Referenced as value-refs (secret://name); resolved at use-time, never logged.
CREATE TABLE IF NOT EXISTS memnos_control.secrets(
    name text PRIMARY KEY, nonce bytea NOT NULL, ciphertext bytea NOT NULL, description text,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
-- namespace pub/sub: cursor-based feed (poll) + optional webhook. cursor = last raw_turn
-- id delivered for the namespace; subscribe starts at the current max so feed yields only
-- NEW memories. Replaces the old Redis/ArcadeDB subscription records on one PG engine.
CREATE TABLE IF NOT EXISTS memnos_control.subscriptions(
    id bigserial PRIMARY KEY,
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    namespace text NOT NULL,
    webhook text,
    cursor bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_polled_at timestamptz);
CREATE INDEX IF NOT EXISTS subs_ns ON memnos_control.subscriptions(namespace);
ALTER TABLE memnos_control.subscriptions ADD COLUMN IF NOT EXISTS delivery_failures int NOT NULL DEFAULT 0;
ALTER TABLE memnos_control.subscriptions ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE memnos_control.subscriptions ADD COLUMN IF NOT EXISTS last_delivery_at timestamptz;
-- corpus registry: tracks ingested architecture docs (LLD/HLD/ADR). Their extracted
-- normative constraints (SHALL/MUST/...) live as kind='constraint' semantic facts so
-- corpus_check can hybrid-search them against a code snippet.
CREATE TABLE IF NOT EXISTS memnos_control.corpus_sources(
    id bigserial PRIMARY KEY, namespace text NOT NULL, name text NOT NULL,
    kind text, git_sha text, constraint_count int NOT NULL DEFAULT 0,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(namespace, name));
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Control:
    """Operates over a pooled connection (passed per call) so it's stateless + poolable."""

    @staticmethod
    def init(conn):
        with conn.cursor() as c:
            c.execute(CONTROL_DDL)

    # --- namespace pub/sub (cursor feed + optional webhook) -----------------
    @staticmethod
    def subscribe(conn, principal_id, namespace, webhook=None):
        """Create a subscription. Cursor starts at the namespace's current max raw_turn id,
        so the feed delivers only memories written AFTER subscribing."""
        with conn.cursor() as c:
            c.execute("SELECT COALESCE(max(id),0) AS m FROM tenant_memnos.raw_turns WHERE namespace=%s",
                      (namespace,))
            cur = c.fetchone()["m"]
            c.execute("INSERT INTO memnos_control.subscriptions(principal_id,namespace,webhook,cursor) "
                      "VALUES(%s,%s,%s,%s) RETURNING id", (principal_id, namespace, webhook, cur))
            sid = c.fetchone()["id"]
        return {"subscription_id": sid, "namespace": namespace, "cursor": cur, "webhook": webhook}

    @staticmethod
    def feed(conn, principal_id, subscription_id, namespace, *, limit=50):
        """Poll new memories since the subscription cursor; advance the cursor. Returns
        None if the subscription doesn't exist or isn't owned by this principal/namespace."""
        with conn.cursor() as c:
            c.execute("SELECT id, principal_id, namespace, cursor FROM memnos_control.subscriptions WHERE id=%s",
                      (subscription_id,))
            sub = c.fetchone()
            if not sub or sub["principal_id"] != principal_id or sub["namespace"] != namespace:
                return None
            c.execute("SELECT id, speaker, text AS content, observed_at FROM tenant_memnos.raw_turns "
                      "WHERE namespace=%s AND id > %s ORDER BY id LIMIT %s",
                      (namespace, sub["cursor"], limit))
            items = c.fetchall()
            newcur = items[-1]["id"] if items else sub["cursor"]
            c.execute("UPDATE memnos_control.subscriptions SET cursor=%s, last_polled_at=now() WHERE id=%s",
                      (newcur, subscription_id))
        return {"subscription_id": subscription_id, "items": items, "cursor": newcur}

    # --- corpus registry ----------------------------------------------------
    @staticmethod
    def corpus_record(conn, namespace, name, kind, git_sha, count):
        """Upsert a corpus source row (re-ingesting the same name updates count + sha)."""
        with conn.cursor() as c:
            c.execute("""INSERT INTO memnos_control.corpus_sources(namespace,name,kind,git_sha,constraint_count)
                         VALUES(%s,%s,%s,%s,%s)
                         ON CONFLICT (namespace,name) DO UPDATE
                         SET kind=EXCLUDED.kind, git_sha=EXCLUDED.git_sha,
                             constraint_count=EXCLUDED.constraint_count, ingested_at=now()
                         RETURNING id""", (namespace, name, kind, git_sha, count))
            return c.fetchone()["id"]

    @staticmethod
    def corpus_list(conn, namespace):
        with conn.cursor() as c:
            c.execute("SELECT id, name, kind, git_sha, constraint_count, ingested_at "
                      "FROM memnos_control.corpus_sources WHERE namespace=%s ORDER BY name", (namespace,))
            return c.fetchall()

    @staticmethod
    def deliver_pending(conn, post_fn, *, batch=50, max_failures=5, conn_factory=None):
        """WEBHOOK PUSH — for each active webhook subscription, POST the memories written
        since its cursor, then advance the cursor (at-least-once). `post_fn(url, payload)`
        must raise on non-2xx; injected so tests use a fake and prod uses real HTTP. On
        failure the cursor is NOT advanced (retried next tick) and a failure counter
        increments; after `max_failures` the subscription is deactivated. Returns per-sub
        results. Idempotent to run from both the background pusher and the admin endpoint.

        `conn_factory` (pooled callers, e.g. POOL.connection): each DB step uses its own
        short-lived connection so NO pool slot is held across the webhook POSTs (network,
        up to several seconds each) — the held-conn-across-network anti-pattern. With
        conn_factory set, `conn` may be None."""
        import contextlib
        cf = conn_factory if conn_factory is not None else (lambda: contextlib.nullcontext(conn))
        with cf() as cx, cx.cursor() as c:
            c.execute("SELECT id, namespace, webhook, cursor, delivery_failures "
                      "FROM memnos_control.subscriptions "
                      "WHERE webhook IS NOT NULL AND active=true ORDER BY id")
            subs = c.fetchall()
        results = []
        for sub in subs:
            with cf() as cx, cx.cursor() as c:
                c.execute("SELECT id, speaker, text AS content, observed_at "
                          "FROM tenant_memnos.raw_turns WHERE namespace=%s AND id > %s "
                          "ORDER BY id LIMIT %s", (sub["namespace"], sub["cursor"], batch))
                items = c.fetchall()
            if not items:
                continue
            payload = {"subscription_id": sub["id"], "namespace": sub["namespace"],
                       "events": [{"id": i["id"], "speaker": i["speaker"], "content": i["content"],
                                   "observed_at": i["observed_at"].isoformat() if i["observed_at"] else None}
                                  for i in items]}
            try:
                post_fn(sub["webhook"], payload)              # network — NO conn held when pooled
                newcur = items[-1]["id"]
                with cf() as cx, cx.cursor() as c:
                    c.execute("UPDATE memnos_control.subscriptions "
                              "SET cursor=%s, delivery_failures=0, last_delivery_at=now() WHERE id=%s",
                              (newcur, sub["id"]))
                results.append({"subscription_id": sub["id"], "delivered": len(items), "cursor": newcur})
            except Exception as e:
                fails = sub["delivery_failures"] + 1
                still = fails < max_failures
                with cf() as cx, cx.cursor() as c:
                    c.execute("UPDATE memnos_control.subscriptions SET delivery_failures=%s, active=%s WHERE id=%s",
                              (fails, still, sub["id"]))
                results.append({"subscription_id": sub["id"], "error": str(e)[:200],
                                "failures": fails, "active": still})
        return results

    @staticmethod
    def list_subscriptions(conn, principal_id):
        with conn.cursor() as c:
            c.execute("SELECT id, namespace, webhook, cursor, active, delivery_failures, "
                      "created_at, last_polled_at, last_delivery_at "
                      "FROM memnos_control.subscriptions WHERE principal_id=%s ORDER BY id", (principal_id,))
            return c.fetchall()

    @staticmethod
    def unsubscribe(conn, principal_id, subscription_id) -> bool:
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.subscriptions WHERE id=%s AND principal_id=%s",
                      (subscription_id, principal_id))
            return c.rowcount > 0

    # --- identity / tokens (admin) ----------------------------------------
    @staticmethod
    def create_principal(conn, name, kind="user") -> int:
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.principals(name,kind) VALUES(%s,%s) "
                      "ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind RETURNING id", (name, kind))
            return c.fetchone()["id"]

    @staticmethod
    def mint_token(conn, principal_id, label=None, ttl_days=None) -> str:
        token = "mnk_" + secrets.token_urlsafe(32)
        hint = token[:8] + "…" + token[-4:]   # non-secret: 8 of 43 random chars → identifiable, not brute-forceable
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.api_tokens(principal_id,token_hash,label,hint,expires_at) "
                      "VALUES(%s,%s,%s,%s, CASE WHEN %s::int IS NULL THEN NULL "
                      "ELSE now()+(%s::int||' days')::interval END)",
                      (principal_id, _hash(token), label, hint, ttl_days, ttl_days))
        return token   # plaintext returned ONCE; only the hash is stored

    @staticmethod
    def grant(conn, principal_id, namespace, can_read=True, can_write=True):
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.grants(principal_id,namespace,can_read,can_write) "
                      "VALUES(%s,%s,%s,%s) ON CONFLICT (principal_id,namespace) "
                      "DO UPDATE SET can_read=EXCLUDED.can_read, can_write=EXCLUDED.can_write",
                      (principal_id, namespace, can_read, can_write))

    @staticmethod
    def revoke_token(conn, token):
        with conn.cursor() as c:
            c.execute("UPDATE memnos_control.api_tokens SET revoked=true WHERE token_hash=%s", (_hash(token),))

    # --- auth + ACL (hot path) --------------------------------------------
    @staticmethod
    def authenticate(conn, token):
        """Bearer token -> principal_id (or None). Checks revoked + expiry."""
        if not token:
            return None
        with conn.cursor() as c:
            c.execute("SELECT principal_id FROM memnos_control.api_tokens "
                      "WHERE token_hash=%s AND NOT revoked AND (expires_at IS NULL OR expires_at>now())",
                      (_hash(token),))
            r = c.fetchone()
            return r["principal_id"] if r else None

    @staticmethod
    def principal_info(conn, principal_id):
        """Name + kind of a principal — the server stamps `author_principal` on every
        write from this (token-derived) identity, never from the request body."""
        if principal_id is None:
            return None
        with conn.cursor() as c:
            c.execute("SELECT name, kind FROM memnos_control.principals WHERE id=%s", (principal_id,))
            return c.fetchone()

    @staticmethod
    def authorize(conn, principal_id, namespace, write=False) -> bool:
        """A grant on the exact namespace, a parent prefix ('team:eng:*'), or '*' (admin)."""
        col = "can_write" if write else "can_read"
        with conn.cursor() as c:
            c.execute(f"SELECT namespace, {col} AS ok FROM memnos_control.grants WHERE principal_id=%s",
                      (principal_id,))
            for g in c.fetchall():
                gns = g["namespace"]
                if not g["ok"]:
                    continue
                if gns == "*" or gns == namespace:
                    return True
                if gns.endswith(":*") and namespace.startswith(gns[:-1]):
                    return True
        return False

    @staticmethod
    def authorized_namespaces(conn, principal_id):
        with conn.cursor() as c:
            c.execute("SELECT namespace, can_read, can_write FROM memnos_control.grants WHERE principal_id=%s",
                      (principal_id,))
            return c.fetchall()

    @staticmethod
    def readable_namespaces(conn, principal_id):
        """The CONCRETE namespaces (that actually hold memory) this principal may READ —
        expanding its grants (exact, prefix `p:*`, or `*`) against the existing namespaces.
        Used to WIDEN recall across all of an agent's permissible namespaces."""
        grants = [g for g in Control.authorized_namespaces(conn, principal_id) if g["can_read"]]
        if not grants:
            return []
        patterns = [g["namespace"] for g in grants]
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT namespace FROM tenant_memnos.raw_turns "
                      "UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic")
            existing = [r["namespace"] for r in c.fetchall()]

        def covered(ns):
            for p in patterns:
                if p == "*" or p == ns:
                    return True
                if p.endswith(":*") and ns.startswith(p[:-1]):   # 'zudioz:*' covers 'zudioz:tap'
                    return True
            return False

        return sorted(ns for ns in existing if covered(ns))

    @staticmethod
    def is_admin(conn, principal_id) -> bool:
        """Admin = holds the '*' grant. Gates the management console endpoints."""
        if principal_id is None:
            return False
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM memnos_control.grants WHERE principal_id=%s AND namespace='*' "
                      "AND can_read LIMIT 1", (principal_id,))
            return c.fetchone() is not None

    # --- namespace registry (explicit, user-created) ----------------------
    @staticmethod
    def create_namespace(conn, name, created_by=None, description=None):
        """Register a namespace + grant its creator read/write. Idempotent on name."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespaces(name,created_by,description) "
                      "VALUES(%s,%s,%s) ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description",
                      (name, created_by, description))
        if created_by is not None:
            Control.grant(conn, created_by, name)

    @staticmethod
    def set_namespace_kind(conn, name, kind):
        """Set a namespace's kind: 'memory' (default) or 'knowledge' (a curated corpus
        that grounds other namespaces via links). Registers the namespace if needed."""
        if kind not in ("memory", "knowledge"):
            raise ValueError("kind must be 'memory' or 'knowledge'")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespaces(name, kind) VALUES(%s,%s) "
                      "ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind", (name, kind))

    @staticmethod
    def link_namespaces(conn, src, dst, created_by=None):
        """Declare that recall on `src` should also be GROUNDED in `dst` (policy only —
        each caller still needs a read grant on `dst` for the fan-out to happen)."""
        if src == dst:
            raise ValueError("src and dst must differ")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespace_links(src_ns,dst_ns,created_by) "
                      "VALUES(%s,%s,%s) ON CONFLICT (src_ns,dst_ns) DO NOTHING", (src, dst, created_by))

    @staticmethod
    def unlink_namespaces(conn, src, dst) -> bool:
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s AND dst_ns=%s",
                      (src, dst))
            return c.rowcount > 0

    @staticmethod
    def linked_namespaces(conn, src):
        """The dst namespaces linked FROM `src` (recall fan-out targets), stable order."""
        with conn.cursor() as c:
            c.execute("SELECT dst_ns FROM memnos_control.namespace_links WHERE src_ns=%s "
                      "ORDER BY dst_ns", (src,))
            return [r["dst_ns"] for r in c.fetchall()]

    @staticmethod
    def list_links(conn, src=None):
        with conn.cursor() as c:
            if src:
                c.execute("SELECT l.src_ns, l.dst_ns, l.created_at, p.name AS created_by "
                          "FROM memnos_control.namespace_links l "
                          "LEFT JOIN memnos_control.principals p ON p.id=l.created_by "
                          "WHERE l.src_ns=%s ORDER BY l.dst_ns", (src,))
            else:
                c.execute("SELECT l.src_ns, l.dst_ns, l.created_at, p.name AS created_by "
                          "FROM memnos_control.namespace_links l "
                          "LEFT JOIN memnos_control.principals p ON p.id=l.created_by "
                          "ORDER BY l.src_ns, l.dst_ns")
            return c.fetchall()

    @staticmethod
    def list_namespaces(conn):
        """ALL real namespaces: the explicit registry UNION any namespace that has data
        (raw_turns/semantic) or a concrete grant — so implicitly-created namespaces (e.g.
        from hooks) still show. `registered` flags whether it's in the registry."""
        with conn.cursor() as c:
            c.execute("""
                WITH names AS (
                    SELECT name FROM memnos_control.namespaces
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.raw_turns
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic
                    UNION SELECT DISTINCT namespace FROM memnos_control.grants
                      WHERE namespace <> '*' AND namespace NOT LIKE '%*'
                )
                SELECT nm.name, n.description, n.created_at, p.name AS created_by,
                  COALESCE(n.kind, 'memory') AS kind,
                  COALESCE(rt.cnt,0) AS turns, COALESCE(sm.cnt,0) AS facts,
                  (n.name IS NOT NULL) AS registered
                FROM names nm
                LEFT JOIN memnos_control.namespaces n ON n.name=nm.name
                LEFT JOIN memnos_control.principals p ON p.id=n.created_by
                LEFT JOIN (SELECT namespace, count(*) cnt FROM tenant_memnos.raw_turns GROUP BY namespace) rt
                  ON rt.namespace=nm.name
                LEFT JOIN (SELECT namespace, count(*) cnt FROM tenant_memnos.semantic GROUP BY namespace) sm
                  ON sm.namespace=nm.name
                ORDER BY turns DESC, nm.name""")
            return c.fetchall()

    @staticmethod
    def delete_namespace(conn, name, purge_data=False):
        """Remove registry row + all grants on it; optionally purge its memory rows."""
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (name,))
            c.execute("DELETE FROM memnos_control.grants WHERE namespace=%s", (name,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s",
                      (name, name))
            if purge_data:
                c.execute("DELETE FROM tenant_memnos.mentions m USING tenant_memnos.entities e "
                          "WHERE m.entity_id=e.id AND e.namespace=%s", (name,))
                for t in ("edges", "entities", "semantic", "episodic", "raw_turns"):
                    c.execute(f"DELETE FROM tenant_memnos.{t} WHERE namespace=%s", (name,))

    # --- listings for the console -----------------------------------------
    @staticmethod
    def list_principals(conn):
        with conn.cursor() as c:
            c.execute("""SELECT p.id, p.name, p.kind, p.created_at,
                  count(t.id) FILTER (WHERE NOT t.revoked) AS active_tokens
                FROM memnos_control.principals p
                LEFT JOIN memnos_control.api_tokens t ON t.principal_id=p.id
                GROUP BY p.id ORDER BY p.created_at""")
            return c.fetchall()

    @staticmethod
    def list_tokens(conn, principal_id):
        """Token METADATA only — the secret is never retrievable (only its hash is stored)."""
        with conn.cursor() as c:
            c.execute("SELECT id, label, hint, created_at, expires_at, revoked FROM memnos_control.api_tokens "
                      "WHERE principal_id=%s ORDER BY created_at DESC LIMIT 500", (principal_id,))
            return c.fetchall()

    @staticmethod
    def revoke_token_by_id(conn, token_id):
        with conn.cursor() as c:
            c.execute("UPDATE memnos_control.api_tokens SET revoked=true WHERE id=%s", (token_id,))

    @staticmethod
    def revoke_grant(conn, principal_id, namespace):
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s AND namespace=%s",
                      (principal_id, namespace))

    @staticmethod
    def memory_feed(conn, limit=50, offset=0, namespace=None, memory_type=None):
        """ADMIN MEMORY FEED (0.1.6): the most recent memories (verbatim raw turns) across
        ALL namespaces, newest first, paginated — the console's live view of what the
        platform is remembering. Optional namespace / type filters. Admin-only at the
        endpoint (this is a cross-namespace read, so it must sit behind the '*' grant)."""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        where, params = [], []
        if namespace:
            where.append("namespace=%s"); params.append(namespace)
        if memory_type:
            where.append("memory_type=%s"); params.append(memory_type)
        cond = ("WHERE " + " AND ".join(where)) if where else ""
        with conn.cursor() as c:
            c.execute(f"SELECT id, namespace, speaker, text AS content, memory_type AS type, "
                      f"author_principal AS author, observed_at "
                      f"FROM tenant_memnos.raw_turns {cond} "
                      f"ORDER BY id DESC LIMIT %s OFFSET %s", (*params, limit, offset))
            return c.fetchall()

    @staticmethod
    def recent_audit(conn, limit=50, offset=0):
        """Paginated audit page (newest first). limit clamped to 1..1000 and offset >= 0
        so a console (or a bad client) can never pull the whole log in one response."""
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        with conn.cursor() as c:
            c.execute("SELECT ts, principal_id, action, namespace, ok, status, latency_ms, result_count "
                      "FROM memnos_control.audit_log ORDER BY ts DESC LIMIT %s OFFSET %s",
                      (limit, offset))
            return c.fetchall()

    @staticmethod
    def audit_total(conn) -> int:
        """APPROXIMATE audit row count from planner stats (pg_class.reltuples) — exact
        counts seq-scan a table that grows unbounded. Falls back to exact count only
        when the table has never been analyzed (reltuples = -1)."""
        with conn.cursor() as c:
            c.execute("SELECT reltuples::bigint AS n FROM pg_class "
                      "WHERE oid = 'memnos_control.audit_log'::regclass")
            n = c.fetchone()["n"]
            if n < 0:
                c.execute("SELECT count(*) AS n FROM memnos_control.audit_log")
                n = c.fetchone()["n"]
            return int(n)

    @staticmethod
    def usage_rollup(conn, hours=None):
        """Usage/cost rollup by op — all-time by default; pass hours for a window so the
        scan stays bounded (usage_ts index) on long-lived deployments."""
        win = "WHERE ts > now() - (%s||' hours')::interval" if hours else ""
        with conn.cursor() as c:
            c.execute("SELECT op, count(*) n, round(sum(cost_usd),4) cost, sum(tokens_in) tin, "
                      f"sum(tokens_out) tout FROM memnos_control.usage_ledger {win} "
                      "GROUP BY op ORDER BY cost DESC NULLS LAST",
                      ((hours,) if hours else None))
            return c.fetchall()

    # --- audit + usage ----------------------------------------------------
    @staticmethod
    def audit(conn, principal_id, action, namespace, ok, detail=None,
              latency_ms=None, result_count=None, status=None):
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.audit_log"
                      "(principal_id,action,namespace,ok,detail,latency_ms,result_count,status) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                      (principal_id, action, namespace, ok, json.dumps(detail) if detail else None,
                       latency_ms, result_count, status))

    @staticmethod
    def stats(conn, hours=24):
        """Pilot reliability rollup from the audit log: volume, error rate, latency
        p50/p95, recall-empty rate — by op, over the last N hours."""
        with conn.cursor() as c:
            c.execute("""
                SELECT action,
                  count(*) AS calls,
                  -- reliability error = genuine server FAULT (5xx). 403 ACL denials are
                  -- governance working as designed (status NULL) and are excluded here;
                  -- they remain visible via `errors` and acl_denied_pct below.
                  round(100.0*avg((coalesce(status,0) >= 500)::int), 1) AS error_pct,
                  round(100.0*avg((NOT ok AND coalesce(status,0) < 500)::int), 1) AS acl_denied_pct,
                  percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                  percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                  round(100.0*avg(CASE WHEN action='recall' AND result_count=0 THEN 1
                                       WHEN action='recall' THEN 0 END), 1) AS recall_empty_pct
                FROM memnos_control.audit_log
                WHERE ts > now() - (%s||' hours')::interval
                GROUP BY action ORDER BY calls DESC""", (hours,))
            return c.fetchall()

    @staticmethod
    def record_usage(conn, principal_id, namespace, op, model, tin, tout, cost):
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.usage_ledger"
                      "(principal_id,namespace,op,model,tokens_in,tokens_out,cost_usd) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s)", (principal_id, namespace, op, model, tin, tout, cost))

    # --- quality canary + feedback + errors ------------------------------
    @staticmethod
    def record_eval(conn, eval_name, metric, value, n=None, detail=None):
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.eval_runs(eval,metric,value,n,detail) "
                      "VALUES(%s,%s,%s,%s,%s)",
                      (eval_name, metric, value, n, json.dumps(detail) if detail else None))

    @staticmethod
    def eval_trend(conn, eval_name, metric, limit=10):
        with conn.cursor() as c:
            c.execute("SELECT ts, value, n FROM memnos_control.eval_runs WHERE eval=%s AND metric=%s "
                      "ORDER BY ts DESC LIMIT %s", (eval_name, metric, limit))
            return c.fetchall()

    @staticmethod
    def record_feedback(conn, principal_id, namespace, query, helpful, note=None):
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.feedback(principal_id,namespace,query,helpful,note) "
                      "VALUES(%s,%s,%s,%s,%s)", (principal_id, namespace, query, helpful, note))

    @staticmethod
    def recent_errors(conn, hours=24, limit=20):
        with conn.cursor() as c:
            c.execute("SELECT ts, principal_id, action, namespace, status, latency_ms, detail "
                      "FROM memnos_control.audit_log WHERE NOT ok AND ts > now()-(%s||' hours')::interval "
                      "ORDER BY ts DESC LIMIT %s", (hours, limit))
            return c.fetchall()

    # --- RELIABILITY HEURISTIC: metrics -> actionable findings -----------
    @staticmethod
    def health(conn, hours=24):
        """Turn raw metrics into actionable findings (the 'doctor'). Thresholds chosen for
        a pilot — tune as you learn the platform's normal ranges."""
        findings = []  # (level, message)
        T = {"err_warn": 5, "err_crit": 20, "p95_warn": 2000, "p95_crit": 8000,
             "empty_warn": 25, "quality_warn": 0.70, "quality_crit": 0.50}
        for r in Control.stats(conn, hours):
            op, calls = r["action"], r["calls"]
            err = float(r["error_pct"] or 0); p95 = int(r["p95_ms"] or 0); empty = r["recall_empty_pct"]
            if err >= T["err_crit"]:
                findings.append(("CRITICAL", f"{op}: {err}% errors — inspect `errors`; auth/ACL or a bug"))
            elif err >= T["err_warn"]:
                findings.append(("WARN", f"{op}: {err}% errors over {hours}h — check `errors`"))
            if p95 >= T["p95_crit"]:
                findings.append(("CRITICAL", f"{op}: p95 {p95}ms — pool saturation/reranker/DB; scale or tune"))
            elif p95 >= T["p95_warn"]:
                findings.append(("WARN", f"{op}: p95 {p95}ms — watch reranker/pool; consider lighter reranker"))
            if op == "recall" and empty is not None and float(empty) >= T["empty_warn"]:
                findings.append(("WARN", f"recall returns nothing {empty}% of the time — ingest gaps or namespace mismatch"))
        # quality canary regression
        q = Control.eval_trend(conn, "stale_suppression", "rate", 1)
        if q:
            v = float(q[0]["value"] or 0)
            if v < T["quality_crit"]:
                findings.append(("CRITICAL", f"stale-suppression {v:.0%} — memory serving STALE as current; supersession broken"))
            elif v < T["quality_warn"]:
                findings.append(("WARN", f"stale-suppression {v:.0%} (target ~85%) — quality regressed, review consolidation"))
        else:
            findings.append(("INFO", "no quality-canary eval recorded yet — run `memnos_eval.py`"))
        # restart storms (uptime signal)
        with conn.cursor() as c:
            c.execute("SELECT count(*) n FROM memnos_control.audit_log WHERE action='server_start' "
                      "AND ts > now()-interval '1 hour'")
            starts = c.fetchone()["n"]
        if starts >= 5:
            findings.append(("CRITICAL", f"{starts} server restarts in 1h — crash loop; check /tmp/memnos_server.log"))
        if not findings:
            findings.append(("OK", "no issues detected"))
        return findings
