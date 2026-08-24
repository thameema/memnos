"""Production control plane: identity, token auth, namespace ACL, audit, usage ledger.

Server-side identity (never client-trusted): a Bearer token resolves to a principal,
whose namespace GRANTS clamp every read/write. Tokens are stored as SHA-256 hashes
(high-entropy random tokens — no bcrypt needed). Every operation is audited; every
LLM op is metered. All in the same Postgres (one ACID engine = the governance moat).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets

logger = logging.getLogger(__name__)

# suggest-on-mismatch (issue #20, Part B) thresholds — TUNABLE, env-overridable. Kept
# conservative so the advisory never fires on noise, but a genuine strong match surfaces.
#   MIN_ENTITIES: a write must carry at least this many proper-noun TOKENS to be eligible
#     (avoids nudging on tiny/keyword-free writes).
#   DOMINANCE: the OTHER namespace must cover at least this share of those tokens.
SUGGEST_MIN_ENTITIES = int(os.environ.get("MEMNOS_SUGGEST_MIN_ENTITIES", "2"))
SUGGEST_DOMINANCE = float(os.environ.get("MEMNOS_SUGGEST_DOMINANCE", "0.6"))

# namespace registry backfill (issue #41 fix A follow-up): rows scanned per keyset chunk
# per source table. Bounds each individual query's cost regardless of table size, so a
# huge pre-existing table means more batches, not one query holding the connection (and
# its statement_timeout exemption) for the duration of a full scan.
NAMESPACE_BACKFILL_BATCH = int(os.environ.get("MEMNOS_NAMESPACE_BACKFILL_BATCH", "50000"))

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
-- ROLE-BASED GRANTS (issue #81, epic #70 item 4): roles/groups as grantable subjects,
-- layered OVER the `grants` table above -- never modifies it. A principal's EFFECTIVE
-- access is the union of its own direct grants (`grants`) and the grants of every role
-- it's a member of (`role_grants` via `role_members`) -- see effective_namespaces()
-- below, the single resolver authorize()/readable_namespaces()/writable_namespaces()/
-- is_admin() all call so role support composes everywhere instead of being
-- re-implemented per call site. Two NEW tables rather than a nullable role_id column on
-- `grants` (which would require relaxing grants.principal_id's NOT NULL -- exactly the
-- schema-breaking change issue #81 rules out): `grants` and every existing grant row is
-- completely untouched.
CREATE TABLE IF NOT EXISTS memnos_control.roles(
    id bigserial PRIMARY KEY, name text UNIQUE NOT NULL, description text,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now());
-- role membership: which principals hold which role. Same join-table shape as `bindings`.
CREATE TABLE IF NOT EXISTS memnos_control.role_members(
    role_id bigint NOT NULL REFERENCES memnos_control.roles(id),
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(role_id, principal_id));
CREATE INDEX IF NOT EXISTS role_members_principal ON memnos_control.role_members(principal_id);
-- role grants: same shape as `grants` (can_read/can_write, exact/prefix-'*'/'*' wildcard
-- matching reused VERBATIM by authorize() via effective_namespaces()) -- just keyed by
-- role_id instead of principal_id, so "architects can write to standards" is one row
-- instead of one row per architect.
CREATE TABLE IF NOT EXISTS memnos_control.role_grants(
    role_id bigint NOT NULL REFERENCES memnos_control.roles(id),
    namespace text NOT NULL, can_read boolean NOT NULL DEFAULT true, can_write boolean NOT NULL DEFAULT true,
    UNIQUE(role_id, namespace));
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
-- namespace registry: namespaces are explicit, user-created objects (via the UI/CLI) OR
-- auto-registered the first time they receive data (see auto_registered below). Lets the
-- console list/create/delete them, and (issue #41) lets readable_namespaces() resolve
-- wildcard grants against this small table instead of DISTINCT-scanning the data tables.
CREATE TABLE IF NOT EXISTS memnos_control.namespaces(
    name text PRIMARY KEY, created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(), description text);
-- namespace KIND (0.1.6): 'memory' (default, conversational) or 'knowledge' (curated
-- reference corpus meant to GROUND other namespaces' recall via namespace_links).
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'memory';
-- AUTO-REGISTRATION (issue #41): true when this row was created by the write path's
-- first-touch upsert (core/store.py insert_raw_turn), false when created explicitly via
-- create_namespace() (UI/CLI). Keeps the registry a COMPLETE index of every namespace that
-- has data (so wildcard-grant expansion never needs a data-table scan) while preserving
-- the UI's "discovered" pill (ui/app.js) for namespaces nobody explicitly registered.
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS auto_registered boolean NOT NULL DEFAULT false;
-- SAME-ROOT INHERITANCE OPT-OUT (issue #85, epic #70 Mechanism A): true (default) means
-- this namespace automatically also consults its ':'-prefix ancestors' PINNED CONSTRAINTS
-- at recall/enforce time (customer:example:widgets -> customer:example -> customer) —
-- implicit and safe because the user owns the whole subtree, but each ancestor is still
-- gated by the caller's own read grant on it (see Control.effective_ancestors /
-- memnos_server.py's grounded-recall branch), same as an explicit namespace_links target.
-- Set false to opt a namespace OUT of automatic ancestor consultation entirely (all
-- levels at once — this is a property of the CONSULTING namespace, not a per-ancestor
-- edge, so there is no separate table here). Missing row (namespace never registered)
-- MUST read as true, not false — see Control.namespace_inherits_ancestors's COALESCE.
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS inherit_ancestors boolean NOT NULL DEFAULT true;
-- BACKFILL COMPLETION MARKER: a singleton row (id is always `true`, enforced by the CHECK)
-- inserted once _run_namespace_registry_backfill has scanned tenant_memnos.* to
-- completion. Deliberately NOT derived from whether any auto_registered row exists --
-- a deployment where every namespace was already explicitly registered before this PR
-- would never produce one via ON CONFLICT DO NOTHING, which would make the "has it run"
-- guard below true forever and re-scan tenant_memnos.* on every single boot.
CREATE TABLE IF NOT EXISTS memnos_control.namespace_registry_backfill(
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    completed_at timestamptz NOT NULL DEFAULT now());
-- GROUNDED RECALL links (0.1.6): recall on src_ns ALSO searches dst_ns — but only if the
-- CALLING principal holds a read grant on dst_ns (link = policy, grant = permission;
-- BOTH required). Skipped links are surfaced in the /recall response (links_skipped).
CREATE TABLE IF NOT EXISTS memnos_control.namespace_links(
    src_ns text NOT NULL, dst_ns text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(src_ns, dst_ns));
-- LINK KIND (issue #85): taxonomy for explicit namespace_links edges, distinct from the
-- automatic same-root walk-up (Mechanism A, above — that mechanism has NO row here at
-- all; it's derived purely from ':'-prefix string structure at query time, gated by
-- inherit_ancestors + the caller's grant, never stored as an edge). Default 'link' is
-- deliberately the ONLY value every EXISTING row gets on this migration — it names what
-- these rows have always meant ("recall on src also searches dst", i.e. grounding), not
-- a retroactive relabeling to 'governed_by'/'inherits'. Callers may pass kind='inherits'
-- or kind='governed_by' when creating a NEW link to record explicit cross-root
-- inheritance/governance intent for humans reading `namespace links` — informational only
-- today (no runtime behavior currently branches on kind; #83's constraint_overrides is
-- the actual precedence mechanism and intentionally stays a separate table/concept, see
-- its own DDL comment). Kept open (no CHECK) — same as namespaces.kind above; validated at
-- the application layer in Control.link_namespaces instead.
ALTER TABLE memnos_control.namespace_links ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'link';
-- COPY PROVENANCE (issue #85 item 5): one row per copy_memories_from / `namespace
-- copy|move` call — an APPEND-ONLY event log, not an edge. Deliberately NOT folded into
-- namespace_links: that table's UNIQUE(src_ns,dst_ns) + ON CONFLICT DO NOTHING models a
-- single deduped policy edge, but a copy is a repeatable point-in-time EVENT (the same
-- src->dst pair can be copied more than once, each at its own timestamp) — reusing
-- namespace_links would silently collapse repeat copies into one row and lose exactly the
-- history this exists to capture. This is intentionally ONLY provenance (who copied what,
-- from where, when) — no staleness signal is computed from it. The live-link paths
-- (Mechanism A/B above) need no staleness signal by design (nothing ever goes stale when
-- the read is live); a one-time snapshot's staleness is a distinct, NOT-yet-built feature
-- this table is a prerequisite for, not a replacement for building it now.
CREATE TABLE IF NOT EXISTS memnos_control.namespace_copy_provenance(
    id bigserial PRIMARY KEY,
    dst_ns text NOT NULL,
    src_ns text NOT NULL,
    mode text NOT NULL,
    copied_by bigint REFERENCES memnos_control.principals(id),
    copied_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS namespace_copy_provenance_dst ON memnos_control.namespace_copy_provenance(dst_ns);
-- ENFORCED CONSTRAINTS (issue #28): ask/block-level constraints only. advise-level
-- constraints (the default, and everything #27's `/memnos constraint` writes) are plain
-- memory_type='constraint' pinned memories in the TENANT schema — see
-- core/store.py:pinned_constraints(). Only ask/block need a row HERE, in the control
-- plane, because only those need a cheap, LLM-free, DB-free lookup on the PreToolUse hot
-- path: a SessionStart hook caches this table's active rows for the session's namespace to
-- a local file; PreToolUse matches against that cache with no server/DB/LLM round-trip on
-- the common (no-match/allow) case. tool_matcher is NOT NULL (enforced in code at write
-- time, not just this constraint) because a prose rule can't be matched to a structured
-- tool call deterministically without an LLM, and enforcement is LLM-free by design — a
-- --enforce ask|block with no --tool is REJECTED at `constraint add`, never silently
-- downgraded (a silently-inert guardrail is worse than an error).
CREATE TABLE IF NOT EXISTS memnos_control.constraint_enforcement(
    id bigserial PRIMARY KEY,
    namespace text NOT NULL,
    rule_text text NOT NULL,
    enforce_level text NOT NULL CHECK (enforce_level IN ('ask','block')),
    tool_matcher text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    active boolean NOT NULL DEFAULT true);
CREATE INDEX IF NOT EXISTS constraint_enforcement_ns ON memnos_control.constraint_enforcement(namespace) WHERE active;
-- CONSTRAINT PRECEDENCE OVERRIDES (issue #83, epic #70 item 2): an explicit edge
-- letting a CHILD namespace's pinned constraint deterministically WIN a precedence
-- conflict against a same-`constraint_subject` constraint in an ANCESTOR namespace,
-- reversing the default (ancestor wins — see BrainStore.resolve_constraint_precedence).
-- "Ancestor" is PURE ':'-prefix string comparison between two namespaces already
-- present in one recall's pin set (epic #70 Mechanism A) — deliberately NOT
-- `namespace_links` above, whose meaning is "recall here also searches there" (grounded
-- recall), an unrelated concept. Overloading it would silently turn every EXISTING
-- grounding link into a precedence relationship the day this feature ships — an install
-- that linked a project namespace to a shared reference corpus would find its own
-- project constraints suppressed on next boot with no config change of its own. Epic
-- #70 warns about exactly this ("implicit cross-root governance is opaque and
-- dangerous") and calls for a NEW, explicit table — this is it. Never auto-created:
-- only `constraint override add` (CLI, admin-only) inserts a row (epic #70 Mechanism B:
-- "explicit, always visible, deliberate act").
CREATE TABLE IF NOT EXISTS memnos_control.constraint_overrides(
    id bigserial PRIMARY KEY,
    child_namespace text NOT NULL,
    parent_namespace text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(child_namespace, parent_namespace));
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
-- APPROVED DEVIATION LOG (issue #106): an explicit, audited exception to a corpus
-- constraint — "the architect signed off on breaking this rule, here's why, here's how
-- long it's good for" — recorded instead of silently ignoring a corpus_check hit.
-- constraint_id references a {tenant}.semantic row (kind='constraint') by id, but
-- DELIBERATELY carries no FK: ingest_constraints() (core/store.py) DELETEs and
-- re-inserts every constraint row for a source on re-ingest, so a constraint's id is
-- NOT stable across re-ingests of the same doc. An FK would make every re-ingest of a
-- source with a live deviation fail outright; instead a deviation whose constraint_id
-- no longer exists simply stops matching anything in corpus_check's per-candidate
-- lookup (silently inert, not an error) — see BrainStore.corpus_check(). `until` is the
-- same optional dotted-numeric version string ingest_constraints()'s `since`/`until`
-- use (NULL = the deviation never expires on its own); it is INDEPENDENT of the
-- constraint's own constraint_since/constraint_until window (core/schema.sql) — a
-- deviation can outlive the constraint's own nominal expiry (grace period) or expire
-- before it (approval revoked early by a later, shorter-lived deviation not modeled
-- here — corpus_check always uses the most recently created row for a constraint_id).
CREATE TABLE IF NOT EXISTS memnos_control.corpus_deviations(
    id bigserial PRIMARY KEY,
    namespace text NOT NULL,
    constraint_id bigint NOT NULL,
    rationale text NOT NULL,
    approved_by text NOT NULL,
    until text,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS corpus_deviations_lookup
    ON memnos_control.corpus_deviations(namespace, constraint_id, created_at DESC);
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

# Pseudo-namespace convention for secret-resolve authorization (issue #114, "Secret
# Shield"): POST /secret/resolve authorizes a request for secret NAME by calling
# Control.authorize(conn, principal_id, f"{SECRET_NS_PREFIX}NAME") -- i.e. it reuses the
# EXACT SAME grants table + CLI (`memnos grant add <principal> secret:NAME`) as real
# memory namespaces. Zero schema changes: `secret:NAME` (exact) or `secret:*` (broad,
# via authorize()'s existing ':*' prefix-wildcard matching) is just another row in
# memnos_control.grants. NOT to be confused with core/vault.py's REF_PREFIX
# ("secret://NAME") -- that's a VALUE reference substituted into config at resolve
# time; this is a NAMESPACE string checked by authorize(), never touched by Vault.
#
# Real costs of reusing the namespace string space (documented, not fully solved --
# would need a schema change to fully separate, which issue #114 rules out):
#   1. Pseudo-namespaces are indistinguishable, at the string level, from a real memory
#      namespace that happens to start with "secret:". An admin who ever registers an
#      actual memory namespace under that prefix (nothing stops them -- create_namespace
#      has no reserved-prefix check) would collide with this convention: a grant meant
#      for that memory namespace would ALSO authorize secret-resolve calls whose name
#      matches the suffix, and vice versa. Treat "secret:" as reserved by operator
#      convention; not enforced.
#   2. Pseudo-namespaces still show up wherever a principal's grants are listed/expanded
#      as raw ACL rows -- authorized_namespaces() (CLI `whoami`/`grant ls`),
#      effective_namespaces() (role-inherited grants), and the wildcard-expansion inputs
#      to readable_namespaces()/writable_namespaces() (a '*' or 'secret:*' grant makes an
#      exact "secret:NAME" grant enumerable there too). We do NOT filter those: they are
#      the ENFORCEMENT path authorize() depends on, and no namespace starting with
#      "secret:" is ever written to tenant_memnos.raw_turns/semantic (Vault storage is a
#      completely separate table), so a pseudo-namespace surfacing in a wide-recall fan-out
#      or a `whoami` grant listing yields zero memory rows -- never a plaintext leak, just
#      a cosmetic namespace-shaped string. We DO filter the two admin-facing namespace
#      CENSUS queries below (list_namespaces, namespace_prune_candidates) so a secret
#      grant never masquerades as a real (browsable/prunable) memory namespace in the
#      console or CLI -- see the `secret:` exclusion in both queries' grants UNION branch.
#      Without that filter, `memnos namespace prune --empty` would treat a `secret:NAME`
#      grant as an "empty" namespace candidate and delete_namespace() would silently
#      revoke it (delete_namespace unconditionally deletes matching grants rows).
#   3. '*' matches every namespace per authorize()'s existing semantics, INCLUDING every
#      "secret:NAME" pseudo-namespace. Granting a new, narrower `secret:NAME` scope to a
#      new token does not revoke or narrow any existing '*'-admin principal's ability to
#      resolve that (or any other) secret -- narrower grants are strictly additive, never
#      a retroactive reduction of an existing admin token's blast radius.
SECRET_NS_PREFIX = "secret:"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Control:
    """Operates over a pooled connection (passed per call) so it's stateless + poolable."""

    @staticmethod
    def init(conn):
        with conn.cursor() as c:
            c.execute(CONTROL_DDL)               # DDL failures must still crash boot
            if Control._namespace_registry_needs_backfill(c):
                # This wraps the WHOLE backfill call -- the paginated scan across both
                # tables plus the final marker-row INSERT -- not just the marker row.
                # Any statement in there (a chunk INSERT, lock contention, a connection
                # blip, the marker row itself) can transiently fail, and it's
                # self-healing either way: log and continue rather than crash boot over
                # it -- the next restart just redoes the (already-safe, idempotent) scan.
                # Narrowing this to just the marker-row INSERT would let a mid-scan
                # failure crash server boot -- exactly the round-1 finding this broad
                # scope exists to close.
                try:
                    Control._run_namespace_registry_backfill(c)
                except Exception:
                    logger.error("namespace registry backfill failed; namespace "
                                 "registry may be incomplete until this succeeds, so "
                                 "wildcard-grant wide recall may silently omit "
                                 "namespaces until the next restart retries it",
                                 exc_info=True)

    @staticmethod
    def _namespace_registry_needs_backfill(c) -> bool:
        """Guard for _run_namespace_registry_backfill: true only before the backfill has
        ever completed (tracked by the namespace_registry_backfill marker row, set
        unconditionally at the end of a successful run — see that method) AND once the
        tenant schema actually exists (fresh install: create_schema() may not have run
        yet — nothing to backfill). Split from the backfill itself so tests can invoke
        the backfill directly regardless of this database's global guard state."""
        c.execute("""
            SELECT NOT EXISTS(SELECT 1 FROM memnos_control.namespace_registry_backfill)
                   AND to_regclass('tenant_memnos.raw_turns') IS NOT NULL AS need_backfill""")
        return c.fetchone()["need_backfill"]

    @staticmethod
    def _run_namespace_registry_backfill(c):
        """One-time (per-database, ever) seed of memnos_control.namespaces from data that
        predates issue #41's auto-registration (core/store.py insert_raw_turn now
        registers a namespace on its first write, but namespaces written BEFORE that
        change exist only in tenant_memnos.raw_turns/semantic, not the registry). Runs at
        server boot, never on the recall hot path — gated by
        _namespace_registry_needs_backfill() so it runs AT MOST ONCE ever, not on every
        restart (the marker row is set unconditionally below, regardless of whether any
        namespace actually needed inserting).

        This used to be the same unbounded DISTINCT-scan-under-UNION query
        readable_namespaces() ran on every wide recall — i.e. exactly the full-table-scan
        pattern this issue exists to get off a 15s-statement_timeout connection, just
        moved to boot instead of request time. Two changes fix that: (1) exempted from
        this connection's statement_timeout for the duration (restored after, same
        reasoning as create_schema()'s DDL exemption in memnos_server.py); (2) paginated
        via keyset chunks on each table's bigserial id, so a single query is never itself
        the failure point on a huge pre-existing table — a bigger table just means more
        (small, bounded) batches, not one query holding a snapshot/locks for minutes."""
        c.execute("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
        prior_timeout_ms = int(c.fetchone()["setting"])
        c.execute("SET statement_timeout = 0")
        try:
            for table in ("raw_turns", "semantic"):
                c.execute(f"SELECT max(id) AS max_id FROM tenant_memnos.{table}")
                max_id = c.fetchone()["max_id"]
                start = 0
                while max_id is not None and start < max_id:
                    end = start + NAMESPACE_BACKFILL_BATCH
                    c.execute(
                        f"INSERT INTO memnos_control.namespaces(name, auto_registered) "
                        f"SELECT DISTINCT namespace, true FROM tenant_memnos.{table} "
                        f"WHERE id > %s AND id <= %s "
                        f"ON CONFLICT (name) DO NOTHING",
                        (start, end))
                    start = end
            c.execute(
                "INSERT INTO memnos_control.namespace_registry_backfill DEFAULT VALUES "
                "ON CONFLICT (id) DO NOTHING")
        finally:
            c.execute(f"SET statement_timeout = {prior_timeout_ms}")

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

    # --- approved-deviation log (issue #106) --------------------------------
    @staticmethod
    def corpus_deviation_record(conn, namespace, constraint_id, rationale, approved_by,
                                 until=None, created_by=None):
        """Insert an approved-deviation row. Caller (memnos_server.py) must already have
        verified `constraint_id` exists, is kind='constraint', and belongs to `namespace`
        — this just records the decision, no FK (see corpus_deviations' DDL comment)."""
        with conn.cursor() as c:
            c.execute("""INSERT INTO memnos_control.corpus_deviations
                         (namespace, constraint_id, rationale, approved_by, until, created_by)
                         VALUES(%s,%s,%s,%s,%s,%s)
                         RETURNING id, namespace, constraint_id, rationale, approved_by, until, created_at""",
                      (namespace, constraint_id, rationale, approved_by, until, created_by))
            return c.fetchone()

    @staticmethod
    def corpus_descendants(conn, namespace):
        """issue #107 — DESCENDANT namespaces (':'-prefix children/grandchildren/...,
        SAME direction Control.namespace_ancestors walks in reverse) that already have
        their OWN ingested corpus docs. Drives the propagation-alert event fired from
        /corpus/ingest: when an org-level namespace's constraints are added/updated,
        every project namespace underneath it that has its own corpus (and therefore
        presumably relies on `corpus_check`/`corpus_check_diff` gating its own code) is a
        plausible audience for "a rule above you just changed — go re-check."

        Pure prefix match (`namespace LIKE namespace || ':%'`), same semantics as
        BrainStore._is_ancestor_ns — no recursive CTE needed since ':' segments are
        already a flat, closed-form hierarchy. One row per descendant namespace with at
        least one corpus_sources row, carrying how many docs it holds."""
        with conn.cursor() as c:
            c.execute("SELECT namespace, count(*) AS docs "
                      "FROM memnos_control.corpus_sources "
                      "WHERE namespace LIKE %s ESCAPE '\\' "
                      "GROUP BY namespace ORDER BY namespace",
                      (namespace.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",))
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
        # the principal's writable namespaces (can_write, direct OR role-inherited --
        # issue #81), excluding the write target.
        writable = [g["namespace"] for g in Control.effective_namespaces(conn, principal_id)
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


    @staticmethod
    def entity_dossier_candidates(conn, schema, namespace, min_mentions=3) -> list[dict]:
        """Return entities with at least min_mentions mentions in the given namespace,
        ordered by mention count descending (issue #23). Used by the /consolidate
        handler to decide which entities to generate dossiers for.
        Returns: list of {"entity_id": int, "name": str, "mention_count": int}."""
        with conn.cursor() as c:
            c.execute(
                f"SELECT e.id AS entity_id, e.name, count(m.memory_id) AS mention_count "
                f"FROM {schema}.entities e "
                f"JOIN {schema}.mentions m ON m.entity_id = e.id "
                f"WHERE e.namespace=%s "
                f"GROUP BY e.id, e.name "
                f"HAVING count(m.memory_id) >= %s "
                f"ORDER BY mention_count DESC",
                (namespace, min_mentions))
            rows = c.fetchall()
        return [{"entity_id": r["entity_id"], "name": r["name"],
                 "mention_count": int(r["mention_count"])} for r in rows]

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
        """A grant on the exact namespace, a parent prefix ('team:eng:*'), or '*' (admin) --
        from the principal's own direct grant OR any role it's a member of (issue #81,
        resolved via effective_namespaces())."""
        col = "can_write" if write else "can_read"
        for g in Control.effective_namespaces(conn, principal_id):
            if not g[col]:
                continue
            gns = g["namespace"]
            if gns == "*" or gns == namespace:
                return True
            if gns.endswith(":*") and namespace.startswith(gns[:-1]):
                return True
        return False

    @staticmethod
    def authorized_namespaces(conn, principal_id):
        """This principal's OWN DIRECT grants only (memnos_control.grants) -- the
        management accessor backing the admin API's GET /admin/grants and `memnos grant
        ls`. Deliberately does NOT blend in role-inherited grants: its paired mutator
        revoke_grant() only deletes from `grants`, so a row here is always exactly what a
        `grant rm` on this principal can remove. For the ENFORCEMENT-path view that
        includes role-inherited access (what a principal can actually do), see
        effective_namespaces()."""
        with conn.cursor() as c:
            c.execute("SELECT namespace, can_read, can_write FROM memnos_control.grants WHERE principal_id=%s",
                      (principal_id,))
            return c.fetchall()

    @staticmethod
    def effective_namespaces(conn, principal_id):
        """ALL namespace ACL entries visible to this principal: its own direct grants
        (memnos_control.grants) UNIONED with the grants of every role it's a member of
        (memnos_control.role_grants via role_members) -- issue #81. This is the
        ENFORCEMENT-path resolver: authorize(), readable_namespaces(),
        writable_namespaces(), and is_admin() all call this (never `grants` directly) so
        role support composes everywhere for free instead of being re-implemented per
        call site.

        Aggregated per namespace (bool_or) so a namespace granted BOTH directly (e.g.
        read-only) and via a role (e.g. read+write) yields ONE row with the union of
        permissions -- a direct grant and a role grant on the same namespace compose
        additively, never conflict.

        NOT the same as authorized_namespaces() -- see that method's docstring for why
        the management accessor stays direct-grants-only."""
        with conn.cursor() as c:
            c.execute("""
                SELECT namespace, bool_or(can_read) AS can_read, bool_or(can_write) AS can_write
                FROM (
                    SELECT namespace, can_read, can_write
                    FROM memnos_control.grants WHERE principal_id=%s
                    UNION ALL
                    SELECT rg.namespace, rg.can_read, rg.can_write
                    FROM memnos_control.role_grants rg
                    JOIN memnos_control.role_members rm ON rm.role_id = rg.role_id
                    WHERE rm.principal_id = %s
                ) x
                GROUP BY namespace
            """, (principal_id, principal_id))
            return c.fetchall()

    @staticmethod
    def writable_namespaces(conn, principal_id, limit=10):
        """Concrete namespaces this principal may WRITE to, capped at `limit`.
        Exact grants are included directly (even if the namespace has no data yet).
        Wildcard grants ('*' or 'prefix:*') are expanded against existing namespaces.
        Used to populate the suggestion in write-403 responses.

        NOTE: still does the DISTINCT-scan readable_namespaces() used to do before issue
        #41 fix A. Deliberately not converted to the memnos_control.namespaces registry in
        that fix — this only runs on a write-403 (cold path), not on every wide recall, so
        it wasn't the timeout source and touching it wasn't needed to close the issue.

        Uses effective_namespaces() (issue #81) so a role's write grant (direct or
        wildcard) is included, not just the principal's own direct grants."""
        grants = [g for g in Control.effective_namespaces(conn, principal_id) if g["can_write"]]
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
    def readable_namespaces(conn, principal_id, exclude=None, limit=None):
        """Concrete namespaces this principal may READ, optionally capped at `limit`.
        Exact grants are included directly (even if the namespace has no data yet).
        Wildcard grants ('*' or 'prefix:*') are expanded against existing namespaces.
        Pass exclude=<namespace> to omit the current query namespace from the result
        (used to populate other_readable_namespaces in /recall scope metadata).
        limit=None means no cap (backward-compatible default for wide recall).

        This is the fan-out driver for WIDE recall (memnos_server.py: `nss =
        readable_namespaces(...)` feeds recall_wide_fetch/namespaces_searched directly),
        not just a display hint — so the wildcard-expansion source below must stay a
        COMPLETE index of every namespace that has data.

        issue #41 fix A: wildcard grants used to expand against two full-table DISTINCT
        scans over tenant_memnos.raw_turns/semantic, which blew the 15s statement_timeout
        cold/under load on every wide recall for a wildcard-grant principal (e.g. the
        admin '*' token). memnos_control.namespaces is now kept COMPLETE as a side effect
        of every write (core/store.py insert_raw_turn upserts it on a namespace's first
        turn) plus a one-time boot-time backfill for pre-existing data (Control.init ->
        _run_namespace_registry_backfill) — so wildcard expansion reads that small registry
        table instead of scanning the data tables. See memnos_control.namespaces' DDL
        comment (CONTROL_DDL, near auto_registered) for how completeness is maintained.

        NOTE: writable_namespaces() just above still does the old DISTINCT-scan — it is
        deliberately NOT touched by issue #41 (out of scope for this fix; write-403
        suggestions are a much colder path than every wide recall).

        Uses effective_namespaces() (issue #81) so a role's read grant (direct or
        wildcard) is included in the wide-recall fan-out, not just the principal's own
        direct grants."""
        grants = [g for g in Control.effective_namespaces(conn, principal_id) if g["can_read"]]
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
                c.execute("SELECT name FROM memnos_control.namespaces")
                existing = [r["name"] for r in c.fetchall()]
            for ns in existing:
                for p in wildcard_patterns:
                    if p == "*" or (p.endswith(":*") and ns.startswith(p[:-1])):
                        result.add(ns)
                        break
        if exclude is not None:
            result.discard(exclude)
        out = sorted(result)
        return out[:limit] if limit is not None else out

    @staticmethod
    def is_admin(conn, principal_id) -> bool:
        """Admin = holds the '*' grant, directly OR via a role membership (issue #81) --
        kept consistent with authorize(), which already resolves role grants. Gates the
        management console endpoints. Role management is direct-DB access (same trust
        boundary as `memnos grant add <p> '*'`), so this adds no new privilege-escalation
        path."""
        if principal_id is None:
            return False
        with conn.cursor() as c:
            c.execute("""
                SELECT 1 FROM memnos_control.grants
                WHERE principal_id=%s AND namespace='*' AND can_read
                UNION ALL
                SELECT 1 FROM memnos_control.role_grants rg
                JOIN memnos_control.role_members rm ON rm.role_id = rg.role_id
                WHERE rm.principal_id=%s AND rg.namespace='*' AND rg.can_read
                LIMIT 1
            """, (principal_id, principal_id))
            return c.fetchone() is not None

    # --- namespace registry (explicit, user-created) ----------------------
    @staticmethod
    def create_namespace(conn, name, created_by=None, description=None):
        """Register a namespace + grant its creator read/write. Idempotent on name.
        Explicit registration always claims/reclaims auto_registered=false — even if a
        write already auto-registered this name first, an admin explicitly creating it
        afterward should clear the UI's "discovered" pill (ui/app.js)."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespaces(name,created_by,description,auto_registered) "
                      "VALUES(%s,%s,%s,false) ON CONFLICT (name) DO UPDATE "
                      "SET description=EXCLUDED.description, auto_registered=false",
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
    def set_namespace_inherit_ancestors(conn, name, inherit: bool):
        """Opt a namespace in/out of Mechanism A's automatic same-root ancestor
        consultation (issue #85). Registers the namespace if needed, same pattern as
        set_namespace_kind."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespaces(name, inherit_ancestors) VALUES(%s,%s) "
                      "ON CONFLICT (name) DO UPDATE SET inherit_ancestors=EXCLUDED.inherit_ancestors",
                      (name, bool(inherit)))

    _LINK_KINDS = ("link", "inherits", "governed_by")

    @staticmethod
    def link_namespaces(conn, src, dst, created_by=None, kind="link"):
        """Declare that recall on `src` should also be GROUNDED in `dst` (policy only —
        each caller still needs a read grant on `dst` for the fan-out to happen). `kind`
        (issue #85) is informational taxonomy only — every existing caller keeps getting
        the default 'link' (today's grounding semantics); 'inherits'/'governed_by' let a
        NEW explicit cross-root link record intent for humans reading `namespace links`.
        No runtime behavior currently branches on kind."""
        if src == dst:
            raise ValueError("src and dst must differ")
        if kind not in Control._LINK_KINDS:
            raise ValueError(f"kind must be one of {Control._LINK_KINDS}")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespace_links(src_ns,dst_ns,created_by,kind) "
                      "VALUES(%s,%s,%s,%s) ON CONFLICT (src_ns,dst_ns) DO NOTHING", (src, dst, created_by, kind))

    @staticmethod
    def unlink_namespaces(conn, src, dst) -> bool:
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s AND dst_ns=%s",
                      (src, dst))
            return c.rowcount > 0

    @staticmethod
    def linked_namespaces(conn, src):
        """The dst namespaces linked FROM `src` (recall fan-out targets), stable order.
        Single-hop by design (issue #85): a link is an explicit, deliberate act per pair;
        the epic's framing never demanded transitive closure for explicit cross-root
        links, unlike Mechanism A's same-root walk-up (which IS multi-level, but derived
        from ':'-prefix structure, not from chasing this table recursively)."""
        with conn.cursor() as c:
            c.execute("SELECT dst_ns FROM memnos_control.namespace_links WHERE src_ns=%s "
                      "ORDER BY dst_ns", (src,))
            return [r["dst_ns"] for r in c.fetchall()]

    @staticmethod
    def list_links(conn, src=None):
        with conn.cursor() as c:
            if src:
                c.execute("SELECT l.src_ns, l.dst_ns, l.kind, l.created_at, p.name AS created_by "
                          "FROM memnos_control.namespace_links l "
                          "LEFT JOIN memnos_control.principals p ON p.id=l.created_by "
                          "WHERE l.src_ns=%s ORDER BY l.dst_ns", (src,))
            else:
                c.execute("SELECT l.src_ns, l.dst_ns, l.kind, l.created_at, p.name AS created_by "
                          "FROM memnos_control.namespace_links l "
                          "LEFT JOIN memnos_control.principals p ON p.id=l.created_by "
                          "ORDER BY l.src_ns, l.dst_ns")
            return c.fetchall()

    # --- same-root automatic ancestor inheritance (issue #85, epic #70 Mechanism A) -----
    @staticmethod
    def namespace_ancestors(ns):
        """Pure string derivation of `ns`'s ':'-prefix ancestors, NEAREST-FIRST, ALL
        levels (multi-hop is free here — it's a closed-form split, not a graph walk):
        'customer:example:widgets' -> ['customer:example', 'customer']. No DB access, no
        recursion needed. Deliberately prefix-only: 'customer:a:widgets' and
        'customer:b:widgets' share a leaf segment but NEITHER is a prefix of the other,
        so this never treats them as related (issue #85 acceptance criterion)."""
        parts = (ns or "").split(":")
        return [":".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]

    @staticmethod
    def namespace_inherits_ancestors(conn, ns) -> bool:
        """The opt-out flag (namespaces.inherit_ancestors), COALESCE-safe: a namespace
        with NO registry row (never explicitly registered, e.g. one that only exists via
        grants/data) has never opted out, so missing-row MUST read as True, matching how
        list_namespaces() already treats a missing kind as its default via COALESCE."""
        with conn.cursor() as c:
            c.execute("SELECT inherit_ancestors FROM memnos_control.namespaces WHERE name=%s", (ns,))
            row = c.fetchone()
            return True if row is None else bool(row["inherit_ancestors"])

    @staticmethod
    def effective_ancestors(conn, ns):
        """Mechanism A's actual consultation list: [] if `ns` opted out (a property of
        the CONSULTING namespace — opting out suppresses ALL levels at once, there is no
        per-ancestor-pair opt-out), else every same-root ancestor, nearest-first.

        Checks namespace_ancestors() FIRST (pure string, no DB) and short-circuits on []
        before ever querying the opt-out flag: a single-segment namespace has nothing to
        inherit regardless of that flag's value, so there is no reason to pay a DB round
        trip (or be exposed to a failure on memnos_control.namespaces) for a namespace
        that could never have anything to consult anyway."""
        anc = Control.namespace_ancestors(ns)
        if not anc:
            return []
        if not Control.namespace_inherits_ancestors(conn, ns):
            return []
        return anc

    @staticmethod
    def list_namespaces(conn):
        """ALL real namespaces: the explicit registry UNION any namespace that has data
        (raw_turns/semantic) or a concrete grant — so implicitly-created namespaces (e.g.
        from hooks) still show. `registered` flags whether a human explicitly created it
        (create_namespace, via the UI/CLI) as opposed to it only being present because a
        write auto-registered it (auto_registered=true) — that distinction drives the
        "discovered" pill in ui/app.js, so a namespace that only self-registered on write
        must still read as unregistered here even though it now has a memnos_control.
        namespaces row (issue #41 made every namespace with data get one).

        issue #114: the grants-sourced branch below excludes `secret:`-prefixed rows --
        those are secret-resolve pseudo-namespace grants (SECRET_NS_PREFIX, see the module
        docstring above Control), not real memory namespaces, and must never appear here
        as a browsable/creatable-looking namespace in the console or `memnos namespace ls`
        (they have no data and never will -- Vault storage is a separate table)."""
        with conn.cursor() as c:
            c.execute("""
                WITH names AS (
                    SELECT name FROM memnos_control.namespaces
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.raw_turns
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic
                    UNION SELECT DISTINCT namespace FROM memnos_control.grants
                      WHERE namespace <> '*' AND namespace NOT LIKE '%*'
                        AND namespace NOT LIKE 'secret:%'
                )
                SELECT nm.name, n.description, n.created_at, p.name AS created_by,
                  COALESCE(n.kind, 'memory') AS kind,
                  COALESCE(n.inherit_ancestors, true) AS inherit_ancestors,
                  COALESCE(rt.cnt,0) AS turns, COALESCE(sm.cnt,0) AS facts,
                  (n.name IS NOT NULL AND NOT COALESCE(n.auto_registered, false)) AS registered
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
    def namespace_prune_candidates(conn, empty=True, stale_days=None, stale_max_facts=20):
        """Candidates for `memnos namespace prune` (issue #30): the same universe as
        list_namespaces, filtered to `empty` (0 turns AND 0 facts — safe, no data to lose)
        and/or `stale_days` (last write older than N days AND a small (<=stale_max_facts)
        fact count — holds SOME data, caller must gate deletion behind --force). Each row
        also carries `bound`: True if a bindings row currently routes some repo/host to
        this namespace — delete_namespace() always revokes grants (even without
        purge_data), so deleting a bound namespace would 403 that binding's next write;
        the caller should skip these unless the user explicitly forces past them.

        issue #114: same `secret:`-prefix exclusion as list_namespaces() (see that
        docstring, and SECRET_NS_PREFIX above Control) -- WITHOUT it, a `secret:NAME`
        pseudo-namespace grant would look exactly like an empty, safe-to-delete memory
        namespace (0 turns, 0 facts) and `memnos namespace prune --empty` would silently
        revoke a live secret-resolve grant via delete_namespace()'s unconditional
        `DELETE FROM grants WHERE namespace=...`."""
        with conn.cursor() as c:
            c.execute("""
                WITH names AS (
                    SELECT name FROM memnos_control.namespaces
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.raw_turns
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic
                    UNION SELECT DISTINCT namespace FROM memnos_control.grants
                      WHERE namespace <> '*' AND namespace NOT LIKE '%%*'
                        AND namespace NOT LIKE 'secret:%%'
                ), stats AS (
                    SELECT nm.name,
                      COALESCE(rt.cnt,0) AS turns, COALESCE(sm.cnt,0) AS facts,
                      GREATEST(rt.last, sm.last) AS last_write,
                      EXISTS(SELECT 1 FROM memnos_control.bindings b WHERE b.namespace=nm.name) AS bound
                    FROM names nm
                    LEFT JOIN (SELECT namespace, count(*) cnt, max(observed_at) last
                               FROM tenant_memnos.raw_turns GROUP BY namespace) rt ON rt.namespace=nm.name
                    LEFT JOIN (SELECT namespace, count(*) cnt, max(created_at) last
                               FROM tenant_memnos.semantic GROUP BY namespace) sm ON sm.namespace=nm.name
                )
                SELECT name, turns, facts, last_write, bound,
                  (turns=0 AND facts=0) AS is_empty,
                  (%(stale_days)s::int IS NOT NULL AND last_write IS NOT NULL
                   AND facts <= %(stale_max_facts)s
                   AND last_write < now() - (%(stale_days)s::int || ' days')::interval) AS is_stale
                FROM stats
                WHERE (%(empty)s AND turns=0 AND facts=0)
                   OR (%(stale_days)s::int IS NOT NULL AND last_write IS NOT NULL
                       AND facts <= %(stale_max_facts)s
                       AND last_write < now() - (%(stale_days)s::int || ' days')::interval)
                ORDER BY name""",
                {"empty": empty, "stale_days": stale_days, "stale_max_facts": stale_max_facts})
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

    # --- enforced constraints (issue #28) -----------------------------------
    @staticmethod
    def add_constraint_enforcement(conn, namespace, rule_text, enforce_level, tool_matcher,
                                   created_by=None):
        """Register an ask/block enforcement rule (control-plane only — the pinned advisory
        memory, if any, is written separately via the normal /remember path). Raises
        ValueError on a level/matcher combination that can never be evaluated deterministically
        — reject, don't silently write an inert rule the caller believes is enforced."""
        if enforce_level not in ("ask", "block"):
            raise ValueError("enforce_level must be 'ask' or 'block' — 'advise' constraints are "
                             "plain pinned memories (memnos remember <rule> --type constraint), "
                             "not control-plane rows")
        if not tool_matcher:
            raise ValueError("--tool is required for --enforce ask|block: a prose rule can't be "
                             "matched to a tool call deterministically without an LLM, and "
                             "enforcement is LLM-free by design")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.constraint_enforcement"
                      "(namespace,rule_text,enforce_level,tool_matcher,created_by) "
                      "VALUES(%s,%s,%s,%s,%s) RETURNING id",
                      (namespace, rule_text, enforce_level, tool_matcher, created_by))
            return c.fetchone()["id"]

    @staticmethod
    def list_constraint_enforcement(conn, namespace=None, active_only=True):
        with conn.cursor() as c:
            q = ("SELECT id, namespace, rule_text, enforce_level, tool_matcher, created_at, active "
                 "FROM memnos_control.constraint_enforcement WHERE true")
            params = []
            if namespace:
                q += " AND namespace=%s"; params.append(namespace)
            if active_only:
                q += " AND active"
            q += " ORDER BY namespace, id"
            c.execute(q, params)
            return c.fetchall()

    @staticmethod
    def remove_constraint_enforcement(conn, row_id) -> bool:
        """Soft-delete (active=false) rather than DELETE — keeps a governance trail of what
        WAS enforced, matching the audit-everything design goal."""
        with conn.cursor() as c:
            c.execute("UPDATE memnos_control.constraint_enforcement SET active=false "
                      "WHERE id=%s AND active", (row_id,))
            return c.rowcount > 0

    @staticmethod
    def list_constraint_enforcement_fanout(conn, ns):
        """issue #85 item 4: the REAL set of active ask/block rules that govern `ns` —
        its own rows PLUS Mechanism A's same-root ancestors PLUS Mechanism B's explicit
        namespace_links targets. Before this, `_refresh_enforce_cache` (memnos_cli.py)
        called list_constraint_enforcement(namespace=ns) directly, an EXACT-namespace
        filter — so an ask/block rule written on a parent or linked namespace was
        invisible to the PreToolUse hook's cache and to `hook status`'s loaded-count,
        even though the SAME rule's advisory (pinned-memory) form already flows correctly
        through /recall via pin_nss. This closes that gap.

        No grant/authorize() gate here (unlike the /recall grounded-recall branch): this
        runs from `_refresh_enforce_cache`, which has no principal/token context at all —
        it's a direct-DSN, single-trusted-config CLI path, same trust model as every other
        `memnos namespace`/`hook status` verb that talks straight to Postgres.

        Dedup by id (a rule could in principle be reachable via both an ancestor AND a
        link — e.g. ns links to its own parent). Order: own rules first, then ancestors
        nearest-first, then links — informational only; the PreToolUse matcher doesn't
        care about order, it just needs a level match."""
        seen_ids, out = set(), []
        for src_ns in [ns] + Control.effective_ancestors(conn, ns) + Control.linked_namespaces(conn, ns):
            for r in Control.list_constraint_enforcement(conn, namespace=src_ns):
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                out.append(r)
        return out

    # --- copy provenance (issue #85 item 5) ---------------------------------
    @staticmethod
    def record_namespace_copy(conn, dst_ns, src_ns, mode, copied_by=None):
        """One append-only row per copy_memories_from / `namespace copy|move` call.
        Provenance only — no staleness signal is computed here (see the DDL comment on
        namespace_copy_provenance for why that's deliberately out of scope for this PR)."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.namespace_copy_provenance"
                      "(dst_ns,src_ns,mode,copied_by) VALUES(%s,%s,%s,%s) RETURNING id",
                      (dst_ns, src_ns, mode, copied_by))
            return c.fetchone()["id"]

    @staticmethod
    def list_namespace_copy_provenance(conn, ns=None):
        """Provenance rows touching `ns` (as either side), newest first; all rows if ns
        is None."""
        with conn.cursor() as c:
            if ns:
                c.execute("SELECT id, dst_ns, src_ns, mode, copied_by, copied_at "
                          "FROM memnos_control.namespace_copy_provenance "
                          "WHERE dst_ns=%s OR src_ns=%s ORDER BY copied_at DESC", (ns, ns))
            else:
                c.execute("SELECT id, dst_ns, src_ns, mode, copied_by, copied_at "
                          "FROM memnos_control.namespace_copy_provenance ORDER BY copied_at DESC")
            return c.fetchall()

    # --- constraint precedence overrides (issue #83) ------------------------
    @staticmethod
    def add_constraint_override(conn, child_namespace, parent_namespace, created_by=None):
        """Declare that CHILD wins a precedence conflict against PARENT instead of the
        default (parent wins — see BrainStore.resolve_constraint_precedence). Raises
        ValueError unless child is genuinely a ':'-prefix descendant of parent — an
        override between unrelated namespaces has no default to reverse, so it can
        never mean anything to the precedence algorithm; reject it rather than accept
        a row that silently never fires."""
        if not child_namespace or not parent_namespace:
            raise ValueError("child_namespace and parent_namespace are both required")
        if not child_namespace.startswith(parent_namespace + ":"):
            raise ValueError(f"{child_namespace!r} is not a ':'-prefix descendant of "
                             f"{parent_namespace!r} — override edges only apply between "
                             "namespaces that are already in a default parent/child "
                             "precedence relationship")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.constraint_overrides"
                      "(child_namespace,parent_namespace,created_by) VALUES(%s,%s,%s) "
                      "ON CONFLICT (child_namespace,parent_namespace) DO NOTHING RETURNING id",
                      (child_namespace, parent_namespace, created_by))
            row = c.fetchone()
            if row:
                return row["id"]
            c.execute("SELECT id FROM memnos_control.constraint_overrides "
                      "WHERE child_namespace=%s AND parent_namespace=%s",
                      (child_namespace, parent_namespace))
            return c.fetchone()["id"]

    @staticmethod
    def list_constraint_overrides(conn, namespace=None):
        with conn.cursor() as c:
            if namespace:
                c.execute("SELECT id, child_namespace, parent_namespace, created_at "
                          "FROM memnos_control.constraint_overrides "
                          "WHERE child_namespace=%s OR parent_namespace=%s ORDER BY id",
                          (namespace, namespace))
            else:
                c.execute("SELECT id, child_namespace, parent_namespace, created_at "
                          "FROM memnos_control.constraint_overrides ORDER BY id")
            return c.fetchall()

    @staticmethod
    def remove_constraint_override(conn, row_id) -> bool:
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.constraint_overrides WHERE id=%s", (row_id,))
            return c.rowcount > 0

    @staticmethod
    def get_constraint_overrides(conn, namespaces) -> set:
        """Resolve override edges touching ANY of `namespaces` into a plain
        {(child_namespace, parent_namespace), ...} set — pure data handed to
        BrainStore.resolve_constraint_precedence. Keeps every memnos_control SQL
        statement inside Control; BrainStore never queries memnos_control directly for
        this (the one pre-existing exception — the namespace-registry upsert in
        insert_raw_turn — is a narrow, unrelated, already-established case)."""
        nss = list(namespaces or [])
        if not nss:
            return set()
        with conn.cursor() as c:
            c.execute("SELECT child_namespace, parent_namespace "
                      "FROM memnos_control.constraint_overrides "
                      "WHERE child_namespace = ANY(%s) OR parent_namespace = ANY(%s)",
                      (nss, nss))
            return {(r["child_namespace"], r["parent_namespace"]) for r in c.fetchall()}

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

    # --- roles (issue #81) --------------------------------------------------
    @staticmethod
    def create_role(conn, name, description=None, created_by=None) -> int:
        """Register a role (idempotent on name — a repeat create just updates the
        description, same UPSERT shape as create_principal())."""
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.roles(name,description,created_by) "
                      "VALUES(%s,%s,%s) ON CONFLICT (name) DO UPDATE "
                      "SET description=COALESCE(EXCLUDED.description, memnos_control.roles.description) "
                      "RETURNING id", (name, description, created_by))
            return c.fetchone()["id"]

    @staticmethod
    def list_roles(conn):
        """All roles with member/grant counts, for `memnos role ls`."""
        with conn.cursor() as c:
            c.execute("""
                SELECT r.id, r.name, r.description, r.created_at, p.name AS created_by,
                  count(DISTINCT rm.principal_id) AS member_count,
                  count(DISTINCT rg.namespace) AS grant_count
                FROM memnos_control.roles r
                LEFT JOIN memnos_control.principals p ON p.id = r.created_by
                LEFT JOIN memnos_control.role_members rm ON rm.role_id = r.id
                LEFT JOIN memnos_control.role_grants rg ON rg.role_id = r.id
                GROUP BY r.id, p.name
                ORDER BY r.name""")
            return c.fetchall()

    @staticmethod
    def role_id(conn, name):
        """Role name -> id, or None if no such role."""
        with conn.cursor() as c:
            c.execute("SELECT id FROM memnos_control.roles WHERE name=%s", (name,))
            r = c.fetchone()
            return r["id"] if r else None

    @staticmethod
    def delete_role(conn, name) -> bool:
        """Delete a role and its grants + memberships. Explicit child-row deletes (no
        ON DELETE CASCADE — this codebase has no precedent for it; delete_namespace()
        follows the same explicit-multi-statement pattern) in dependency order:
        role_grants/role_members (reference roles.id) before roles itself."""
        rid = Control.role_id(conn, name)
        if rid is None:
            return False
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.role_grants WHERE role_id=%s", (rid,))
            c.execute("DELETE FROM memnos_control.role_members WHERE role_id=%s", (rid,))
            c.execute("DELETE FROM memnos_control.roles WHERE id=%s", (rid,))
        return True

    @staticmethod
    def grant_role(conn, role_name, namespace, can_read=True, can_write=True):
        """Grant a role access to a namespace — the exact/prefix('team:*')/'*' wildcard
        matching authorize() already applies to per-principal grants applies identically
        here once resolved through effective_namespaces() (issue #81)."""
        rid = Control.role_id(conn, role_name)
        if rid is None:
            raise ValueError(f"no role '{role_name}'")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.role_grants(role_id,namespace,can_read,can_write) "
                      "VALUES(%s,%s,%s,%s) ON CONFLICT (role_id,namespace) "
                      "DO UPDATE SET can_read=EXCLUDED.can_read, can_write=EXCLUDED.can_write",
                      (rid, namespace, can_read, can_write))

    @staticmethod
    def revoke_role_grant(conn, role_name, namespace):
        rid = Control.role_id(conn, role_name)
        if rid is None:
            return
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.role_grants WHERE role_id=%s AND namespace=%s",
                      (rid, namespace))

    @staticmethod
    def list_role_grants(conn, role_name):
        rid = Control.role_id(conn, role_name)
        if rid is None:
            return []
        with conn.cursor() as c:
            c.execute("SELECT namespace, can_read, can_write FROM memnos_control.role_grants "
                      "WHERE role_id=%s ORDER BY namespace", (rid,))
            return c.fetchall()

    @staticmethod
    def add_role_member(conn, role_name, principal_id):
        rid = Control.role_id(conn, role_name)
        if rid is None:
            raise ValueError(f"no role '{role_name}'")
        with conn.cursor() as c:
            c.execute("INSERT INTO memnos_control.role_members(role_id,principal_id) "
                      "VALUES(%s,%s) ON CONFLICT (role_id,principal_id) DO NOTHING",
                      (rid, principal_id))

    @staticmethod
    def remove_role_member(conn, role_name, principal_id) -> bool:
        """Remove one principal's membership only — other members of the same role are
        untouched (issue #81: revoking one member's access must not affect the rest)."""
        rid = Control.role_id(conn, role_name)
        if rid is None:
            return False
        with conn.cursor() as c:
            c.execute("DELETE FROM memnos_control.role_members WHERE role_id=%s AND principal_id=%s",
                      (rid, principal_id))
            return c.rowcount > 0

    @staticmethod
    def list_role_members(conn, role_name):
        rid = Control.role_id(conn, role_name)
        if rid is None:
            return []
        with conn.cursor() as c:
            c.execute("SELECT p.id, p.name, p.kind FROM memnos_control.role_members rm "
                      "JOIN memnos_control.principals p ON p.id = rm.principal_id "
                      "WHERE rm.role_id=%s ORDER BY p.name", (rid,))
            return c.fetchall()

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
