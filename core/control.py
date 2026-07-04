"""Production control plane: identity, token auth, namespace ACL, audit, usage ledger.

Server-side identity (never client-trusted): a Bearer token resolves to a principal,
whose namespace GRANTS clamp every read/write. Tokens are stored as SHA-256 hashes
(high-entropy random tokens — no bcrypt needed). Every operation is audited; every
LLM op is metered. All in the same Postgres (one ACID engine = the governance moat).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets

# suggest-on-mismatch (issue #20, Part B) thresholds — TUNABLE, env-overridable. Kept
# conservative so the advisory never fires on noise, but a genuine strong match surfaces.
#   MIN_ENTITIES: a write must carry at least this many proper-noun TOKENS to be eligible
#     (avoids nudging on tiny/keyword-free writes).
#   DOMINANCE: the OTHER namespace must cover at least this share of those tokens.
SUGGEST_MIN_ENTITIES = int(os.environ.get("MEMNOS_SUGGEST_MIN_ENTITIES", "2"))
SUGGEST_DOMINANCE = float(os.environ.get("MEMNOS_SUGGEST_DOMINANCE", "0.6"))

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
-- namespace BINDING registry (issue #20, Part A): the dir/repo -> namespace map lives
-- HERE (scoped to the principal), not in a per-machine local file the server never sees.
-- key_type: 'repo' = host-agnostic, key = normalized git remote origin URL (resolves the
-- SAME on every machine); 'host_repo'/'host_path' = host-scoped, key + host_id pins a repo
-- or absolute path to ONE machine. Follows the user across reinstalls + machines.
CREATE TABLE IF NOT EXISTS memnos_control.bindings(
    id bigserial PRIMARY KEY,
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    key_type text NOT NULL CHECK (key_type IN ('repo','host_repo','host_path')),
    key text NOT NULL,
    namespace text NOT NULL,
    host_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(principal_id, key_type, key, host_id));
CREATE INDEX IF NOT EXISTS bindings_principal ON memnos_control.bindings(principal_id);
-- host registry (issue #20, A3): the principal's machines, so the UI can show
-- "Work laptop" vs "Dev laptop" and scope a binding to one host or "all hosts".
-- machine_id is re-derivable from sanitize(hostname) (no opaque UUID).
CREATE TABLE IF NOT EXISTS memnos_control.hosts(
    id bigserial PRIMARY KEY,
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    machine_id text NOT NULL,
    friendly_name text,
    last_seen timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(principal_id, machine_id));
-- deferred suggest-on-mismatch nudges (issue #20, Part B3): the ASYNC write path
-- (Claude Code Stop hook, async:true) extracts facts in a background worker, so the
-- suggestion can't ride the immediate /remember response. The worker drops an UNDELIVERED
-- nudge here; the next SessionStart hook reads + delivers it ("recent writes to A look
-- like B — bind it?"). Advisory ONLY — never moves a write. One open nudge per
-- (principal, write_ns, suggested_ns); a repeat write just refreshes its count/turn.
CREATE TABLE IF NOT EXISTS memnos_control.ns_nudges(
    id bigserial PRIMARY KEY,
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    write_ns text NOT NULL,
    suggested_ns text NOT NULL,
    reason text,
    hits int NOT NULL DEFAULT 1,
    last_turn_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    UNIQUE(principal_id, write_ns, suggested_ns));
CREATE INDEX IF NOT EXISTS ns_nudges_pending
    ON memnos_control.ns_nudges(principal_id) WHERE delivered_at IS NULL;
-- agent coordination leases (issue #26): at most one unexpired+unreleased holder per (namespace,key).
-- Partial index on released_at IS NULL (immutable predicate); expiry check enforced in acquire logic.
CREATE TABLE IF NOT EXISTS memnos_control.leases(
    id           bigserial PRIMARY KEY,
    namespace    text NOT NULL,
    key          text NOT NULL,
    holder_id    text NOT NULL,
    principal_id bigint REFERENCES memnos_control.principals(id),
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    released_at  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS leases_active
    ON memnos_control.leases(namespace, key) WHERE released_at IS NULL;
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

    # --- namespace binding registry (issue #20, Part A) --------------------
    @staticmethod
    def upsert_binding(conn, principal_id, key_type, key, namespace, host_id=None):
        """Insert (or update the namespace of) a binding, scoped to the principal.
        Conflict target is (principal_id, key_type, key, host_id) — re-binding the same
        key just changes the namespace. Returns the row. Audited."""
        if key_type not in ("repo", "host_repo", "host_path"):
            raise ValueError("key_type must be 'repo', 'host_repo', or 'host_path'")
        with conn.cursor() as c:
            # NULL host_id can't use a UNIQUE constraint conflict target reliably (NULLs
            # are distinct in SQL), so split: repo (host-agnostic) has host_id NULL and we
            # match on the partial key; host-scoped always has a host_id.
            if host_id is None:
                c.execute("SELECT id FROM memnos_control.bindings "
                          "WHERE principal_id=%s AND key_type=%s AND key=%s AND host_id IS NULL",
                          (principal_id, key_type, key))
                ex = c.fetchone()
                if ex:
                    c.execute("UPDATE memnos_control.bindings SET namespace=%s, updated_at=now() "
                              "WHERE id=%s RETURNING *", (namespace, ex["id"]))
                else:
                    c.execute("INSERT INTO memnos_control.bindings"
                              "(principal_id,key_type,key,namespace,host_id) VALUES(%s,%s,%s,%s,NULL) "
                              "RETURNING *", (principal_id, key_type, key, namespace))
            else:
                c.execute("INSERT INTO memnos_control.bindings"
                          "(principal_id,key_type,key,namespace,host_id) VALUES(%s,%s,%s,%s,%s) "
                          "ON CONFLICT (principal_id,key_type,key,host_id) "
                          "DO UPDATE SET namespace=EXCLUDED.namespace, updated_at=now() RETURNING *",
                          (principal_id, key_type, key, namespace, host_id))
            row = c.fetchone()
        Control.audit(conn, principal_id, "bind", namespace, True,
                      {"key_type": key_type, "key": key, "host_id": host_id})
        return row

    @staticmethod
    def list_bindings(conn, principal_id):
        with conn.cursor() as c:
            c.execute("SELECT id, key_type, key, namespace, host_id, created_at, updated_at "
                      "FROM memnos_control.bindings WHERE principal_id=%s "
                      "ORDER BY key_type, key", (principal_id,))
            return c.fetchall()

    @staticmethod
    def delete_binding(conn, principal_id, binding_id) -> bool:
        """Delete one of the principal's OWN bindings (scoped by principal_id, so a
        principal can never delete another's). Returns False if not theirs / not found."""
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.bindings WHERE id=%s AND principal_id=%s",
                      (binding_id, principal_id))
            ok = c.rowcount > 0
        if ok:
            Control.audit(conn, principal_id, "unbind", None, True, {"binding_id": binding_id})
        return ok

    @staticmethod
    def resolve_binding(conn, principal_id, repo_key=None, host_id=None, path_key=None):
        """Server-side resolution helper (optional convenience; the client resolver works
        off its local cache). Best match in priority order: repo (host-agnostic) ->
        host_repo (this host + repo) -> host_path (this host + path). Returns the binding
        row or None."""
        with conn.cursor() as c:
            if repo_key:
                c.execute("SELECT * FROM memnos_control.bindings WHERE principal_id=%s "
                          "AND key_type='repo' AND key=%s LIMIT 1", (principal_id, repo_key))
                r = c.fetchone()
                if r:
                    return r
            if host_id and repo_key:
                c.execute("SELECT * FROM memnos_control.bindings WHERE principal_id=%s "
                          "AND key_type='host_repo' AND key=%s AND host_id=%s LIMIT 1",
                          (principal_id, repo_key, host_id))
                r = c.fetchone()
                if r:
                    return r
            if host_id and path_key:
                c.execute("SELECT * FROM memnos_control.bindings WHERE principal_id=%s "
                          "AND key_type='host_path' AND key=%s AND host_id=%s LIMIT 1",
                          (principal_id, path_key, host_id))
                r = c.fetchone()
                if r:
                    return r
        return None

    # --- suggest-on-mismatch (issue #20, Part B) ---------------------------
    @staticmethod
    def suggest_namespace(conn, principal_id, write_ns, entity_names, *,
                          schema="tenant_memnos", min_entities=None, dominance=None,
                          max_candidate_ns=12):
        """NON-BLOCKING advisory: do the write's extracted entities look like they belong
        to a DIFFERENT namespace than the one it landed in? SUGGEST, NEVER auto-route — the
        write already landed in write_ns; this only returns a hint the client surfaces.

        Cheap + bounded:
          - entity_names is the small set the extractor produced for THIS write (<= a
            handful); we lower-case + dedupe and cap it.
          - one indexed query over `entities` (UNIQUE(namespace,name), ns b-tree) counts,
            per namespace, how many of those names already exist as entities — limited to
            the principal's OWN writable namespaces (bounded set) MINUS write_ns.
          - if some OTHER namespace contains a DOMINANT share of these entities AND the
            write's own namespace contains few of them, return a suggestion.

        Returns {"namespace": <other>, "reason": "..."} or None. No heavy scan: it touches
        only the entities table by (namespace,name), both indexed.
        """
        if min_entities is None:
            min_entities = SUGGEST_MIN_ENTITIES
        if dominance is None:
            dominance = SUGGEST_DOMINANCE
        # TOKEN normalization — the write's NER yields per-word proper-noun TOKENS
        # ("Project", "Zephyr"), but stored entities may be PHRASES ("Project Zephyr",
        # from a fact subject) OR tokens (from the encoder). Exact lower(name) equality
        # therefore misses the common case. We compare on lowercased word TOKENS on BOTH
        # sides: each stored entity name contributes its constituent tokens, and the write
        # contributes its tokens; a stored phrase "Project Zephyr" then matches the write
        # tokens {project, zephyr}. Two-char+ tokens only (drops noise like "a"/"to").
        def _toks(s):
            return [w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower())]
        want = set()
        for n in (entity_names or []):
            want.update(_toks(n))
        want = sorted(want)
        if len(want) < min_entities:
            return None
        # the principal's OWN writable namespaces (can_write), excluding the write target.
        writable = [g["namespace"] for g in Control.authorized_namespaces(conn, principal_id)
                    if g.get("can_write")]

        def covered(ns):
            for p in writable:
                if p == "*" or p == ns:
                    return True
                if p.endswith(":*") and ns.startswith(p[:-1]):
                    return True
            return False

        # CANDIDATE SELECTION BY RELEVANCE, then cap. Earlier code did `SELECT DISTINCT
        # namespace` (arbitrary order) and truncated to max_candidate_ns BEFORE checking
        # which namespaces actually hold the query's entities — so with more than the cap's
        # worth of entity-namespaces, the matching one could be silently dropped (past the
        # cut) → cand_hits empty → None. Instead, pre-filter in SQL to namespaces that
        # contain ≥1 of the want-TOKENS as a whole word (Postgres \m..\M word boundaries,
        # so 'zephyr' matches the phrase entity 'Project Zephyr' but not 'zephyrant'),
        # RANK by how many matching entity rows each has, and only THEN cap. The cap now
        # bounds the expensive per-name token step over RELEVANT namespaces, never blindly
        # truncates an unordered list.
        token_re = r"\m(" + "|".join(re.escape(t) for t in want) + r")\M"
        with conn.cursor() as c:
            c.execute(f"SELECT namespace, count(*) AS m FROM {schema}.entities "
                      f"WHERE name ~* %s GROUP BY namespace ORDER BY m DESC, namespace",
                      (token_re,))
            ranked = [r["namespace"] for r in c.fetchall()]
        cand = [ns for ns in ranked if ns != write_ns and covered(ns)][:max_candidate_ns]
        if not cand:
            return None
        # Pull the candidate + write namespaces' entity names (bounded: candidate set is
        # capped, names are short) and intersect on TOKENS in Python. One indexed query
        # (namespace b-tree) per side; no per-row scan beyond these namespaces.
        wantset = set(want)

        def _ns_token_hits(namespaces):
            """{namespace: count of distinct want-tokens its entity names cover}."""
            if not namespaces:
                return {}
            with conn.cursor() as c:
                c.execute(f"SELECT namespace, name FROM {schema}.entities "
                          f"WHERE namespace = ANY(%s)", (list(namespaces),))
                rows = c.fetchall()
            hits = {}
            for r in rows:
                covered_toks = {t for t in _toks(r["name"]) if t in wantset}
                if covered_toks:
                    hits.setdefault(r["namespace"], set()).update(covered_toks)
            return {ns: len(ts) for ns, ts in hits.items()}

        own = _ns_token_hits([write_ns]).get(write_ns, 0)
        cand_hits = _ns_token_hits(cand)
        if not cand_hits:
            return None
        other_ns = max(cand_hits, key=cand_hits.get)
        other_n = cand_hits[other_ns]
        total = len(want)
        # DOMINANCE: the other ns holds a strong majority of these entity tokens, the write's
        # own ns holds clearly fewer, and the other ns out-covers the write's own.
        if (other_n / total) >= dominance and other_n > own:
            return {"namespace": other_ns,
                    "reason": f"{other_n} of these {total} entities are mostly in {other_ns}"}
        return None

    # --- deferred nudges (issue #20, Part B3 — async write path) ------------
    @staticmethod
    def record_nudge(conn, principal_id, write_ns, suggested_ns, reason, *, turn_id=None):
        """Persist (or refresh) an UNDELIVERED suggest-on-mismatch nudge from the async
        ingest worker. Idempotent per (principal, write_ns, suggested_ns): a repeat write to
        the same mismatched pair bumps `hits` and re-opens the nudge (clears delivered_at) so
        a persistent pattern resurfaces, rather than spamming a row per write. Advisory only.
        Best-effort: callers wrap in try/except so a write never fails on the nudge."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.ns_nudges"
                      "(principal_id, write_ns, suggested_ns, reason, last_turn_id) "
                      "VALUES(%s,%s,%s,%s,%s) "
                      "ON CONFLICT (principal_id, write_ns, suggested_ns) DO UPDATE SET "
                      "  hits = memnos_control.ns_nudges.hits + 1, "
                      "  reason = EXCLUDED.reason, last_turn_id = EXCLUDED.last_turn_id, "
                      "  updated_at = now(), delivered_at = NULL",
                      (principal_id, write_ns, suggested_ns, reason, turn_id))

    @staticmethod
    def take_pending_nudges(conn, principal_id, *, limit=10):
        """Return the principal's UNDELIVERED nudges and mark them delivered in the SAME
        transaction (at-most-once display — fine for an advisory). Newest first. Used by the
        SessionStart hook to surface 'recent writes to A look like B — bind it?' once."""
        with conn.cursor() as c:
            c.execute("UPDATE memnos_control.ns_nudges SET delivered_at = now() "
                      "WHERE id IN (SELECT id FROM memnos_control.ns_nudges "
                      "             WHERE principal_id=%s AND delivered_at IS NULL "
                      "             ORDER BY updated_at DESC LIMIT %s) "
                      "RETURNING write_ns, suggested_ns, reason, hits, last_turn_id",
                      (principal_id, limit))
            return c.fetchall()

    @staticmethod
    def write_recap(conn, principal_id, *, days=7, limit=20):
        """Per-namespace WRITE count for this principal over the last `days` (issue #20,
        Part B periodic recap). Counts successful write actions in the audit log. Cheap:
        one indexed (audit_ts) aggregate. Returns [{namespace, writes}] desc."""
        with conn.cursor() as c:
            c.execute("SELECT namespace, count(*) AS writes FROM memnos_control.audit_log "
                      "WHERE principal_id=%s AND ok AND namespace IS NOT NULL "
                      "AND action IN ('remember','memory/write','memory_write') "
                      "AND ts > now() - (%s::int || ' days')::interval "
                      "GROUP BY namespace ORDER BY writes DESC LIMIT %s",
                      (principal_id, days, limit))
            return c.fetchall()

    # --- host registry (issue #20, A3) -------------------------------------
    @staticmethod
    def upsert_host(conn, principal_id, machine_id, friendly_name=None):
        """Register this machine (or bump last_seen / rename). friendly_name is only
        overwritten when a non-None value is supplied, so a plain check-in keeps the
        existing name. Returns the row."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.hosts(principal_id,machine_id,friendly_name) "
                      "VALUES(%s,%s,%s) ON CONFLICT (principal_id,machine_id) DO UPDATE "
                      "SET last_seen=now(), "
                      "    friendly_name=COALESCE(EXCLUDED.friendly_name, memnos_control.hosts.friendly_name) "
                      "RETURNING id, machine_id, friendly_name, last_seen, created_at",
                      (principal_id, machine_id, friendly_name))
            return c.fetchone()

    @staticmethod
    def list_hosts(conn, principal_id):
        with conn.cursor() as c:
            c.execute("SELECT id, machine_id, friendly_name, last_seen, created_at "
                      "FROM memnos_control.hosts WHERE principal_id=%s ORDER BY last_seen DESC",
                      (principal_id,))
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

    # --- agent coordination leases (issue #26) --------------------------------

    @staticmethod
    def lease_acquire(conn, namespace, key, holder_id, principal_id, ttl_seconds=1200):
        """Atomic acquire. Returns {granted, holder_id, expires_at}.
        granted=True  → caller now holds the lease.
        granted=False → another unexpired holder has it; caller should back off."""
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO memnos_control.leases(namespace,key,holder_id,principal_id,expires_at)
                VALUES (%s,%s,%s,%s, now() + (%s * interval '1 second'))
                ON CONFLICT (namespace,key) WHERE released_at IS NULL
                DO UPDATE SET
                    holder_id    = EXCLUDED.holder_id,
                    principal_id = EXCLUDED.principal_id,
                    acquired_at  = now(),
                    expires_at   = EXCLUDED.expires_at
                WHERE memnos_control.leases.expires_at <= now()
                RETURNING holder_id, expires_at
            """, (namespace, key, holder_id, principal_id, ttl_seconds))
            row = c.fetchone()
        if row:
            return {"granted": True, "holder_id": row["holder_id"],
                    "expires_at": row["expires_at"].isoformat()}
        # Lease is held and unexpired — read who holds it
        with conn.cursor() as c:
            c.execute("""
                SELECT holder_id, expires_at FROM memnos_control.leases
                WHERE namespace=%s AND key=%s AND released_at IS NULL AND expires_at > now()
            """, (namespace, key))
            cur = c.fetchone()
        if cur:
            return {"granted": False, "holder_id": cur["holder_id"],
                    "expires_at": cur["expires_at"].isoformat()}
        # Edge: expired between our two reads — tell caller to retry immediately
        return {"granted": False, "holder_id": None, "expires_at": None, "retry": True}

    @staticmethod
    def lease_heartbeat(conn, namespace, key, holder_id, ttl_seconds=1200):
        """Extend the expiry of a held lease. Returns {renewed, expires_at}."""
        with conn.cursor() as c:
            c.execute("""
                UPDATE memnos_control.leases
                SET expires_at = now() + (%s * interval '1 second')
                WHERE namespace=%s AND key=%s AND holder_id=%s
                  AND released_at IS NULL AND expires_at > now()
                RETURNING expires_at
            """, (ttl_seconds, namespace, key, holder_id))
            row = c.fetchone()
        if not row:
            return {"renewed": False}
        return {"renewed": True, "expires_at": row["expires_at"].isoformat()}

    @staticmethod
    def lease_release(conn, namespace, key, holder_id):
        """Release a held lease. Returns {released: bool}."""
        with conn.cursor() as c:
            c.execute("""
                UPDATE memnos_control.leases SET released_at = now()
                WHERE namespace=%s AND key=%s AND holder_id=%s AND released_at IS NULL
                RETURNING id
            """, (namespace, key, holder_id))
            row = c.fetchone()
        return {"released": bool(row)}

    @staticmethod
    def lease_who_holds(conn, namespace, key):
        """Return current unexpired holder info, or {held: False}."""
        with conn.cursor() as c:
            c.execute("""
                SELECT holder_id, acquired_at, expires_at FROM memnos_control.leases
                WHERE namespace=%s AND key=%s AND released_at IS NULL AND expires_at > now()
            """, (namespace, key))
            row = c.fetchone()
        if not row:
            return {"held": False, "holder_id": None}
        return {"held": True, "holder_id": row["holder_id"],
                "acquired_at": row["acquired_at"].isoformat(),
                "expires_at": row["expires_at"].isoformat()}

    @staticmethod
    def lease_list(conn, namespace):
        """List all active (unexpired, unreleased) leases in a namespace."""
        with conn.cursor() as c:
            c.execute("""
                SELECT key, holder_id, acquired_at, expires_at FROM memnos_control.leases
                WHERE namespace=%s AND released_at IS NULL AND expires_at > now()
                ORDER BY acquired_at
            """, (namespace,))
            rows = c.fetchall()
        return [{"key": r["key"], "holder_id": r["holder_id"],
                 "acquired_at": r["acquired_at"].isoformat(),
                 "expires_at": r["expires_at"].isoformat()} for r in rows]

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
    def writable_namespaces(conn, principal_id, limit=10):
        """Concrete namespaces this principal may WRITE to, capped at `limit`.
        Exact grants are included directly (even if the namespace has no data yet).
        Wildcard grants ('*' or 'prefix:*') are expanded against existing namespaces.
        Used to populate the suggestion in write-403 responses."""
        grants = [g for g in Control.authorized_namespaces(conn, principal_id) if g["can_write"]]
        if not grants:
            return []
        result = set()
        wildcard_patterns = []
        for g in grants:
            ns = g["namespace"]
            if ns == "*" or ns.endswith(":*"):
                wildcard_patterns.append(ns)
            else:
                result.add(ns)
        if wildcard_patterns:
            with conn.cursor() as c:
                c.execute("SELECT DISTINCT namespace FROM tenant_memnos.raw_turns "
                          "UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic")
                existing = [r["namespace"] for r in c.fetchall()]
            for ns in existing:
                for p in wildcard_patterns:
                    if p == "*" or (p.endswith(":*") and ns.startswith(p[:-1])):
                        result.add(ns)
                        break
        return sorted(result)[:limit]

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

    @staticmethod
    def usage_summary(conn, period_days=30, namespace=None):
        """Richer usage summary: totals + breakdown by op, namespace, and day-by-day trend.

        Returns:
            {
                "total_usd": float,
                "total_tokens": int,
                "by_op": {op: {"n": int, "usd": float, "tokens_in": int, "tokens_out": int}},
                "by_namespace": {ns: {"n": int, "usd": float, "tokens": int}},
                "by_day": [{"date": "YYYY-MM-DD", "usd": float, "tokens": int}]  # newest first
            }
        """
        days = max(1, min(int(period_days), 365))
        where = "WHERE ts > now() - (%s || ' days')::interval"
        base = [days]
        if namespace:
            where += " AND namespace=%s"
            base = [days, namespace]

        with conn.cursor() as c:
            c.execute(
                f"SELECT COALESCE(sum(cost_usd), 0) AS total_usd, "
                f"COALESCE(sum(tokens_in + tokens_out), 0) AS total_tokens "
                f"FROM memnos_control.usage_ledger {where}", base)
            totals = c.fetchone()

            c.execute(
                f"SELECT op, count(*) AS n, COALESCE(sum(cost_usd), 0) AS usd, "
                f"COALESCE(sum(tokens_in), 0) AS tokens_in, "
                f"COALESCE(sum(tokens_out), 0) AS tokens_out "
                f"FROM memnos_control.usage_ledger {where} "
                f"GROUP BY op ORDER BY usd DESC NULLS LAST", base)
            by_op = {(r["op"] or ""): {"n": r["n"], "usd": float(r["usd"]),
                                        "tokens_in": r["tokens_in"],
                                        "tokens_out": r["tokens_out"]}
                     for r in c.fetchall()}

            c.execute(
                f"SELECT namespace, count(*) AS n, COALESCE(sum(cost_usd), 0) AS usd, "
                f"COALESCE(sum(tokens_in + tokens_out), 0) AS tokens "
                f"FROM memnos_control.usage_ledger {where} "
                f"GROUP BY namespace ORDER BY usd DESC NULLS LAST", base)
            by_namespace = {(r["namespace"] or ""): {"n": r["n"], "usd": float(r["usd"]),
                                                      "tokens": r["tokens"]}
                            for r in c.fetchall()}

            trend_days = min(days, 7)
            trend_where = "WHERE ts > now() - (%s || ' days')::interval"
            trend_base = [trend_days]
            if namespace:
                trend_where += " AND namespace=%s"
                trend_base = [trend_days, namespace]
            c.execute(
                f"SELECT date_trunc('day', ts)::date AS day, "
                f"COALESCE(sum(cost_usd), 0) AS usd, "
                f"COALESCE(sum(tokens_in + tokens_out), 0) AS tokens "
                f"FROM memnos_control.usage_ledger {trend_where} "
                f"GROUP BY day ORDER BY day DESC", trend_base)
            by_day = [{"date": str(r["day"]), "usd": float(r["usd"]), "tokens": r["tokens"]}
                      for r in c.fetchall()]

        return {
            "total_usd": float(totals["total_usd"]),
            "total_tokens": int(totals["total_tokens"]),
            "by_op": by_op,
            "by_namespace": by_namespace,
            "by_day": by_day,
        }

    @staticmethod
    def budget_status(conn, daily_usd=None, monthly_usd=None):
        """Check current spend against optional daily/monthly thresholds.

        Returns dict with daily_spend, monthly_spend, thresholds, and exceeded flags.
        Spend is computed from the usage_ledger using the usage_ts index.
        """
        with conn.cursor() as c:
            c.execute(
                "SELECT COALESCE(sum(cost_usd), 0) AS spend "
                "FROM memnos_control.usage_ledger "
                "WHERE ts > now() - interval '1 day'")
            daily_spend = float(c.fetchone()["spend"])

            c.execute(
                "SELECT COALESCE(sum(cost_usd), 0) AS spend "
                "FROM memnos_control.usage_ledger "
                "WHERE ts > now() - interval '30 days'")
            monthly_spend = float(c.fetchone()["spend"])

        daily_ok = daily_spend <= daily_usd if daily_usd is not None else True
        monthly_ok = monthly_spend <= monthly_usd if monthly_usd is not None else True
        return {
            "daily_spend_usd": round(daily_spend, 6),
            "monthly_spend_usd": round(monthly_spend, 6),
            "daily_limit_usd": daily_usd,
            "monthly_limit_usd": monthly_usd,
            "daily_ok": daily_ok,
            "monthly_ok": monthly_ok,
            "exceeded": not (daily_ok and monthly_ok),
        }

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
    def secret_get(conn, name):
        """Return the ciphertext row for a named secret, or None if not found."""
        with conn.cursor() as c:
            c.execute("SELECT nonce, ciphertext FROM memnos_control.secrets WHERE name=%s", (name,))
            return c.fetchone()

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
