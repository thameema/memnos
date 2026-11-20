//! Postgres control plane (`memnos_control` schema): identity, token auth, namespace ACL + roles,
//! audit/usage ledger, leases, named sessions, channel pub/sub, competing-consumer task claiming,
//! agent inbox and webhook push. Same schema and wire shapes as the Python engine, so a Rust
//! server and a Python server can share one database.
//!
//! Every public function takes a `&Pool` and returns `serde_json::Value` (or plain types) so HTTP
//! and CLI layers can serialize responses directly. Internally, functions that others compose
//! have an `*_on` variant over `tokio_postgres::GenericClient` so they run inside one transaction.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD as B64URL, Engine as _};
use chrono::{DateTime, NaiveDate, NaiveDateTime, SecondsFormat, Utc};
use deadpool_postgres::{Object, Pool};
use hmac::{Hmac, Mac};
use once_cell::sync::Lazy;
use rand::RngCore;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use tokio_postgres::types::{ToSql, Type};
use tokio_postgres::{GenericClient, Row};

use crate::nsresolve;

/// Pseudo-namespace prefix for secret-resolve grants (`secret:NAME` / `secret:*`).
pub const SECRET_NS_PREFIX: &str = "secret:";

pub const INTENTS: [&str; 9] = [
    "message",
    "status",
    "task",
    "review_request",
    "critique",
    "question",
    "answer",
    "handoff",
    "done",
];
const PRIORITIES: [&str; 4] = ["urgent", "high", "normal", "low"];
const INBOX_LIVE: [&str; 3] = ["pending", "read", "in_progress"];
const LINK_KINDS: [&str; 3] = ["link", "inherits", "governed_by"];
const NAMESPACE_BACKFILL_BATCH: i64 = 50_000;

#[derive(Debug, thiserror::Error)]
pub enum ControlError {
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    Unauthorized(String),
    #[error("{0}")]
    Forbidden(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    Conflict(String),
    #[error("{0}")]
    Db(String),
}

impl From<tokio_postgres::Error> for ControlError {
    fn from(e: tokio_postgres::Error) -> Self {
        // surface the server-side message (tokio_postgres' Display is just "db error")
        match e.as_db_error() {
            Some(d) => ControlError::Db(format!("{}: {}", d.code().code(), d.message())),
            None => ControlError::Db(e.to_string()),
        }
    }
}

impl From<deadpool_postgres::PoolError> for ControlError {
    fn from(e: deadpool_postgres::PoolError) -> Self {
        ControlError::Db(format!("pool: {e}"))
    }
}

pub type CResult<T> = Result<T, ControlError>;

fn bad<T>(msg: impl Into<String>) -> CResult<T> {
    Err(ControlError::BadRequest(msg.into()))
}

macro_rules! params {
    ($($x:expr),* $(,)?) => {
        &[$(&$x as &(dyn ToSql + Sync)),*] as &[&(dyn ToSql + Sync)]
    };
}

type Arg = Box<dyn ToSql + Sync + Send>;
fn arg_refs(a: &[Arg]) -> Vec<&(dyn ToSql + Sync)> {
    a.iter().map(|b| &**b as &(dyn ToSql + Sync)).collect()
}

async fn conn(pool: &Pool) -> CResult<Object> {
    Ok(pool.get().await?)
}

// ---------------------------------------------------------------- pure helpers

/// SHA-256 hex of a bearer token (only the hash is ever stored).
pub fn hash_token(token: &str) -> String {
    hex::encode(Sha256::digest(token.as_bytes()))
}

static NAME_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$").unwrap());
static CH_SIMPLE_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[#@][A-Za-z0-9._-]{1,64}$").unwrap());
static CH_STATUS_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^status:[A-Za-z0-9._-]{1,64}$").unwrap());
static CH_PREFIX_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^#[A-Za-z0-9._-]{1,63}\*$").unwrap());
static TOPIC_OK_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^#?[A-Za-z0-9._-]{1,64}$").unwrap());
static TOPIC_BAD_RUN_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[^A-Za-z0-9._-]+").unwrap());
static TOKEN_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[a-z0-9]{2,}").unwrap());

/// (machine_id, hostname) of THIS machine, computed once (machine_id() touches the filesystem).
static LOCAL: Lazy<(String, String)> =
    Lazy::new(|| (nsresolve::machine_id(), nsresolve::hostname()));

/// Validate a session/owner name: `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` (leading `@` tolerated).
pub fn slug(name: &str, what: &str) -> CResult<String> {
    let n = name.trim().trim_start_matches('@');
    if !NAME_RE.is_match(n) {
        return bad(format!(
            "{what} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        ));
    }
    Ok(n.to_string())
}

/// Normalize + validate a channel: `#topic`, `@session`, `status:<session>`, and (when
/// `allow_wild`) `*`, `status:*`, `#prefix*`. A bare word becomes `#word`.
pub fn validate_channel_opts(ch: &str, allow_wild: bool) -> CResult<String> {
    let mut c = ch.trim().to_string();
    if let Some(f) = c.chars().next() {
        if !"#@*".contains(f) && !c.starts_with("status:") {
            c = format!("#{c}");
        }
    }
    let ok = CH_SIMPLE_RE.is_match(&c)
        || CH_STATUS_RE.is_match(&c)
        || (allow_wild && (c == "*" || c == "status:*" || CH_PREFIX_RE.is_match(&c)));
    if !ok {
        return bad(format!(
            "invalid channel {ch:?} (use #topic, @session, status:<session>{})",
            if allow_wild {
                ", status:*, *, #prefix*"
            } else {
                ""
            }
        ));
    }
    Ok(c)
}

pub fn validate_channel(ch: &str) -> CResult<String> {
    validate_channel_opts(ch, true)
}

/// `'a:b:c'` -> `['a:b', 'a']` (nearest-first, pure string derivation, no DB).
pub fn namespace_ancestors(ns: &str) -> Vec<String> {
    let parts: Vec<&str> = ns.split(':').collect();
    (1..parts.len())
        .rev()
        .map(|i| parts[..i].join(":"))
        .collect()
}

/// Levenshtein distance, short-circuiting to 99 when lengths differ by more than 2.
pub fn edit_distance_1_or_2(a: &str, b: &str) -> usize {
    if a == b {
        return 0;
    }
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    if a.len().abs_diff(b.len()) > 2 {
        return 99;
    }
    let mut dp: Vec<usize> = (0..=b.len()).collect();
    for (i, ca) in a.iter().enumerate() {
        let mut nd = vec![i + 1];
        for (j, cb) in b.iter().enumerate() {
            let cost = usize::from(ca != cb);
            nd.push((nd[j] + 1).min(dp[j + 1] + 1).min(dp[j] + cost));
        }
        dp = nd;
    }
    dp[b.len()]
}

/// Natural-language topic -> `#slug` (`"payments sync"` -> `"#payments-sync"`). Returns
/// `(topic, slugified)`; already-valid topics pass through untouched.
pub fn normalize_topic(raw: &str) -> (String, bool) {
    let raw = raw.trim();
    if raw.is_empty() || TOPIC_OK_RE.is_match(raw) {
        return (raw.to_string(), false);
    }
    let lowered = raw.trim_start_matches('#').to_lowercase();
    let replaced = TOPIC_BAD_RUN_RE.replace_all(&lowered, "-");
    let s: String = replaced.trim_matches('-').chars().take(60).collect();
    if s.is_empty() {
        (raw.to_string(), false)
    } else {
        (format!("#{s}"), true)
    }
}

fn bound(what: &str, val: &str, n: usize) -> CResult<()> {
    if val.chars().count() > n {
        return bad(format!("{what} too long (max {n} chars)"));
    }
    Ok(())
}

fn bound_meta(m: &Value) -> CResult<()> {
    if m.to_string().len() > 65536 {
        return bad("metadata too large (max 65536 bytes)");
    }
    Ok(())
}

fn bound_channels(n: usize) -> CResult<()> {
    if n > 50 {
        return bad("too many channels (max 50)");
    }
    Ok(())
}

fn iso(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(SecondsFormat::Micros, false)
}

/// Convert one Postgres row to a JSON object (timestamps as ISO-8601, arrays/jsonb preserved).
/// `numeric` columns must be cast to `float8` in SQL.
pub fn row_to_json(row: &Row) -> Value {
    let mut m = Map::new();
    for (i, col) in row.columns().iter().enumerate() {
        m.insert(col.name().to_string(), cell(row, i, col.type_()));
    }
    Value::Object(m)
}

fn cell(row: &Row, i: usize, t: &Type) -> Value {
    macro_rules! g {
        ($ty:ty, $f:expr) => {
            match row.try_get::<_, Option<$ty>>(i) {
                Ok(Some(v)) => $f(v),
                _ => Value::Null,
            }
        };
    }
    if *t == Type::BOOL {
        g!(bool, Value::from)
    } else if *t == Type::INT2 {
        g!(i16, Value::from)
    } else if *t == Type::INT4 {
        g!(i32, Value::from)
    } else if *t == Type::INT8 {
        g!(i64, Value::from)
    } else if *t == Type::FLOAT4 {
        g!(f32, |v: f32| json!(v as f64))
    } else if *t == Type::FLOAT8 {
        g!(f64, |v: f64| json!(v))
    } else if *t == Type::TEXT || *t == Type::VARCHAR || *t == Type::NAME || *t == Type::BPCHAR {
        g!(String, Value::from)
    } else if *t == Type::JSON || *t == Type::JSONB {
        g!(Value, |v| v)
    } else if *t == Type::TIMESTAMPTZ {
        g!(DateTime<Utc>, |v| Value::from(iso(v)))
    } else if *t == Type::TIMESTAMP {
        g!(NaiveDateTime, |v: NaiveDateTime| Value::from(
            v.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
        ))
    } else if *t == Type::DATE {
        g!(NaiveDate, |v: NaiveDate| Value::from(v.to_string()))
    } else if *t == Type::TEXT_ARRAY || *t == Type::VARCHAR_ARRAY {
        g!(Vec<String>, Value::from)
    } else if *t == Type::INT8_ARRAY {
        g!(Vec<i64>, Value::from)
    } else {
        Value::Null
    }
}

fn rows_json(rows: &[Row]) -> Vec<Value> {
    rows.iter().map(row_to_json).collect()
}

fn obj(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

fn sget(m: &Map<String, Value>, k: &str) -> String {
    m.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn same_laptop(machine_id: &str, host: &str) -> bool {
    let (mid, hn) = &*LOCAL;
    let (mid, hn) = (mid.to_lowercase(), hn.to_lowercase());
    let (r_mid, r_host) = (machine_id.to_lowercase(), host.to_lowercase());
    (!r_mid.is_empty() && r_mid == mid) || (!r_host.is_empty() && (r_host == hn || r_host == mid))
}

fn pid_alive(pid: i64) -> bool {
    pid > 1 && pid <= u32::MAX as i64 && nsresolve::is_local_pid_alive(pid as u32)
}

/// HMAC-SHA256 hex of `body` under `secret` — the value after `sha256=` in `X-Memnos-Signature`.
pub fn sign_body(secret: &str, body: &[u8]) -> String {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("hmac accepts any key length");
    mac.update(body);
    hex::encode(mac.finalize().into_bytes())
}

// ---------------------------------------------------------------- DDL

pub const CONTROL_DDL: &str = r#"
CREATE SCHEMA IF NOT EXISTS memnos_control;
CREATE TABLE IF NOT EXISTS memnos_control.principals(
    id bigserial PRIMARY KEY, name text UNIQUE NOT NULL, kind text NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS memnos_control.api_tokens(
    id bigserial PRIMARY KEY, principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    token_hash text UNIQUE NOT NULL, label text, created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz, revoked boolean NOT NULL DEFAULT false);
ALTER TABLE memnos_control.api_tokens ADD COLUMN IF NOT EXISTS hint text;
CREATE INDEX IF NOT EXISTS tok_hash ON memnos_control.api_tokens(token_hash);
CREATE TABLE IF NOT EXISTS memnos_control.grants(
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    namespace text NOT NULL, can_read boolean NOT NULL DEFAULT true, can_write boolean NOT NULL DEFAULT true,
    UNIQUE(principal_id, namespace));
CREATE TABLE IF NOT EXISTS memnos_control.roles(
    id bigserial PRIMARY KEY, name text UNIQUE NOT NULL, description text,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS memnos_control.role_members(
    role_id bigint NOT NULL REFERENCES memnos_control.roles(id),
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(role_id, principal_id));
CREATE INDEX IF NOT EXISTS role_members_principal ON memnos_control.role_members(principal_id);
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
CREATE TABLE IF NOT EXISTS memnos_control.eval_runs(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), eval text NOT NULL,
    metric text NOT NULL, value numeric, n int, detail jsonb);
CREATE INDEX IF NOT EXISTS eval_ts ON memnos_control.eval_runs(ts);
CREATE TABLE IF NOT EXISTS memnos_control.feedback(
    id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), principal_id bigint,
    namespace text, query text, helpful boolean, note text);
CREATE TABLE IF NOT EXISTS memnos_control.namespaces(
    name text PRIMARY KEY, created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(), description text);
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'memory';
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS auto_registered boolean NOT NULL DEFAULT false;
ALTER TABLE memnos_control.namespaces ADD COLUMN IF NOT EXISTS inherit_ancestors boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS memnos_control.namespace_registry_backfill(
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    completed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS memnos_control.entity_graph_cleanup_v1(
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    completed_at timestamptz NOT NULL DEFAULT now(),
    stats jsonb);
CREATE TABLE IF NOT EXISTS memnos_control.namespace_links(
    src_ns text NOT NULL, dst_ns text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(src_ns, dst_ns));
ALTER TABLE memnos_control.namespace_links ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'link';
CREATE TABLE IF NOT EXISTS memnos_control.namespace_copy_provenance(
    id bigserial PRIMARY KEY,
    dst_ns text NOT NULL,
    src_ns text NOT NULL,
    mode text NOT NULL,
    copied_by bigint REFERENCES memnos_control.principals(id),
    copied_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS namespace_copy_provenance_dst ON memnos_control.namespace_copy_provenance(dst_ns);
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
CREATE TABLE IF NOT EXISTS memnos_control.constraint_overrides(
    id bigserial PRIMARY KEY,
    child_namespace text NOT NULL,
    parent_namespace text NOT NULL,
    created_by bigint REFERENCES memnos_control.principals(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(child_namespace, parent_namespace));
CREATE TABLE IF NOT EXISTS memnos_control.secrets(
    name text PRIMARY KEY, nonce bytea NOT NULL, ciphertext bytea NOT NULL, description text,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
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
CREATE TABLE IF NOT EXISTS memnos_control.corpus_sources(
    id bigserial PRIMARY KEY, namespace text NOT NULL, name text NOT NULL,
    kind text, git_sha text, constraint_count int NOT NULL DEFAULT 0,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(namespace, name));
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
CREATE TABLE IF NOT EXISTS memnos_control.hosts(
    id bigserial PRIMARY KEY,
    principal_id bigint NOT NULL REFERENCES memnos_control.principals(id),
    machine_id text NOT NULL,
    friendly_name text,
    last_seen timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(principal_id, machine_id));
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
CREATE TABLE IF NOT EXISTS memnos_control.agent_sessions(
    id                bigserial PRIMARY KEY,
    namespace         text NOT NULL,
    name              text NOT NULL,
    principal_id      bigint NOT NULL REFERENCES memnos_control.principals(id),
    harness           text NOT NULL DEFAULT '',
    model             text NOT NULL DEFAULT '',
    role              text NOT NULL DEFAULT '',
    host              text NOT NULL DEFAULT '',
    cwd               text NOT NULL DEFAULT '',
    status            text NOT NULL DEFAULT 'active',
    current_task      text NOT NULL DEFAULT '',
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    registered_at     timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL,
    UNIQUE(namespace, name)
);
CREATE INDEX IF NOT EXISTS agent_sessions_ns_exp
    ON memnos_control.agent_sessions(namespace, expires_at DESC);
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS webhook text;
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS webhook_failures int NOT NULL DEFAULT 0;
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS webhook_active boolean NOT NULL DEFAULT true;
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS webhook_secret text;
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS machine_id text NOT NULL DEFAULT '';
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS pid bigint;
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS tty text NOT NULL DEFAULT '';
ALTER TABLE memnos_control.agent_sessions ADD COLUMN IF NOT EXISTS run_state text NOT NULL DEFAULT 'idle';
CREATE TABLE IF NOT EXISTS memnos_control.channel_messages(
    id             bigserial PRIMARY KEY,
    namespace      text NOT NULL,
    channel        text NOT NULL,
    sender_session text NOT NULL,
    principal_id   bigint NOT NULL REFERENCES memnos_control.principals(id),
    intent         text NOT NULL DEFAULT 'message',
    content        text NOT NULL,
    metadata       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    recalled_at    timestamptz,
    recall_reason  text NOT NULL DEFAULT ''
);
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS recalled_at timestamptz;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS recall_reason text NOT NULL DEFAULT '';
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claimable boolean NOT NULL DEFAULT false;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claim_status text NOT NULL DEFAULT 'none';
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claimed_by text;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claimed_by_ns text;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claim_lease_until timestamptz;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS claim_attempts int NOT NULL DEFAULT 0;
ALTER TABLE memnos_control.channel_messages ADD COLUMN IF NOT EXISTS resolution text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS channel_messages_ns_ch_id ON memnos_control.channel_messages(namespace, channel, id);
CREATE INDEX IF NOT EXISTS channel_messages_ch_id ON memnos_control.channel_messages(channel, id);
CREATE INDEX IF NOT EXISTS channel_messages_ns_id ON memnos_control.channel_messages(namespace, id);
CREATE TABLE IF NOT EXISTS memnos_control.agent_inbox (
    id                BIGSERIAL PRIMARY KEY,
    namespace         TEXT NOT NULL,
    to_session        TEXT NOT NULL,
    from_session      TEXT NOT NULL,
    principal_id      BIGINT NOT NULL REFERENCES memnos_control.principals(id),
    subject           TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL,
    intent            TEXT NOT NULL DEFAULT 'task',
    priority          TEXT NOT NULL DEFAULT 'normal',
    status            TEXT NOT NULL DEFAULT 'pending',
    thread_key        TEXT NOT NULL DEFAULT '',
    replaces_id       BIGINT REFERENCES memnos_control.agent_inbox(id),
    superseded_by_id  BIGINT REFERENCES memnos_control.agent_inbox(id),
    in_reply_to       BIGINT REFERENCES memnos_control.agent_inbox(id),
    resolution        TEXT NOT NULL DEFAULT '',
    recall_reason     TEXT NOT NULL DEFAULT '',
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at           TIMESTAMPTZ,
    acted_at          TIMESTAMPTZ,
    recalled_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_inbox_ns_to_status_idx
    ON memnos_control.agent_inbox(namespace, to_session, status, id);
CREATE TABLE IF NOT EXISTS memnos_control.channel_subscriptions(
    id             bigserial PRIMARY KEY,
    namespace      text NOT NULL,
    session_name   text NOT NULL,
    principal_id   bigint NOT NULL REFERENCES memnos_control.principals(id),
    channel        text NOT NULL,
    cursor         bigint NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_polled_at timestamptz,
    UNIQUE(namespace, session_name, channel)
);
CREATE INDEX IF NOT EXISTS channel_subs_ns_session
    ON memnos_control.channel_subscriptions(namespace, session_name);
CREATE TABLE IF NOT EXISTS memnos_control.channel_catalog (
    namespace       TEXT NOT NULL,
    channel         TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'topic',
    description     TEXT NOT NULL DEFAULT '',
    what_you_get    TEXT NOT NULL DEFAULT '',
    intents         TEXT[] NOT NULL DEFAULT '{}',
    owner_session   TEXT NOT NULL DEFAULT '',
    principal_id    BIGINT REFERENCES memnos_control.principals(id),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, channel)
);
ALTER TABLE memnos_control.channel_catalog ADD COLUMN IF NOT EXISTS global_topic boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS memnos_control.namespace_reconcile_runs(
    tenant_schema      text NOT NULL,
    namespace          text NOT NULL,
    cursor_observed_at timestamptz,
    cursor_id          bigint,
    facts_scanned      bigint NOT NULL DEFAULT 0,
    deduped            bigint NOT NULL DEFAULT 0,
    closed             bigint NOT NULL DEFAULT 0,
    chunks             bigint NOT NULL DEFAULT 0,
    started_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    PRIMARY KEY (tenant_schema, namespace)
);
"#;

/// Does message `m` match subscription `s`? Exact | `#prefix*` | `*` (everything but other
/// sessions' direct inboxes). A session never sees its own posts except in its own `@inbox`.
/// `global_topic` channels match across namespaces when the subscriber (directly or via a role)
/// holds a read grant on the message's namespace.
const SUB_MATCH: &str = "(
    m.namespace = s.namespace
    OR (
        m.channel = s.channel
        AND m.channel LIKE '#%'
        AND m.channel <> '#general'
        AND EXISTS (SELECT 1 FROM memnos_control.channel_catalog cc
                    WHERE cc.channel = m.channel AND cc.global_topic = true)
        AND (
            m.principal_id = s.principal_id
            OR EXISTS (
                SELECT 1 FROM (
                    SELECT g0.namespace, g0.can_read FROM memnos_control.grants g0
                    WHERE g0.principal_id = s.principal_id
                    UNION ALL
                    SELECT rg.namespace, rg.can_read FROM memnos_control.role_grants rg
                    JOIN memnos_control.role_members rm ON rm.role_id = rg.role_id
                    WHERE rm.principal_id = s.principal_id
                ) g
                WHERE g.can_read = true
                  AND (g.namespace = '*' OR g.namespace = m.namespace
                       OR (right(g.namespace, 2) = ':*'
                           AND starts_with(m.namespace, left(g.namespace, length(g.namespace) - 1))))
            )
        )
    )
) AND m.id > s.cursor AND (
    m.channel = s.channel
    OR (s.channel = '*' AND (m.channel NOT LIKE '@%' OR m.channel = '@' || s.session_name))
    OR (s.channel <> '*' AND right(s.channel, 1) = '*'
        AND starts_with(m.channel, left(s.channel, length(s.channel) - 1))))
    AND (m.sender_session <> s.session_name OR m.channel = '@' || s.session_name)
    AND m.recalled_at IS NULL
    AND (NOT m.claimable OR m.claim_status = 'pending'
         OR (m.claim_status = 'claimed' AND m.claim_lease_until < now() AND m.claim_attempts < 3)
         OR (m.claimed_by = s.session_name AND m.claimed_by_ns = s.namespace))";

pub struct Control;

/// Associated-function aliases of the pure helpers, so callers can use either `control::slug` or `Control::slug`.
impl Control {
    pub fn hash_token(token: &str) -> String {
        hash_token(token)
    }
    pub fn slug(name: &str, what: &str) -> CResult<String> {
        slug(name, what)
    }
    pub fn validate_channel(ch: &str) -> CResult<String> {
        validate_channel(ch)
    }
    pub fn namespace_ancestors(ns: &str) -> Vec<String> {
        namespace_ancestors(ns)
    }
}

impl Control {
    /// Create the schema, run additive migrations, mark legacy shared topics global, and run the
    /// one-time namespace-registry backfill (best-effort, never fatal).
    pub async fn init(pool: &Pool) -> CResult<()> {
        let c = conn(pool).await?;
        let locked_ddl = format!(
            "BEGIN;\nSELECT pg_advisory_xact_lock(7819204501);\n{}\nCOMMIT;",
            CONTROL_DDL
        );
        c.batch_execute(&locked_ddl).await?;
        c.execute(
            "UPDATE memnos_control.channel_catalog SET global_topic = true
             WHERE kind = 'topic' AND channel LIKE '#%' AND channel <> '#general'
               AND channel IN (SELECT channel FROM memnos_control.channel_catalog
                               WHERE channel LIKE '#%' AND channel <> '#general'
                               GROUP BY channel HAVING count(DISTINCT namespace) > 1)",
            &[],
        )
        .await?;
        if let Err(e) = Self::run_namespace_registry_backfill(&**c).await {
            eprintln!("memnos: namespace registry backfill failed (retried next boot): {e}");
        }
        Ok(())
    }

    async fn run_namespace_registry_backfill(c: &impl GenericClient) -> CResult<()> {
        let need: bool = c
            .query_one(
                "SELECT NOT EXISTS(SELECT 1 FROM memnos_control.namespace_registry_backfill)
                        AND to_regclass('tenant_memnos.raw_turns') IS NOT NULL",
                &[],
            )
            .await?
            .get(0);
        if !need {
            return Ok(());
        }
        let prior: String = c
            .query_one(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'",
                &[],
            )
            .await?
            .get(0);
        c.batch_execute("SET statement_timeout = 0").await?;
        let res = async {
            for table in ["raw_turns", "semantic"] {
                let max_id: Option<i64> = c
                    .query_one(format!("SELECT max(id) FROM tenant_memnos.{table}").as_str(), &[])
                    .await?
                    .get(0);
                let mut start = 0i64;
                while max_id.is_some_and(|m| start < m) {
                    let end = start + NAMESPACE_BACKFILL_BATCH;
                    c.execute(
                        format!(
                            "INSERT INTO memnos_control.namespaces(name, auto_registered)
                             SELECT DISTINCT namespace, true FROM tenant_memnos.{table}
                             WHERE id > $1 AND id <= $2 ON CONFLICT (name) DO NOTHING"
                        )
                        .as_str(),
                        params![start, end],
                    )
                    .await?;
                    start = end;
                }
            }
            c.execute(
                "INSERT INTO memnos_control.namespace_registry_backfill DEFAULT VALUES ON CONFLICT (id) DO NOTHING",
                &[],
            )
            .await?;
            Ok::<(), ControlError>(())
        }
        .await;
        let _ = c
            .batch_execute(
                format!(
                    "SET statement_timeout = {}",
                    prior.parse::<i64>().unwrap_or(0)
                )
                .as_str(),
            )
            .await;
        res
    }
}

// ================================================================ identity, tokens, ACL, roles
impl Control {
    pub async fn create_principal(pool: &Pool, name: &str, kind: &str) -> CResult<i64> {
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.principals(name,kind) VALUES($1,$2)
                 ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind RETURNING id",
                params![name, kind],
            )
            .await?;
        Ok(r.get(0))
    }

    pub async fn list_principals(pool: &Pool) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT p.id, p.name, p.kind, p.created_at,
                        count(t.id) FILTER (WHERE NOT t.revoked) AS active_tokens
                 FROM memnos_control.principals p
                 LEFT JOIN memnos_control.api_tokens t ON t.principal_id=p.id
                 GROUP BY p.id ORDER BY p.created_at",
                &[],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// `{name, kind}` of a principal, or None. The server stamps authorship from this.
    pub async fn principal_info(pool: &Pool, principal_id: i64) -> CResult<Option<Value>> {
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "SELECT name, kind FROM memnos_control.principals WHERE id=$1",
                params![principal_id],
            )
            .await?;
        Ok(r.as_ref().map(row_to_json))
    }

    /// Mint a token (`mnk_` + 32 random bytes, url-safe base64). Plaintext is returned ONCE;
    /// only its SHA-256 hash and a non-secret hint are stored.
    pub async fn mint_token(
        pool: &Pool,
        principal_id: i64,
        label: Option<&str>,
        ttl_days: Option<i32>,
    ) -> CResult<String> {
        let mut raw = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut raw);
        let token = format!("mnk_{}", B64URL.encode(raw));
        let chars: Vec<char> = token.chars().collect();
        let hint = format!(
            "{}…{}",
            chars[..8].iter().collect::<String>(),
            chars[chars.len() - 4..].iter().collect::<String>()
        );
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.api_tokens(principal_id,token_hash,label,hint,expires_at)
             VALUES($1,$2,$3,$4, CASE WHEN $5::int IS NULL THEN NULL
                                      ELSE now() + ($5::int * interval '1 day') END)",
            params![principal_id, hash_token(&token), label, hint, ttl_days],
        )
        .await?;
        Ok(token)
    }

    /// Bearer token -> principal id (None if unknown, revoked or expired).
    pub async fn authenticate(pool: &Pool, token: &str) -> CResult<Option<i64>> {
        if token.is_empty() {
            return Ok(None);
        }
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "SELECT principal_id FROM memnos_control.api_tokens
                 WHERE token_hash=$1 AND NOT revoked AND (expires_at IS NULL OR expires_at>now())",
                params![hash_token(token)],
            )
            .await?;
        Ok(r.map(|r| r.get(0)))
    }

    /// Token METADATA only; the secret is not retrievable.
    pub async fn list_tokens(pool: &Pool, principal_id: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, label, hint, created_at, expires_at, revoked FROM memnos_control.api_tokens
                 WHERE principal_id=$1 ORDER BY created_at DESC LIMIT 500",
                params![principal_id],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn revoke_token(pool: &Pool, token: &str) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "UPDATE memnos_control.api_tokens SET revoked=true WHERE token_hash=$1",
            params![hash_token(token)],
        )
        .await?;
        Ok(())
    }

    pub async fn revoke_token_by_id(pool: &Pool, token_id: i64) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "UPDATE memnos_control.api_tokens SET revoked=true WHERE id=$1",
            params![token_id],
        )
        .await?;
        Ok(())
    }

    pub async fn grant(
        pool: &Pool,
        principal_id: i64,
        namespace: &str,
        can_read: bool,
        can_write: bool,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        Self::grant_on(&**c, principal_id, namespace, can_read, can_write).await
    }

    async fn grant_on(
        c: &impl GenericClient,
        principal_id: i64,
        namespace: &str,
        r: bool,
        w: bool,
    ) -> CResult<()> {
        c.execute(
            "INSERT INTO memnos_control.grants(principal_id,namespace,can_read,can_write) VALUES($1,$2,$3,$4)
             ON CONFLICT (principal_id,namespace) DO UPDATE SET can_read=EXCLUDED.can_read, can_write=EXCLUDED.can_write",
            params![principal_id, namespace, r, w],
        )
        .await?;
        Ok(())
    }

    pub async fn revoke_grant(pool: &Pool, principal_id: i64, namespace: &str) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "DELETE FROM memnos_control.grants WHERE principal_id=$1 AND namespace=$2",
            params![principal_id, namespace],
        )
        .await?;
        Ok(())
    }

    /// The principal's OWN DIRECT grants only (what `grant rm` can remove).
    pub async fn authorized_namespaces(pool: &Pool, principal_id: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT namespace, can_read, can_write FROM memnos_control.grants WHERE principal_id=$1",
                params![principal_id],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Enforcement-path ACL: direct grants UNION role grants, aggregated per namespace.
    /// Returns `(namespace, can_read, can_write)`.
    pub async fn effective_namespaces_on(
        c: &impl GenericClient,
        principal_id: i64,
    ) -> CResult<Vec<(String, bool, bool)>> {
        let rows = c
            .query(
                "SELECT namespace, bool_or(can_read) AS can_read, bool_or(can_write) AS can_write
                 FROM (SELECT namespace, can_read, can_write FROM memnos_control.grants WHERE principal_id=$1
                       UNION ALL
                       SELECT rg.namespace, rg.can_read, rg.can_write FROM memnos_control.role_grants rg
                       JOIN memnos_control.role_members rm ON rm.role_id = rg.role_id
                       WHERE rm.principal_id = $1) x
                 GROUP BY namespace",
                params![principal_id],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| (r.get(0), r.get(1), r.get(2)))
            .collect())
    }

    pub async fn effective_namespaces(
        pool: &Pool,
        principal_id: i64,
    ) -> CResult<Vec<(String, bool, bool)>> {
        let c = conn(pool).await?;
        Self::effective_namespaces_on(&**c, principal_id).await
    }

    fn grant_matches(grant_ns: &str, namespace: &str) -> bool {
        grant_ns == "*"
            || grant_ns == namespace
            || (grant_ns.ends_with(":*") && namespace.starts_with(&grant_ns[..grant_ns.len() - 1]))
    }

    /// Exact grant, `prefix:*` wildcard, or `*`, from a direct grant OR any role membership.
    pub async fn authorize_on(
        c: &impl GenericClient,
        principal_id: i64,
        namespace: &str,
        write: bool,
    ) -> CResult<bool> {
        Ok(Self::effective_namespaces_on(c, principal_id)
            .await?
            .iter()
            .any(|(g, r, w)| (if write { *w } else { *r }) && Self::grant_matches(g, namespace)))
    }

    pub async fn authorize(
        pool: &Pool,
        principal_id: i64,
        namespace: &str,
        write: bool,
    ) -> CResult<bool> {
        let c = conn(pool).await?;
        Self::authorize_on(&**c, principal_id, namespace, write).await
    }

    /// Admin = the `*` read grant, direct or via a role.
    pub async fn is_admin(pool: &Pool, principal_id: i64) -> CResult<bool> {
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "SELECT 1 FROM memnos_control.grants WHERE principal_id=$1 AND namespace='*' AND can_read
                 UNION ALL
                 SELECT 1 FROM memnos_control.role_grants rg
                 JOIN memnos_control.role_members rm ON rm.role_id = rg.role_id
                 WHERE rm.principal_id=$1 AND rg.namespace='*' AND rg.can_read
                 LIMIT 1",
                params![principal_id],
            )
            .await?;
        Ok(r.is_some())
    }

    /// Expand exact + wildcard ACL entries against `existing` namespace names.
    fn expand(grants: Vec<String>, existing: Vec<String>) -> BTreeSet<String> {
        let mut out = BTreeSet::new();
        let mut wild = vec![];
        for g in grants {
            if g == "*" || g.ends_with(":*") {
                wild.push(g);
            } else {
                out.insert(g);
            }
        }
        for ns in existing {
            if wild.iter().any(|p| Self::grant_matches(p, &ns)) {
                out.insert(ns);
            }
        }
        out
    }

    /// Concrete namespaces the principal may READ (wildcards expanded against the registry).
    pub async fn readable_namespaces_on(
        c: &impl GenericClient,
        principal_id: i64,
        exclude: Option<&str>,
        limit: Option<usize>,
    ) -> CResult<Vec<String>> {
        let grants: Vec<String> = Self::effective_namespaces_on(c, principal_id)
            .await?
            .into_iter()
            .filter(|g| g.1)
            .map(|g| g.0)
            .collect();
        if grants.is_empty() {
            return Ok(vec![]);
        }
        let existing: Vec<String> = if grants.iter().any(|g| g == "*" || g.ends_with(":*")) {
            c.query("SELECT name FROM memnos_control.namespaces", &[])
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect()
        } else {
            vec![]
        };
        let mut set = Self::expand(grants, existing);
        if let Some(x) = exclude {
            set.remove(x);
        }
        Ok(set.into_iter().take(limit.unwrap_or(usize::MAX)).collect())
    }

    pub async fn readable_namespaces(
        pool: &Pool,
        principal_id: i64,
        exclude: Option<&str>,
        limit: Option<usize>,
    ) -> CResult<Vec<String>> {
        let c = conn(pool).await?;
        Self::readable_namespaces_on(&**c, principal_id, exclude, limit).await
    }

    /// Concrete namespaces the principal may WRITE, capped at `limit` (cold path: scans data tables).
    pub async fn writable_namespaces(
        pool: &Pool,
        principal_id: i64,
        limit: usize,
    ) -> CResult<Vec<String>> {
        let c = conn(pool).await?;
        let grants: Vec<String> = Self::effective_namespaces_on(&**c, principal_id)
            .await?
            .into_iter()
            .filter(|g| g.2)
            .map(|g| g.0)
            .collect();
        if grants.is_empty() {
            return Ok(vec![]);
        }
        let existing: Vec<String> = if grants.iter().any(|g| g == "*" || g.ends_with(":*")) {
            c.query(
                "SELECT DISTINCT namespace FROM tenant_memnos.raw_turns
                 UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic",
                &[],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect()
        } else {
            vec![]
        };
        Ok(Self::expand(grants, existing)
            .into_iter()
            .take(limit)
            .collect())
    }

    // ---- roles
    pub async fn create_role(
        pool: &Pool,
        name: &str,
        description: Option<&str>,
        created_by: Option<i64>,
    ) -> CResult<i64> {
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.roles(name,description,created_by) VALUES($1,$2,$3)
                 ON CONFLICT (name) DO UPDATE SET description=COALESCE(EXCLUDED.description, memnos_control.roles.description)
                 RETURNING id",
                params![name, description, created_by],
            )
            .await?;
        Ok(r.get(0))
    }

    pub async fn list_roles(pool: &Pool) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT r.id, r.name, r.description, r.created_at, p.name AS created_by,
                        count(DISTINCT rm.principal_id) AS member_count, count(DISTINCT rg.namespace) AS grant_count
                 FROM memnos_control.roles r
                 LEFT JOIN memnos_control.principals p ON p.id = r.created_by
                 LEFT JOIN memnos_control.role_members rm ON rm.role_id = r.id
                 LEFT JOIN memnos_control.role_grants rg ON rg.role_id = r.id
                 GROUP BY r.id, p.name ORDER BY r.name",
                &[],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    async fn role_id(c: &impl GenericClient, name: &str) -> CResult<Option<i64>> {
        Ok(c.query_opt(
            "SELECT id FROM memnos_control.roles WHERE name=$1",
            params![name],
        )
        .await?
        .map(|r| r.get(0)))
    }

    pub async fn delete_role(pool: &Pool, name: &str) -> CResult<bool> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, name).await? else {
            return Ok(false);
        };
        c.execute(
            "DELETE FROM memnos_control.role_grants WHERE role_id=$1",
            params![rid],
        )
        .await?;
        c.execute(
            "DELETE FROM memnos_control.role_members WHERE role_id=$1",
            params![rid],
        )
        .await?;
        c.execute("DELETE FROM memnos_control.roles WHERE id=$1", params![rid])
            .await?;
        Ok(true)
    }

    pub async fn grant_role(
        pool: &Pool,
        role: &str,
        namespace: &str,
        can_read: bool,
        can_write: bool,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, role).await? else {
            return bad(format!("no role '{role}'"));
        };
        c.execute(
            "INSERT INTO memnos_control.role_grants(role_id,namespace,can_read,can_write) VALUES($1,$2,$3,$4)
             ON CONFLICT (role_id,namespace) DO UPDATE SET can_read=EXCLUDED.can_read, can_write=EXCLUDED.can_write",
            params![rid, namespace, can_read, can_write],
        )
        .await?;
        Ok(())
    }

    pub async fn revoke_role_grant(pool: &Pool, role: &str, namespace: &str) -> CResult<()> {
        let c = conn(pool).await?;
        if let Some(rid) = Self::role_id(&**c, role).await? {
            c.execute(
                "DELETE FROM memnos_control.role_grants WHERE role_id=$1 AND namespace=$2",
                params![rid, namespace],
            )
            .await?;
        }
        Ok(())
    }

    pub async fn list_role_grants(pool: &Pool, role: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, role).await? else {
            return Ok(vec![]);
        };
        let rows = c
            .query("SELECT namespace, can_read, can_write FROM memnos_control.role_grants WHERE role_id=$1 ORDER BY namespace", params![rid])
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn add_role_member(pool: &Pool, role: &str, principal_id: i64) -> CResult<()> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, role).await? else {
            return bad(format!("no role '{role}'"));
        };
        c.execute(
            "INSERT INTO memnos_control.role_members(role_id,principal_id) VALUES($1,$2) ON CONFLICT (role_id,principal_id) DO NOTHING",
            params![rid, principal_id],
        )
        .await?;
        Ok(())
    }

    pub async fn remove_role_member(pool: &Pool, role: &str, principal_id: i64) -> CResult<bool> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, role).await? else {
            return Ok(false);
        };
        Ok(c.execute(
            "DELETE FROM memnos_control.role_members WHERE role_id=$1 AND principal_id=$2",
            params![rid, principal_id],
        )
        .await?
            > 0)
    }

    pub async fn list_role_members(pool: &Pool, role: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let Some(rid) = Self::role_id(&**c, role).await? else {
            return Ok(vec![]);
        };
        let rows = c
            .query(
                "SELECT p.id, p.name, p.kind FROM memnos_control.role_members rm
                 JOIN memnos_control.principals p ON p.id = rm.principal_id WHERE rm.role_id=$1 ORDER BY p.name",
                params![rid],
            )
            .await?;
        Ok(rows_json(&rows))
    }
}

// ================================================================ namespaces, links, bindings, hosts, nudges
impl Control {
    /// Register a namespace and grant its creator read/write. Idempotent on name.
    pub async fn create_namespace(
        pool: &Pool,
        name: &str,
        created_by: Option<i64>,
        description: Option<&str>,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.namespaces(name,created_by,description,auto_registered) VALUES($1,$2,$3,false)
             ON CONFLICT (name) DO UPDATE SET description=EXCLUDED.description, auto_registered=false",
            params![name, created_by, description],
        )
        .await?;
        if let Some(p) = created_by {
            Self::grant_on(&**c, p, name, true, true).await?;
        }
        Ok(())
    }

    pub async fn set_namespace_kind(pool: &Pool, name: &str, kind: &str) -> CResult<()> {
        if kind != "memory" && kind != "knowledge" {
            return bad("kind must be 'memory' or 'knowledge'");
        }
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.namespaces(name, kind) VALUES($1,$2) ON CONFLICT (name) DO UPDATE SET kind=EXCLUDED.kind",
            params![name, kind],
        )
        .await?;
        Ok(())
    }

    pub async fn set_namespace_inherit_ancestors(
        pool: &Pool,
        name: &str,
        inherit: bool,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.namespaces(name, inherit_ancestors) VALUES($1,$2)
             ON CONFLICT (name) DO UPDATE SET inherit_ancestors=EXCLUDED.inherit_ancestors",
            params![name, inherit],
        )
        .await?;
        Ok(())
    }

    /// Remove the registry row + all grants/links on `name`; optionally purge its memory rows.
    pub async fn delete_namespace(pool: &Pool, name: &str, purge_data: bool) -> CResult<()> {
        let mut c = conn(pool).await?;
        let tx = c.transaction().await?;
        tx.execute(
            "DELETE FROM memnos_control.namespaces WHERE name=$1",
            params![name],
        )
        .await?;
        tx.execute(
            "DELETE FROM memnos_control.grants WHERE namespace=$1",
            params![name],
        )
        .await?;
        tx.execute(
            "DELETE FROM memnos_control.namespace_links WHERE src_ns=$1 OR dst_ns=$1",
            params![name],
        )
        .await?;
        if purge_data {
            tx.execute(
                "DELETE FROM tenant_memnos.mentions m USING tenant_memnos.entities e WHERE m.entity_id=e.id AND e.namespace=$1",
                params![name],
            )
            .await?;
            for t in ["edges", "entities", "semantic", "episodic", "raw_turns"] {
                tx.execute(
                    format!("DELETE FROM tenant_memnos.{t} WHERE namespace=$1").as_str(),
                    params![name],
                )
                .await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }

    /// Registry UNION namespaces with data or concrete grants (`secret:` pseudo-namespaces excluded).
    pub async fn list_namespaces(pool: &Pool) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "WITH names AS (
                    SELECT name FROM memnos_control.namespaces
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.raw_turns
                    UNION SELECT DISTINCT namespace FROM tenant_memnos.semantic
                    UNION SELECT DISTINCT namespace FROM memnos_control.grants
                      WHERE namespace <> '*' AND namespace NOT LIKE '%*' AND namespace NOT LIKE 'secret:%')
                 SELECT nm.name, n.description, n.created_at, p.name AS created_by,
                   COALESCE(n.kind, 'memory') AS kind, COALESCE(n.inherit_ancestors, true) AS inherit_ancestors,
                   COALESCE(rt.cnt,0) AS turns, COALESCE(sm.cnt,0) AS facts,
                   (n.name IS NOT NULL AND NOT COALESCE(n.auto_registered, false)) AS registered
                 FROM names nm
                 LEFT JOIN memnos_control.namespaces n ON n.name=nm.name
                 LEFT JOIN memnos_control.principals p ON p.id=n.created_by
                 LEFT JOIN (SELECT namespace, count(*) cnt FROM tenant_memnos.raw_turns GROUP BY namespace) rt ON rt.namespace=nm.name
                 LEFT JOIN (SELECT namespace, count(*) cnt FROM tenant_memnos.semantic GROUP BY namespace) sm ON sm.namespace=nm.name
                 ORDER BY turns DESC, nm.name",
                &[],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    // ---- links (grounded recall)
    /// Declare that recall on `src` is also grounded in `dst`. Rejects self-links, unknown kinds
    /// and any link that would close a cycle (dst already reaches src).
    pub async fn link_namespaces(
        pool: &Pool,
        src: &str,
        dst: &str,
        created_by: Option<i64>,
        kind: &str,
    ) -> CResult<()> {
        if src == dst {
            return bad("src and dst must differ");
        }
        if !LINK_KINDS.contains(&kind) {
            return bad(format!("kind must be one of {LINK_KINDS:?}"));
        }
        let c = conn(pool).await?;
        let cyc = c
            .query_one(
                "WITH RECURSIVE reach(ns) AS (
                    SELECT $2::text
                    UNION
                    SELECT l.dst_ns FROM memnos_control.namespace_links l JOIN reach r ON l.src_ns = r.ns)
                 SELECT EXISTS(SELECT 1 FROM reach WHERE ns = $1::text)",
                params![src, dst],
            )
            .await?;
        if cyc.get::<_, bool>(0) {
            return bad(format!("linking {src} -> {dst} would create a cycle"));
        }
        c.execute(
            "INSERT INTO memnos_control.namespace_links(src_ns,dst_ns,created_by,kind) VALUES($1,$2,$3,$4)
             ON CONFLICT (src_ns,dst_ns) DO NOTHING",
            params![src, dst, created_by, kind],
        )
        .await?;
        Ok(())
    }

    pub async fn unlink_namespaces(pool: &Pool, src: &str, dst: &str) -> CResult<bool> {
        let c = conn(pool).await?;
        Ok(c.execute(
            "DELETE FROM memnos_control.namespace_links WHERE src_ns=$1 AND dst_ns=$2",
            params![src, dst],
        )
        .await?
            > 0)
    }

    pub async fn linked_namespaces_on(c: &impl GenericClient, src: &str) -> CResult<Vec<String>> {
        let rows = c
            .query(
                "SELECT dst_ns FROM memnos_control.namespace_links WHERE src_ns=$1 ORDER BY dst_ns",
                params![src],
            )
            .await?;
        Ok(rows.iter().map(|r| r.get(0)).collect())
    }

    pub async fn linked_namespaces(pool: &Pool, src: &str) -> CResult<Vec<String>> {
        let c = conn(pool).await?;
        Self::linked_namespaces_on(&**c, src).await
    }

    pub async fn list_links(pool: &Pool, src: Option<&str>) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT l.src_ns, l.dst_ns, l.kind, l.created_at, p.name AS created_by
                 FROM memnos_control.namespace_links l LEFT JOIN memnos_control.principals p ON p.id=l.created_by
                 WHERE ($1::text IS NULL OR l.src_ns=$1) ORDER BY l.src_ns, l.dst_ns",
                params![src],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Mechanism-A ancestors: `[]` if `ns` opted out (or has no ancestors), else nearest-first.
    pub async fn effective_ancestors_on(c: &impl GenericClient, ns: &str) -> CResult<Vec<String>> {
        let anc = namespace_ancestors(ns);
        if anc.is_empty() {
            return Ok(anc);
        }
        let row = c
            .query_opt(
                "SELECT inherit_ancestors FROM memnos_control.namespaces WHERE name=$1",
                params![ns],
            )
            .await?;
        Ok(if row.map_or(true, |r| r.get::<_, bool>(0)) {
            anc
        } else {
            vec![]
        })
    }

    pub async fn effective_ancestors(pool: &Pool, ns: &str) -> CResult<Vec<String>> {
        let c = conn(pool).await?;
        Self::effective_ancestors_on(&**c, ns).await
    }

    // ---- bindings
    pub async fn upsert_binding(
        pool: &Pool,
        principal_id: i64,
        key_type: &str,
        key: &str,
        namespace: &str,
        host_id: Option<&str>,
    ) -> CResult<Value> {
        if !["repo", "host_repo", "host_path"].contains(&key_type) {
            return bad("key_type must be 'repo', 'host_repo', or 'host_path'");
        }
        let c = conn(pool).await?;
        let row = if let Some(h) = host_id {
            c.query_one(
                "INSERT INTO memnos_control.bindings(principal_id,key_type,key,namespace,host_id) VALUES($1,$2,$3,$4,$5)
                 ON CONFLICT (principal_id,key_type,key,host_id) DO UPDATE SET namespace=EXCLUDED.namespace, updated_at=now()
                 RETURNING *",
                params![principal_id, key_type, key, namespace, h],
            )
            .await?
        } else if let Some(ex) = c
            .query_opt(
                "SELECT id FROM memnos_control.bindings WHERE principal_id=$1 AND key_type=$2 AND key=$3 AND host_id IS NULL",
                params![principal_id, key_type, key],
            )
            .await?
        {
            let id: i64 = ex.get(0);
            c.query_one("UPDATE memnos_control.bindings SET namespace=$1, updated_at=now() WHERE id=$2 RETURNING *", params![namespace, id])
                .await?
        } else {
            c.query_one(
                "INSERT INTO memnos_control.bindings(principal_id,key_type,key,namespace,host_id) VALUES($1,$2,$3,$4,NULL) RETURNING *",
                params![principal_id, key_type, key, namespace],
            )
            .await?
        };
        Self::audit_on(
            &**c,
            Some(principal_id),
            "bind",
            Some(namespace),
            true,
            Some(json!({"key_type": key_type, "key": key, "host_id": host_id})),
            None,
            None,
            None,
        )
        .await?;
        Ok(row_to_json(&row))
    }

    pub async fn list_bindings(pool: &Pool, principal_id: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, key_type, key, namespace, host_id, created_at, updated_at FROM memnos_control.bindings
                 WHERE principal_id=$1 ORDER BY key_type, key",
                params![principal_id],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Delete one of the principal's OWN bindings; false if not theirs / not found.
    pub async fn delete_binding(pool: &Pool, principal_id: i64, binding_id: i64) -> CResult<bool> {
        let c = conn(pool).await?;
        let ok = c
            .execute(
                "DELETE FROM memnos_control.bindings WHERE id=$1 AND principal_id=$2",
                params![binding_id, principal_id],
            )
            .await?
            > 0;
        if ok {
            Self::audit_on(
                &**c,
                Some(principal_id),
                "unbind",
                None,
                true,
                Some(json!({"binding_id": binding_id})),
                None,
                None,
                None,
            )
            .await?;
        }
        Ok(ok)
    }

    /// Best binding: repo (host-agnostic) -> host_repo -> host_path.
    pub async fn resolve_binding(
        pool: &Pool,
        principal_id: i64,
        repo_key: Option<&str>,
        host_id: Option<&str>,
        path_key: Option<&str>,
    ) -> CResult<Option<Value>> {
        let c = conn(pool).await?;
        if let Some(rk) = repo_key {
            if let Some(r) = c
                .query_opt("SELECT * FROM memnos_control.bindings WHERE principal_id=$1 AND key_type='repo' AND key=$2 LIMIT 1", params![principal_id, rk])
                .await?
            {
                return Ok(Some(row_to_json(&r)));
            }
        }
        if let (Some(h), Some(rk)) = (host_id, repo_key) {
            if let Some(r) = c
                .query_opt(
                    "SELECT * FROM memnos_control.bindings WHERE principal_id=$1 AND key_type='host_repo' AND key=$2 AND host_id=$3 LIMIT 1",
                    params![principal_id, rk, h],
                )
                .await?
            {
                return Ok(Some(row_to_json(&r)));
            }
        }
        if let (Some(h), Some(pk)) = (host_id, path_key) {
            if let Some(r) = c
                .query_opt(
                    "SELECT * FROM memnos_control.bindings WHERE principal_id=$1 AND key_type='host_path' AND key=$2 AND host_id=$3 LIMIT 1",
                    params![principal_id, pk, h],
                )
                .await?
            {
                return Ok(Some(row_to_json(&r)));
            }
        }
        Ok(None)
    }

    // ---- hosts
    pub async fn upsert_host(
        pool: &Pool,
        principal_id: i64,
        machine_id: &str,
        friendly_name: Option<&str>,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.hosts(principal_id,machine_id,friendly_name) VALUES($1,$2,$3)
                 ON CONFLICT (principal_id,machine_id) DO UPDATE
                 SET last_seen=now(), friendly_name=COALESCE(EXCLUDED.friendly_name, memnos_control.hosts.friendly_name)
                 RETURNING id, machine_id, friendly_name, last_seen, created_at",
                params![principal_id, machine_id, friendly_name],
            )
            .await?;
        Ok(row_to_json(&r))
    }

    pub async fn list_hosts(pool: &Pool, principal_id: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, machine_id, friendly_name, last_seen, created_at FROM memnos_control.hosts
                 WHERE principal_id=$1 ORDER BY last_seen DESC",
                params![principal_id],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    // ---- suggest-on-mismatch nudges
    /// Advisory: do the write's entities look like they belong to a DIFFERENT writable namespace?
    /// Returns `{namespace, reason}` or None. Never routes a write.
    pub async fn suggest_namespace(
        pool: &Pool,
        principal_id: i64,
        write_ns: &str,
        entity_names: &[String],
        min_entities: usize,
        dominance: f64,
        max_candidate_ns: usize,
    ) -> CResult<Option<Value>> {
        fn toks(s: &str) -> Vec<String> {
            TOKEN_RE
                .find_iter(&s.to_lowercase())
                .map(|m| m.as_str().to_string())
                .collect()
        }
        let want: BTreeSet<String> = entity_names.iter().flat_map(|n| toks(n)).collect();
        if want.len() < min_entities {
            return Ok(None);
        }
        let c = conn(pool).await?;
        let writable: Vec<String> = Self::effective_namespaces_on(&**c, principal_id)
            .await?
            .into_iter()
            .filter(|g| g.2)
            .map(|g| g.0)
            .collect();
        let token_re = format!(
            r"\m({})\M",
            want.iter().cloned().collect::<Vec<_>>().join("|")
        );
        let ranked: Vec<String> = c
            .query(
                "SELECT namespace, count(*) AS m FROM tenant_memnos.entities WHERE name ~* $1
                 GROUP BY namespace ORDER BY m DESC, namespace",
                params![token_re],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let cand: Vec<String> = ranked
            .into_iter()
            .filter(|ns| ns != write_ns && writable.iter().any(|p| Self::grant_matches(p, ns)))
            .take(max_candidate_ns)
            .collect();
        if cand.is_empty() {
            return Ok(None);
        }
        async fn hits(
            c: &impl GenericClient,
            namespaces: Vec<String>,
            want: &BTreeSet<String>,
        ) -> CResult<HashMap<String, usize>> {
            let rows = c
                .query(
                    "SELECT namespace, name FROM tenant_memnos.entities WHERE namespace = ANY($1)",
                    params![namespaces],
                )
                .await?;
            let mut h: HashMap<String, BTreeSet<String>> = HashMap::new();
            for r in rows {
                let (ns, name): (String, String) = (r.get(0), r.get(1));
                let cov: Vec<String> = toks(&name)
                    .into_iter()
                    .filter(|t| want.contains(t))
                    .collect();
                if !cov.is_empty() {
                    h.entry(ns).or_default().extend(cov);
                }
            }
            Ok(h.into_iter().map(|(k, v)| (k, v.len())).collect())
        }
        let own = hits(&**c, vec![write_ns.to_string()], &want)
            .await?
            .get(write_ns)
            .copied()
            .unwrap_or(0);
        let cand_hits = hits(&**c, cand, &want).await?;
        let Some((other_ns, other_n)) = cand_hits
            .iter()
            .max_by(|a, b| a.1.cmp(b.1).then(b.0.cmp(a.0)))
            .map(|(k, v)| (k.clone(), *v))
        else {
            return Ok(None);
        };
        let total = want.len();
        if (other_n as f64 / total as f64) >= dominance && other_n > own {
            return Ok(Some(
                json!({"namespace": other_ns, "reason": format!("{other_n} of these {total} entities are mostly in {other_ns}")}),
            ));
        }
        Ok(None)
    }

    /// Persist (or refresh) an undelivered nudge; a repeat write bumps `hits` and re-opens it.
    pub async fn record_nudge(
        pool: &Pool,
        principal_id: i64,
        write_ns: &str,
        suggested_ns: &str,
        reason: &str,
        turn_id: Option<i64>,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.ns_nudges(principal_id, write_ns, suggested_ns, reason, last_turn_id) VALUES($1,$2,$3,$4,$5)
             ON CONFLICT (principal_id, write_ns, suggested_ns) DO UPDATE SET
               hits = memnos_control.ns_nudges.hits + 1, reason = EXCLUDED.reason, last_turn_id = EXCLUDED.last_turn_id,
               updated_at = now(), delivered_at = NULL",
            params![principal_id, write_ns, suggested_ns, reason, turn_id],
        )
        .await?;
        Ok(())
    }

    /// Return the principal's undelivered nudges (newest first) and mark them delivered atomically.
    pub async fn take_pending_nudges(
        pool: &Pool,
        principal_id: i64,
        limit: i64,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "UPDATE memnos_control.ns_nudges SET delivered_at = now()
                 WHERE id IN (SELECT id FROM memnos_control.ns_nudges WHERE principal_id=$1 AND delivered_at IS NULL
                              ORDER BY updated_at DESC LIMIT $2)
                 RETURNING write_ns, suggested_ns, reason, hits, last_turn_id",
                params![principal_id, limit],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Per-namespace successful-write counts over the last `days`.
    pub async fn write_recap(
        pool: &Pool,
        principal_id: i64,
        days: i32,
        limit: i64,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT namespace, count(*) AS writes FROM memnos_control.audit_log
                 WHERE principal_id=$1 AND ok AND namespace IS NOT NULL
                   AND action IN ('remember','memory/write','memory_write')
                   AND ts > now() - ($2::int * interval '1 day')
                 GROUP BY namespace ORDER BY writes DESC LIMIT $3",
                params![principal_id, days, limit],
            )
            .await?;
        Ok(rows_json(&rows))
    }
}

// ================================================================ audit, stats, usage, budget, health
impl Control {
    #[allow(clippy::too_many_arguments)]
    pub async fn audit_on(
        c: &impl GenericClient,
        principal_id: Option<i64>,
        action: &str,
        namespace: Option<&str>,
        ok: bool,
        detail: Option<Value>,
        latency_ms: Option<i32>,
        result_count: Option<i32>,
        status: Option<i32>,
    ) -> CResult<()> {
        // an empty detail is stored as NULL, like the Python engine
        let detail =
            detail.filter(|d| !d.is_null() && d.as_object().map_or(true, |o| !o.is_empty()));
        c.execute(
            "INSERT INTO memnos_control.audit_log(principal_id,action,namespace,ok,detail,latency_ms,result_count,status)
             VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
            params![principal_id, action, namespace, ok, detail, latency_ms, result_count, status],
        )
        .await?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn audit(
        pool: &Pool,
        principal_id: Option<i64>,
        action: &str,
        namespace: Option<&str>,
        ok: bool,
        detail: Option<Value>,
        latency_ms: Option<i32>,
        result_count: Option<i32>,
        status: Option<i32>,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        Self::audit_on(
            &**c,
            principal_id,
            action,
            namespace,
            ok,
            detail,
            latency_ms,
            result_count,
            status,
        )
        .await
    }

    /// Newest-first audit page; limit clamped to 1..=1000, offset >= 0.
    pub async fn recent_audit(pool: &Pool, limit: i64, offset: i64) -> CResult<Vec<Value>> {
        let (limit, offset) = (limit.clamp(1, 1000), offset.max(0));
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT ts, principal_id, action, namespace, ok, status, latency_ms, result_count
                 FROM memnos_control.audit_log ORDER BY ts DESC LIMIT $1 OFFSET $2",
                params![limit, offset],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Approximate audit row count from planner stats (exact count only if never analyzed).
    pub async fn audit_total(pool: &Pool) -> CResult<i64> {
        let c = conn(pool).await?;
        let n: i64 = c.query_one("SELECT reltuples::bigint FROM pg_class WHERE oid = 'memnos_control.audit_log'::regclass", &[]).await?.get(0);
        if n < 0 {
            return Ok(c
                .query_one("SELECT count(*) FROM memnos_control.audit_log", &[])
                .await?
                .get(0));
        }
        Ok(n)
    }

    /// Reliability rollup per action over the last `hours`: volume, 5xx rate, ACL-denied rate, p50/p95, recall-empty rate.
    pub async fn stats(pool: &Pool, hours: i32) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT action, count(*) AS calls,
                   round(100.0*avg((coalesce(status,0) >= 500)::int), 1)::float8 AS error_pct,
                   round(100.0*avg((NOT ok AND coalesce(status,0) < 500)::int), 1)::float8 AS acl_denied_pct,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                   round(100.0*avg(CASE WHEN action='recall' AND result_count=0 THEN 1
                                        WHEN action='recall' THEN 0 END), 1)::float8 AS recall_empty_pct
                 FROM memnos_control.audit_log WHERE ts > now() - ($1::int * interval '1 hour')
                 GROUP BY action ORDER BY calls DESC",
                params![hours],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn recent_errors(pool: &Pool, hours: i32, limit: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT ts, principal_id, action, namespace, status, latency_ms, detail FROM memnos_control.audit_log
                 WHERE NOT ok AND ts > now() - ($1::int * interval '1 hour') ORDER BY ts DESC LIMIT $2",
                params![hours, limit],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Admin memory feed: newest verbatim raw turns across ALL namespaces (admin-gate at the endpoint).
    pub async fn memory_feed(
        pool: &Pool,
        limit: i64,
        offset: i64,
        namespace: Option<&str>,
        memory_type: Option<&str>,
    ) -> CResult<Vec<Value>> {
        let (limit, offset) = (limit.clamp(1, 200), offset.max(0));
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, namespace, speaker, text AS content, memory_type AS type, author_principal AS author, observed_at
                 FROM tenant_memnos.raw_turns
                 WHERE ($1::text IS NULL OR namespace=$1) AND ($2::text IS NULL OR memory_type=$2)
                 ORDER BY id DESC LIMIT $3 OFFSET $4",
                params![namespace, memory_type, limit, offset],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn record_usage(
        pool: &Pool,
        principal_id: Option<i64>,
        namespace: &str,
        op: &str,
        model: &str,
        tokens_in: i32,
        tokens_out: i32,
        cost_usd: f64,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.usage_ledger(principal_id,namespace,op,model,tokens_in,tokens_out,cost_usd)
             VALUES($1,$2,$3,$4,$5,$6,$7::float8)",
            params![principal_id, namespace, op, model, tokens_in, tokens_out, cost_usd],
        )
        .await?;
        Ok(())
    }

    /// Usage/cost rollup by op; pass `hours` for a bounded window.
    pub async fn usage_rollup(pool: &Pool, hours: Option<i32>) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT op, count(*) n, round(sum(cost_usd),4)::float8 cost, sum(tokens_in) tin, sum(tokens_out) tout
                 FROM memnos_control.usage_ledger
                 WHERE ($1::int IS NULL OR ts > now() - ($1::int * interval '1 hour'))
                 GROUP BY op ORDER BY cost DESC NULLS LAST",
                params![hours],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// `{total_usd, total_tokens, by_op, by_namespace, by_day}` over the last `period_days` (1..=365).
    pub async fn usage_summary(
        pool: &Pool,
        period_days: i32,
        namespace: Option<&str>,
    ) -> CResult<Value> {
        let days = period_days.clamp(1, 365);
        let trend_days = days.min(7);
        let c = conn(pool).await?;
        let w = "ts > now() - ($1::int * interval '1 day') AND ($2::text IS NULL OR namespace=$2)";
        let t = c
            .query_one(
                format!("SELECT COALESCE(sum(cost_usd),0)::float8, COALESCE(sum(tokens_in + tokens_out),0)::int8 FROM memnos_control.usage_ledger WHERE {w}").as_str(),
                params![days, namespace],
            )
            .await?;
        let by_op_rows = c
            .query(
                format!(
                    "SELECT op, count(*) n, COALESCE(sum(cost_usd),0)::float8 usd, COALESCE(sum(tokens_in),0)::int8 tin,
                            COALESCE(sum(tokens_out),0)::int8 tout
                     FROM memnos_control.usage_ledger WHERE {w} GROUP BY op ORDER BY usd DESC NULLS LAST"
                )
                .as_str(),
                params![days, namespace],
            )
            .await?;
        let mut by_op = Map::new();
        for r in &by_op_rows {
            let op: Option<String> = r.get(0);
            by_op.insert(op.unwrap_or_default(), json!({"n": r.get::<_, i64>(1), "usd": r.get::<_, f64>(2), "tokens_in": r.get::<_, i64>(3), "tokens_out": r.get::<_, i64>(4)}));
        }
        let by_ns_rows = c
            .query(
                format!(
                    "SELECT namespace, count(*) n, COALESCE(sum(cost_usd),0)::float8 usd, COALESCE(sum(tokens_in + tokens_out),0)::int8 tokens
                     FROM memnos_control.usage_ledger WHERE {w} GROUP BY namespace ORDER BY usd DESC NULLS LAST"
                )
                .as_str(),
                params![days, namespace],
            )
            .await?;
        let mut by_namespace = Map::new();
        for r in &by_ns_rows {
            let ns: Option<String> = r.get(0);
            by_namespace.insert(ns.unwrap_or_default(), json!({"n": r.get::<_, i64>(1), "usd": r.get::<_, f64>(2), "tokens": r.get::<_, i64>(3)}));
        }
        let day_rows = c
            .query(
                "SELECT date_trunc('day', ts)::date::text AS day, COALESCE(sum(cost_usd),0)::float8 AS usd,
                        COALESCE(sum(tokens_in + tokens_out),0)::int8 AS tokens
                 FROM memnos_control.usage_ledger
                 WHERE ts > now() - ($1::int * interval '1 day') AND ($2::text IS NULL OR namespace=$2)
                 GROUP BY day ORDER BY day DESC",
                params![trend_days, namespace],
            )
            .await?;
        let by_day: Vec<Value> =
            day_rows.iter().map(|r| json!({"date": r.get::<_, String>(0), "usd": r.get::<_, f64>(1), "tokens": r.get::<_, i64>(2)})).collect();
        Ok(
            json!({"total_usd": t.get::<_, f64>(0), "total_tokens": t.get::<_, i64>(1), "by_op": by_op, "by_namespace": by_namespace, "by_day": by_day}),
        )
    }

    /// Spend vs optional daily/monthly (24h / 30d) thresholds.
    pub async fn budget_status(
        pool: &Pool,
        daily_usd: Option<f64>,
        monthly_usd: Option<f64>,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let daily: f64 = c
            .query_one("SELECT COALESCE(sum(cost_usd),0)::float8 FROM memnos_control.usage_ledger WHERE ts > now() - interval '1 day'", &[])
            .await?
            .get(0);
        let monthly: f64 = c
            .query_one("SELECT COALESCE(sum(cost_usd),0)::float8 FROM memnos_control.usage_ledger WHERE ts > now() - interval '30 days'", &[])
            .await?
            .get(0);
        let r6 = |x: f64| (x * 1e6).round() / 1e6;
        let daily_ok = daily_usd.map_or(true, |l| daily <= l);
        let monthly_ok = monthly_usd.map_or(true, |l| monthly <= l);
        Ok(json!({
            "daily_spend_usd": r6(daily), "monthly_spend_usd": r6(monthly),
            "daily_limit_usd": daily_usd, "monthly_limit_usd": monthly_usd,
            "daily_ok": daily_ok, "monthly_ok": monthly_ok, "exceeded": !(daily_ok && monthly_ok),
        }))
    }

    pub async fn record_eval(
        pool: &Pool,
        eval: &str,
        metric: &str,
        value: f64,
        n: Option<i32>,
        detail: Option<Value>,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.eval_runs(eval,metric,value,n,detail) VALUES($1,$2,$3::float8,$4,$5)",
            params![eval, metric, value, n, detail],
        )
        .await?;
        Ok(())
    }

    pub async fn eval_trend(
        pool: &Pool,
        eval: &str,
        metric: &str,
        limit: i64,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT ts, value::float8 AS value, n FROM memnos_control.eval_runs WHERE eval=$1 AND metric=$2
                 ORDER BY ts DESC LIMIT $3",
                params![eval, metric, limit],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn record_feedback(
        pool: &Pool,
        principal_id: Option<i64>,
        namespace: &str,
        query: &str,
        helpful: bool,
        note: Option<&str>,
    ) -> CResult<()> {
        let c = conn(pool).await?;
        c.execute(
            "INSERT INTO memnos_control.feedback(principal_id,namespace,query,helpful,note) VALUES($1,$2,$3,$4,$5)",
            params![principal_id, namespace, query, helpful, note],
        )
        .await?;
        Ok(())
    }

    /// Metrics -> actionable findings, as `[[level, message], ...]` (CRITICAL/WARN/INFO/OK).
    pub async fn health(pool: &Pool, hours: i32) -> CResult<Value> {
        let mut findings: Vec<(&str, String)> = vec![];
        for r in Self::stats(pool, hours).await? {
            let op = r["action"].as_str().unwrap_or("").to_string();
            let err = r["error_pct"].as_f64().unwrap_or(0.0);
            let p95 = r["p95_ms"].as_i64().unwrap_or(0);
            if err >= 20.0 {
                findings.push((
                    "CRITICAL",
                    format!("{op}: {err}% errors — inspect `errors`; auth/ACL or a bug"),
                ));
            } else if err >= 5.0 {
                findings.push((
                    "WARN",
                    format!("{op}: {err}% errors over {hours}h — check `errors`"),
                ));
            }
            if p95 >= 8000 {
                findings.push((
                    "CRITICAL",
                    format!("{op}: p95 {p95}ms — pool saturation/reranker/DB; scale or tune"),
                ));
            } else if p95 >= 2000 {
                findings.push((
                    "WARN",
                    format!("{op}: p95 {p95}ms — watch reranker/pool; consider lighter reranker"),
                ));
            }
            if let (true, Some(e)) = (op == "recall", r["recall_empty_pct"].as_f64()) {
                if e >= 25.0 {
                    findings.push(("WARN", format!("recall returns nothing {e}% of the time — ingest gaps or namespace mismatch")));
                }
            }
        }
        match Self::eval_trend(pool, "stale_suppression", "rate", 1)
            .await?
            .first()
        {
            Some(q) => {
                let v = q["value"].as_f64().unwrap_or(0.0);
                if v < 0.50 {
                    findings.push(("CRITICAL", format!("stale-suppression {:.0}% — memory serving STALE as current; supersession broken", v * 100.0)));
                } else if v < 0.70 {
                    findings.push(("WARN", format!("stale-suppression {:.0}% (target ~85%) — quality regressed, review consolidation", v * 100.0)));
                }
            }
            None => findings.push((
                "INFO",
                "no quality-canary eval recorded yet — run `memnos_eval.py`".to_string(),
            )),
        }
        let starts: i64 = conn(pool)
            .await?
            .query_one("SELECT count(*) FROM memnos_control.audit_log WHERE action='server_start' AND ts > now() - interval '1 hour'", &[])
            .await?
            .get(0);
        if starts >= 5 {
            findings.push((
                "CRITICAL",
                format!("{starts} server restarts in 1h — crash loop; check the server log"),
            ));
        }
        if findings.is_empty() {
            findings.push(("OK", "no issues detected".to_string()));
        }
        Ok(Value::Array(
            findings.into_iter().map(|(l, m)| json!([l, m])).collect(),
        ))
    }
}

// ================================================================ constraints, overrides, corpus
impl Control {
    /// Register an ask/block rule. `tool_matcher` is mandatory: prose can't be matched without an LLM.
    pub async fn add_constraint_enforcement(
        pool: &Pool,
        namespace: &str,
        rule_text: &str,
        enforce_level: &str,
        tool_matcher: &str,
        created_by: Option<i64>,
    ) -> CResult<i64> {
        if enforce_level != "ask" && enforce_level != "block" {
            return bad("enforce_level must be 'ask' or 'block' — 'advise' constraints are plain pinned memories, not control-plane rows");
        }
        if tool_matcher.is_empty() {
            return bad("--tool is required for --enforce ask|block: a prose rule can't be matched to a tool call deterministically without an LLM, and enforcement is LLM-free by design");
        }
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.constraint_enforcement(namespace,rule_text,enforce_level,tool_matcher,created_by)
                 VALUES($1,$2,$3,$4,$5) RETURNING id",
                params![namespace, rule_text, enforce_level, tool_matcher, created_by],
            )
            .await?;
        Ok(r.get(0))
    }

    async fn list_constraint_enforcement_on(
        c: &impl GenericClient,
        namespace: Option<&str>,
        active_only: bool,
    ) -> CResult<Vec<Value>> {
        let rows = c
            .query(
                "SELECT id, namespace, rule_text, enforce_level, tool_matcher, created_at, active
                 FROM memnos_control.constraint_enforcement
                 WHERE ($1::text IS NULL OR namespace=$1) AND (NOT $2::bool OR active) ORDER BY namespace, id",
                params![namespace, active_only],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn list_constraint_enforcement(
        pool: &Pool,
        namespace: Option<&str>,
        active_only: bool,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        Self::list_constraint_enforcement_on(&**c, namespace, active_only).await
    }

    /// Soft delete (active=false) to keep a governance trail.
    pub async fn remove_constraint_enforcement(pool: &Pool, row_id: i64) -> CResult<bool> {
        let c = conn(pool).await?;
        Ok(c.execute(
            "UPDATE memnos_control.constraint_enforcement SET active=false WHERE id=$1 AND active",
            params![row_id],
        )
        .await?
            > 0)
    }

    /// Active ask/block rules governing `ns`: its own, then same-root ancestors (nearest-first), then explicit links; deduped by id.
    pub async fn list_constraint_enforcement_fanout(pool: &Pool, ns: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let mut srcs = vec![ns.to_string()];
        srcs.extend(Self::effective_ancestors_on(&**c, ns).await?);
        srcs.extend(Self::linked_namespaces_on(&**c, ns).await?);
        let (mut seen, mut out) = (BTreeSet::new(), vec![]);
        for s in srcs {
            for r in Self::list_constraint_enforcement_on(&**c, Some(&s), true).await? {
                if seen.insert(r["id"].as_i64().unwrap_or(-1)) {
                    out.push(r);
                }
            }
        }
        Ok(out)
    }

    /// CHILD wins a precedence conflict against PARENT; child must be a ':'-prefix descendant.
    pub async fn add_constraint_override(
        pool: &Pool,
        child: &str,
        parent: &str,
        created_by: Option<i64>,
    ) -> CResult<i64> {
        if child.is_empty() || parent.is_empty() {
            return bad("child_namespace and parent_namespace are both required");
        }
        if !child.starts_with(&format!("{parent}:")) {
            return bad(format!(
                "{child:?} is not a ':'-prefix descendant of {parent:?} — override edges only apply between namespaces that are already in a default parent/child precedence relationship"
            ));
        }
        let c = conn(pool).await?;
        if let Some(r) = c
            .query_opt(
                "INSERT INTO memnos_control.constraint_overrides(child_namespace,parent_namespace,created_by) VALUES($1,$2,$3)
                 ON CONFLICT (child_namespace,parent_namespace) DO NOTHING RETURNING id",
                params![child, parent, created_by],
            )
            .await?
        {
            return Ok(r.get(0));
        }
        Ok(c.query_one("SELECT id FROM memnos_control.constraint_overrides WHERE child_namespace=$1 AND parent_namespace=$2", params![child, parent])
            .await?
            .get(0))
    }

    pub async fn list_constraint_overrides(
        pool: &Pool,
        namespace: Option<&str>,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, child_namespace, parent_namespace, created_at FROM memnos_control.constraint_overrides
                 WHERE ($1::text IS NULL OR child_namespace=$1 OR parent_namespace=$1) ORDER BY id",
                params![namespace],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn remove_constraint_override(pool: &Pool, row_id: i64) -> CResult<bool> {
        let c = conn(pool).await?;
        Ok(c.execute(
            "DELETE FROM memnos_control.constraint_overrides WHERE id=$1",
            params![row_id],
        )
        .await?
            > 0)
    }

    /// Override edges touching any of `namespaces`, as `(child, parent)` pairs.
    pub async fn get_constraint_overrides(
        pool: &Pool,
        namespaces: &[String],
    ) -> CResult<BTreeSet<(String, String)>> {
        if namespaces.is_empty() {
            return Ok(BTreeSet::new());
        }
        let c = conn(pool).await?;
        let nss = namespaces.to_vec();
        let rows = c
            .query(
                "SELECT child_namespace, parent_namespace FROM memnos_control.constraint_overrides
                 WHERE child_namespace = ANY($1) OR parent_namespace = ANY($1)",
                params![nss],
            )
            .await?;
        Ok(rows.iter().map(|r| (r.get(0), r.get(1))).collect())
    }

    /// Upsert a corpus source row (re-ingest updates count + sha).
    pub async fn corpus_record(
        pool: &Pool,
        namespace: &str,
        name: &str,
        kind: Option<&str>,
        git_sha: Option<&str>,
        count: i32,
    ) -> CResult<i64> {
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.corpus_sources(namespace,name,kind,git_sha,constraint_count) VALUES($1,$2,$3,$4,$5)
                 ON CONFLICT (namespace,name) DO UPDATE SET kind=EXCLUDED.kind, git_sha=EXCLUDED.git_sha,
                   constraint_count=EXCLUDED.constraint_count, ingested_at=now()
                 RETURNING id",
                params![namespace, name, kind, git_sha, count],
            )
            .await?;
        Ok(r.get(0))
    }

    pub async fn corpus_list(pool: &Pool, namespace: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, name, kind, git_sha, constraint_count, ingested_at FROM memnos_control.corpus_sources
                 WHERE namespace=$1 ORDER BY name",
                params![namespace],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    /// Record an approved deviation (caller has verified the constraint exists; no FK by design).
    pub async fn corpus_deviation_record(
        pool: &Pool,
        namespace: &str,
        constraint_id: i64,
        rationale: &str,
        approved_by: &str,
        until: Option<&str>,
        created_by: Option<i64>,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let r = c
            .query_one(
                "INSERT INTO memnos_control.corpus_deviations(namespace, constraint_id, rationale, approved_by, until, created_by)
                 VALUES($1,$2,$3,$4,$5,$6)
                 RETURNING id, namespace, constraint_id, rationale, approved_by, until, created_at",
                params![namespace, constraint_id, rationale, approved_by, until, created_by],
            )
            .await?;
        Ok(row_to_json(&r))
    }

    /// Descendant namespaces (':'-prefix) that have their own corpus docs, with doc counts.
    pub async fn corpus_descendants(pool: &Pool, namespace: &str) -> CResult<Vec<Value>> {
        let like = format!(
            "{}:%",
            namespace
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_")
        );
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT namespace, count(*) AS docs FROM memnos_control.corpus_sources
                 WHERE namespace LIKE $1 ESCAPE '\\' GROUP BY namespace ORDER BY namespace",
                params![like],
            )
            .await?;
        Ok(rows_json(&rows))
    }
}

// ================================================================ advisory leases
impl Control {
    /// Atomic acquire. Succeeds if free, expired, or already held by `holder_id` (renewal).
    /// Returns `{acquired, granted, holder_id, expires_at}` (`granted` = Python field name).
    pub async fn lease_acquire(
        pool: &Pool,
        namespace: &str,
        key: &str,
        holder_id: &str,
        principal_id: Option<i64>,
        ttl_seconds: f64,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let got = c
            .query_opt(
                "INSERT INTO memnos_control.leases(namespace,key,holder_id,principal_id,expires_at)
                 VALUES ($1,$2,$3,$4, now() + ($5::float8 * interval '1 second'))
                 ON CONFLICT (namespace,key) WHERE released_at IS NULL
                 DO UPDATE SET holder_id=EXCLUDED.holder_id, principal_id=EXCLUDED.principal_id,
                               acquired_at=now(), expires_at=EXCLUDED.expires_at
                 WHERE memnos_control.leases.expires_at <= now() OR memnos_control.leases.holder_id = EXCLUDED.holder_id
                 RETURNING holder_id, expires_at",
                params![namespace, key, holder_id, principal_id, ttl_seconds],
            )
            .await?;
        if let Some(r) = got {
            let (h, e): (String, DateTime<Utc>) = (r.get(0), r.get(1));
            return Ok(
                json!({"acquired": true, "granted": true, "holder_id": h, "expires_at": iso(e)}),
            );
        }
        let cur = c
            .query_opt(
                "SELECT holder_id, expires_at FROM memnos_control.leases
                 WHERE namespace=$1 AND key=$2 AND released_at IS NULL AND expires_at > now()",
                params![namespace, key],
            )
            .await?;
        Ok(match cur {
            Some(r) => {
                json!({"acquired": false, "granted": false, "holder_id": r.get::<_, String>(0), "expires_at": iso(r.get(1))})
            }
            // expired between our two statements: tell the caller to retry
            None => {
                json!({"acquired": false, "granted": false, "holder_id": null, "expires_at": null, "retry": true})
            }
        })
    }

    pub async fn lease_heartbeat(
        pool: &Pool,
        namespace: &str,
        key: &str,
        holder_id: &str,
        ttl_seconds: f64,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "UPDATE memnos_control.leases SET expires_at = now() + ($1::float8 * interval '1 second')
                 WHERE namespace=$2 AND key=$3 AND holder_id=$4 AND released_at IS NULL AND expires_at > now()
                 RETURNING expires_at",
                params![ttl_seconds, namespace, key, holder_id],
            )
            .await?;
        Ok(match r {
            Some(r) => json!({"renewed": true, "expires_at": iso(r.get(0))}),
            None => json!({"renewed": false}),
        })
    }

    pub async fn lease_release(
        pool: &Pool,
        namespace: &str,
        key: &str,
        holder_id: &str,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "UPDATE memnos_control.leases SET released_at = now()
                 WHERE namespace=$1 AND key=$2 AND holder_id=$3 AND released_at IS NULL RETURNING id",
                params![namespace, key, holder_id],
            )
            .await?;
        Ok(json!({"released": r.is_some()}))
    }

    pub async fn lease_who_holds(pool: &Pool, namespace: &str, key: &str) -> CResult<Value> {
        let c = conn(pool).await?;
        let r = c
            .query_opt(
                "SELECT holder_id, acquired_at, expires_at FROM memnos_control.leases
                 WHERE namespace=$1 AND key=$2 AND released_at IS NULL AND expires_at > now()",
                params![namespace, key],
            )
            .await?;
        Ok(match r {
            None => json!({"held": false, "holder_id": null}),
            Some(r) => {
                json!({"held": true, "holder_id": r.get::<_, String>(0), "acquired_at": iso(r.get(1)), "expires_at": iso(r.get(2))})
            }
        })
    }

    pub async fn lease_list(pool: &Pool, namespace: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT key, holder_id, acquired_at, expires_at FROM memnos_control.leases
                 WHERE namespace=$1 AND released_at IS NULL AND expires_at > now() ORDER BY acquired_at",
                params![namespace],
            )
            .await?;
        Ok(rows_json(&rows))
    }
}

// ================================================================ sessions, channels, inbox
/// Webhook argument for [`Control::session_register`]: `Unset` preserves any existing webhook;
/// `Set("")`/`Set("none")` clears it; anything else (re)arms it with a fresh signing secret.
#[derive(Clone, Debug, Default)]
pub enum Webhook {
    #[default]
    Unset,
    Set(String),
}

#[derive(Clone, Debug)]
pub struct SessionOpts {
    pub harness: String,
    pub model: String,
    pub role: String,
    pub host: String,
    pub cwd: String,
    pub status: String,
    pub current_task: String,
    pub metadata: Value,
    pub channels: Vec<String>,
    /// `{channel: "description" | {description, what_you_get, intents}}`
    pub channel_ads: Value,
    pub ttl_seconds: i64,
    pub webhook: Webhook,
    pub machine_id: String,
    pub pid: Option<i64>,
    pub tty: String,
    pub run_state: String,
    /// disambiguate with `-2`, `-3`... when another live terminal already holds `name`
    pub auto_named: bool,
}

impl Default for SessionOpts {
    fn default() -> Self {
        SessionOpts {
            harness: String::new(),
            model: String::new(),
            role: String::new(),
            host: String::new(),
            cwd: String::new(),
            status: "active".into(),
            current_task: String::new(),
            metadata: json!({}),
            channels: vec![],
            channel_ads: json!({}),
            ttl_seconds: 1800,
            webhook: Webhook::Unset,
            machine_id: String::new(),
            pid: None,
            tty: String::new(),
            run_state: "idle".into(),
            auto_named: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct HeartbeatOpts {
    pub status: Option<String>,
    pub current_task: Option<String>,
    pub metadata: Option<Value>,
    pub ttl_seconds: Option<i64>,
    pub publish_status: bool,
    pub machine_id: Option<String>,
    pub pid: Option<i64>,
    pub tty: Option<String>,
    pub run_state: Option<String>,
}

impl Default for HeartbeatOpts {
    fn default() -> Self {
        HeartbeatOpts {
            status: None,
            current_task: None,
            metadata: None,
            ttl_seconds: None,
            publish_status: true,
            machine_id: None,
            pid: None,
            tty: None,
            run_state: None,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Advert<'a> {
    pub owner_session: &'a str,
    pub description: &'a str,
    pub what_you_get: &'a str,
    pub intents: Option<&'a [String]>,
    pub metadata: Option<&'a Value>,
}

#[derive(Clone, Debug)]
pub struct PublishOpts {
    pub intent: String,
    pub metadata: Value,
    pub description: String,
    pub what_you_get: String,
    pub claimable: bool,
    pub reply_to: Option<i64>,
}

impl Default for PublishOpts {
    fn default() -> Self {
        PublishOpts {
            intent: "message".into(),
            metadata: json!({}),
            description: String::new(),
            what_you_get: String::new(),
            claimable: false,
            reply_to: None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct SendOpts {
    pub subject: String,
    pub intent: String,
    pub priority: String,
    pub thread_key: String,
    pub replaces_id: Option<i64>,
    pub replace_pending: bool,
    pub in_reply_to: Option<i64>,
    pub metadata: Value,
}

impl Default for SendOpts {
    fn default() -> Self {
        SendOpts {
            subject: String::new(),
            intent: "task".into(),
            priority: "normal".into(),
            thread_key: String::new(),
            replaces_id: None,
            replace_pending: false,
            in_reply_to: None,
            metadata: json!({}),
        }
    }
}

/// `(kind, description, what_you_get, intents)` defaults for a channel's catalog entry.
fn default_channel_ad(
    ch: &str,
    role: &str,
    harness: &str,
) -> (String, String, String, Vec<String>) {
    let all: Vec<String> = INTENTS.iter().map(|s| s.to_string()).collect();
    let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let kind = channel_kind(ch).to_string();
    let (d, w, i) = if ch == "#general" {
        (
            "Default namespace-wide broadcast channel for all sessions.".to_string(),
            "Broadcast announcements, shared context, questions, and cross-agent coordination updates.".to_string(),
            v(&["message", "status", "question", "answer", "handoff", "done"]),
        )
    } else if kind == "direct" {
        let n = &ch[1..];
        let who = if !role.is_empty() || !harness.is_empty() {
            format!(" (role={role}, harness={harness})")
        } else {
            String::new()
        };
        (
            format!("Direct 1-to-1 inbox for session '{n}'{who}."),
            format!("Direct tasks, review requests, critiques, questions, handoffs, and replies addressed to @{n}."),
            v(&["task", "review_request", "critique", "question", "answer", "handoff", "message"]),
        )
    } else if kind == "status" {
        let n = &ch["status:".len()..];
        (
            format!("Live heartbeat & task feed for session '{n}'."),
            format!("Automatic status transitions and current_task announcements whenever '{n}' updates its task."),
            v(&["status", "done"]),
        )
    } else if ch == "status:*" {
        (
            "All peer session status feeds in this namespace.".to_string(),
            "Every heartbeat task transition and status update (intent='status') across all active sessions.".to_string(),
            v(&["status", "done"]),
        )
    } else if ch == "*" {
        (
            "All topic (#...) and status (status:...) channels in this namespace.".to_string(),
            "Every broadcast/topic message and status update (excludes other sessions' private @<session> direct inboxes).".to_string(),
            all,
        )
    } else {
        (
            format!("Topic channel {ch} for multi-agent coordination."),
            format!(
                "Messages, review requests, critiques, tasks, and decisions published to {ch}."
            ),
            v(&[
                "message",
                "task",
                "review_request",
                "critique",
                "question",
                "answer",
                "handoff",
                "done",
            ]),
        )
    };
    (kind, d, w, i)
}

fn channel_kind(ch: &str) -> &'static str {
    if ch == "*" || ch == "status:*" {
        "wildcard"
    } else if ch.starts_with('@') {
        "direct"
    } else if ch.starts_with("status:") {
        "status"
    } else {
        "topic"
    }
}

fn wildcard_ads() -> Value {
    json!([
        {"channel": "status:*", "kind": "wildcard", "description": "All peer session status feeds in this namespace.",
         "what_you_get": "Every heartbeat task transition and status update (intent='status') across all active sessions.",
         "intents": ["status", "done"]},
        {"channel": "*", "kind": "wildcard", "description": "All topic (#...) and status (status:...) channels in this namespace.",
         "what_you_get": "Every broadcast/topic message and status update (excludes other sessions' private @<session> direct inboxes).",
         "intents": INTENTS}
    ])
}

macro_rules! in_tx {
    ($pool:expr, |$tx:ident| $body:expr) => {{
        let mut __c = conn($pool).await?;
        let __t = __c.transaction().await?;
        let $tx = &*__t;
        let __out = $body;
        let __out = __out?;
        __t.commit().await?;
        Ok::<_, ControlError>(__out)
    }};
}

impl Control {
    // ---- ownership + subscriptions
    /// Session names (and their `@direct` channel / inbox) belong to the registering principal until
    /// unregistered (`offline`) or reaped; heartbeat expiry alone does NOT release them.
    /// `Conflict` if owned by another non-offline principal. `claim_if_missing` places an inactive
    /// placeholder claim. Returns the existing `(principal_id, status)`.
    pub async fn verify_session_owner(
        c: &impl GenericClient,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        claim_if_missing: bool,
    ) -> CResult<Option<(i64, String)>> {
        let row = c
            .query_opt("SELECT principal_id, status FROM memnos_control.agent_sessions WHERE namespace=$1 AND name=$2", params![namespace, session_name])
            .await?
            .map(|r| (r.get::<_, i64>(0), r.get::<_, String>(1)));
        if let Some((owner, status)) = &row {
            if *owner != principal_id && status != "offline" {
                return Err(ControlError::Conflict(format!(
                    "session '{session_name}' is owned by another principal"
                )));
            }
        }
        if claim_if_missing && row.as_ref().map_or(true, |(o, _)| *o != principal_id) {
            c.execute(
                "INSERT INTO memnos_control.agent_sessions(namespace,name,principal_id,expires_at) VALUES ($1,$2,$3,now())
                 ON CONFLICT (namespace,name) DO UPDATE SET principal_id=EXCLUDED.principal_id, status='active',
                   expires_at=now(), registered_at=now(), last_heartbeat_at=now()
                 WHERE memnos_control.agent_sessions.status='offline'",
                params![namespace, session_name, principal_id],
            )
            .await?;
            c.execute(
                "DELETE FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 AND principal_id<>$3",
                params![namespace, session_name, principal_id],
            )
            .await?;
        }
        Ok(row)
    }

    async fn sub_rows_on(
        c: &impl GenericClient,
        namespace: &str,
        session: &str,
        principal_id: i64,
        channels: &[String],
        from_beginning: bool,
        new_from_beginning: bool,
    ) -> CResult<()> {
        let insert_zero = from_beginning || new_from_beginning;
        for ch in channels {
            c.execute(
                "INSERT INTO memnos_control.channel_subscriptions(namespace,session_name,principal_id,channel,cursor)
                 VALUES ($1,$2,$3,$4, CASE WHEN $5::bool THEN 0 ELSE
                     (SELECT COALESCE(MAX(id),0) FROM memnos_control.channel_messages WHERE namespace=$1) END)
                 ON CONFLICT (namespace,session_name,channel) DO UPDATE
                   SET cursor = CASE WHEN $6::bool THEN 0 ELSE memnos_control.channel_subscriptions.cursor END
                   WHERE memnos_control.channel_subscriptions.principal_id = EXCLUDED.principal_id",
                params![namespace, session, principal_id, ch, insert_zero, from_beginning],
            )
            .await?;
        }
        Ok(())
    }

    async fn ensure_catalog_on(
        c: &impl GenericClient,
        namespace: &str,
        channels: &[String],
        principal_id: i64,
    ) -> CResult<()> {
        for ch in channels.iter().filter(|c| *c != "*" && *c != "status:*") {
            let (kind, d, w, i) = default_channel_ad(ch, "", "");
            c.execute(
                "INSERT INTO memnos_control.channel_catalog(namespace,channel,kind,description,what_you_get,intents,principal_id)
                 VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
                params![namespace, ch, kind, d, w, i, principal_id],
            )
            .await?;
        }
        Ok(())
    }

    /// Upsert a channel's catalog entry; blank fields get defaults on first insert and are left alone later.
    pub async fn channel_advertise_on(
        c: &impl GenericClient,
        namespace: &str,
        channel: &str,
        principal_id: i64,
        ad: Advert<'_>,
    ) -> CResult<Value> {
        let ch = validate_channel(channel)?;
        let owner = if ad.owner_session.is_empty() {
            String::new()
        } else {
            slug(ad.owner_session, "owner_session")?
        };
        let (kind, d_desc, d_wyg, d_int) = default_channel_ad(&ch, "", "");
        let intents: Vec<String> = ad.intents.map(|i| i.to_vec()).unwrap_or_default();
        let bad_i: Vec<&String> = intents
            .iter()
            .filter(|i| !INTENTS.contains(&i.as_str()))
            .collect();
        if !bad_i.is_empty() {
            return bad(format!(
                "unknown intents {bad_i:?}; use {}",
                INTENTS.join(", ")
            ));
        }
        bound("description", ad.description, 2000)?;
        bound("what_you_get", ad.what_you_get, 2000)?;
        let meta = ad.metadata.cloned().unwrap_or_else(|| json!({}));
        bound_meta(&meta)?;
        if !owner.is_empty() {
            Self::verify_session_owner(c, namespace, &owner, principal_id, true).await?;
        }
        if ch.starts_with('@') || ch.starts_with("status:") {
            let n = ch.split_once(':').map_or(&ch[1..], |(_, n)| n);
            Self::verify_session_owner(c, namespace, n, principal_id, false).await?;
        }
        let desc = if ad.description.is_empty() {
            d_desc
        } else {
            ad.description.to_string()
        };
        let wyg = if ad.what_you_get.is_empty() {
            d_wyg
        } else {
            ad.what_you_get.to_string()
        };
        let ints = if intents.is_empty() {
            d_int
        } else {
            intents.clone()
        };
        let row = c
            .query_one(
                "INSERT INTO memnos_control.channel_catalog(namespace,channel,kind,description,what_you_get,intents,owner_session,principal_id,metadata)
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                 ON CONFLICT (namespace,channel) DO UPDATE SET
                   description  = CASE WHEN $10::text <> '' THEN EXCLUDED.description  ELSE memnos_control.channel_catalog.description END,
                   what_you_get = CASE WHEN $11::text <> '' THEN EXCLUDED.what_you_get ELSE memnos_control.channel_catalog.what_you_get END,
                   intents      = CASE WHEN cardinality($12::text[]) > 0 THEN EXCLUDED.intents ELSE memnos_control.channel_catalog.intents END,
                   owner_session= CASE WHEN $13::text <> '' THEN EXCLUDED.owner_session ELSE memnos_control.channel_catalog.owner_session END,
                   metadata     = CASE WHEN $14::jsonb <> '{}'::jsonb THEN EXCLUDED.metadata ELSE memnos_control.channel_catalog.metadata END,
                   updated_at   = now()
                 RETURNING *",
                params![namespace, ch, kind, desc, wyg, ints, owner, principal_id, meta, ad.description, ad.what_you_get, intents, owner, meta],
            )
            .await?;
        let mut out = obj(row_to_json(&row));
        let author: Option<String> = c
            .query_opt(
                "SELECT name FROM memnos_control.principals WHERE id=$1",
                params![principal_id],
            )
            .await?
            .map(|r| r.get(0));
        out.insert("author".into(), json!(author));
        Ok(Value::Object(out))
    }

    pub async fn channel_advertise(
        pool: &Pool,
        namespace: &str,
        channel: &str,
        principal_id: i64,
        ad: Advert<'_>,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        Self::channel_advertise_on(&**c, namespace, channel, principal_id, ad).await
    }

    // ---- liveness
    /// Mark local-machine sessions whose `pid` is gone `offline` (expires now) and release their claimed tasks.
    pub async fn reap_dead_local_sessions(
        c: &impl GenericClient,
        namespace: Option<&str>,
    ) -> CResult<Vec<String>> {
        let rows = c
            .query(
                "SELECT id, namespace, name, pid, machine_id, host FROM memnos_control.agent_sessions
                 WHERE status <> 'offline' AND expires_at > now() AND pid IS NOT NULL
                   AND ($1::text IS NULL OR namespace = $1)",
                params![namespace],
            )
            .await?;
        let mut reaped = vec![];
        for r in rows {
            let (id, ns, name, pid): (i64, String, String, i64) =
                (r.get(0), r.get(1), r.get(2), r.get(3));
            let (mid, host): (String, String) = (r.get(4), r.get(5));
            if same_laptop(&mid, &host) && !pid_alive(pid) {
                c.execute("UPDATE memnos_control.agent_sessions SET status='offline', expires_at=now() WHERE id=$1", params![id]).await?;
                Self::release_claims_on(c, &name, &ns).await?;
                reaped.push(format!("{ns}/{name}"));
            }
        }
        Ok(reaped)
    }

    async fn release_claims_on(c: &impl GenericClient, name: &str, namespace: &str) -> CResult<()> {
        c.execute(
            "UPDATE memnos_control.channel_messages
             SET claim_status='pending', claimed_by=NULL, claimed_by_ns=NULL, claim_lease_until=NULL
             WHERE claimable=true AND claim_status='claimed' AND claimed_by=$1 AND claimed_by_ns=$2",
            params![name, namespace],
        )
        .await?;
        Ok(())
    }

    pub async fn reap_dead_local_sessions_pool(
        pool: &Pool,
        namespace: Option<&str>,
    ) -> CResult<Vec<String>> {
        let c = conn(pool).await?;
        Self::reap_dead_local_sessions(&**c, namespace).await
    }

    /// Proof-of-life: slide `last_heartbeat_at` and extend `expires_at` to at least 2h out (non-offline only).
    pub async fn touch_session(
        c: &impl GenericClient,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
    ) -> CResult<bool> {
        let name = slug(session_name, "session name")?;
        Ok(c.execute(
            "UPDATE memnos_control.agent_sessions SET last_heartbeat_at = now(),
               expires_at = GREATEST(expires_at, now() + interval '2 hours')
             WHERE namespace=$1 AND name=$2 AND principal_id=$3 AND status <> 'offline'",
            params![namespace, name, principal_id],
        )
        .await?
            > 0)
    }

    pub async fn touch_session_pool(
        pool: &Pool,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
    ) -> CResult<bool> {
        let c = conn(pool).await?;
        Self::touch_session(&**c, namespace, session_name, principal_id).await
    }

    // ---- register / heartbeat / list / unregister
    pub async fn session_register_on(
        c: &impl GenericClient,
        namespace: &str,
        name: &str,
        principal_id: i64,
        opts: &SessionOpts,
    ) -> CResult<Value> {
        let mut name = slug(name, "session name")?;
        bound_channels(opts.channels.len())?;
        let meta = if opts.metadata.is_object() {
            opts.metadata.clone()
        } else {
            json!({})
        };
        bound_meta(&meta)?;
        let mut machine_id = opts.machine_id.clone();
        if machine_id.is_empty() {
            machine_id = meta["machine_id"].as_str().unwrap_or("").to_string();
        }
        let pid = opts.pid.or_else(|| {
            meta["pid"]
                .as_i64()
                .or_else(|| meta["pid"].as_str().and_then(|s| s.parse().ok()))
        });
        let tty = if opts.tty.is_empty() {
            meta["tty"].as_str().unwrap_or("").to_string()
        } else {
            opts.tty.clone()
        };
        let auto_named = opts.auto_named || meta["auto_named"].as_bool().unwrap_or(false);
        let mut disambiguated = false;
        if auto_named {
            Self::reap_dead_local_sessions(c, Some(namespace)).await?;
            let base: String = name.chars().take(56).collect();
            for idx in 1..20 {
                let cand = if idx == 1 {
                    base.clone()
                } else {
                    format!("{base}-{idx}")
                };
                let ex = c
                    .query_opt(
                        "SELECT principal_id, status, expires_at > now() AS live, pid, tty FROM memnos_control.agent_sessions
                         WHERE namespace=$1 AND name=$2",
                        params![namespace, cand],
                    )
                    .await?;
                let Some(ex) = ex else {
                    name = cand;
                    break;
                };
                if ex.get::<_, String>(1) == "offline" {
                    name = cand;
                    break;
                }
                if ex.get::<_, i64>(0) == principal_id {
                    let (live, ex_pid, ex_tty): (bool, Option<i64>, String) =
                        (ex.get(2), ex.get(3), ex.get(4));
                    if (pid.is_some() && ex_pid == pid)
                        || (!tty.is_empty() && ex_tty == tty)
                        || !live
                    {
                        name = cand;
                        break;
                    }
                }
            }
            disambiguated = name != slug(&base, "session name").unwrap_or_default();
        }
        let mut explicit_chans = vec![format!("@{name}")];
        for x in &opts.channels {
            explicit_chans.push(validate_channel(x)?);
        }
        let (touch_wh, wh) = match &opts.webhook {
            Webhook::Unset => (false, None),
            Webhook::Set(u) if u.is_empty() || u == "none" => (true, None),
            Webhook::Set(u) => (true, Some(u.clone())),
        };
        let wh_secret = wh.as_ref().map(|_| {
            let mut b = [0u8; 32];
            rand::thread_rng().fill_bytes(&mut b);
            hex::encode(b)
        });
        let webhook_set_sql = if touch_wh {
            "webhook=EXCLUDED.webhook, webhook_failures=0, webhook_active=true, webhook_secret=EXCLUDED.webhook_secret"
        } else {
            "webhook=memnos_control.agent_sessions.webhook, webhook_secret=memnos_control.agent_sessions.webhook_secret"
        };
        let ttl = opts.ttl_seconds.max(1) as f64;
        let q = format!(
            "INSERT INTO memnos_control.agent_sessions(namespace,name,principal_id,harness,model,role,host,cwd,status,
                 current_task,metadata,expires_at,webhook,webhook_secret,machine_id,pid,tty,run_state)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now() + ($12::float8 * interval '1 second'), $13,$14,$15,$16,$17,$18)
             ON CONFLICT (namespace,name) DO UPDATE SET
               principal_id=EXCLUDED.principal_id, harness=EXCLUDED.harness, model=EXCLUDED.model,
               role=EXCLUDED.role, host=EXCLUDED.host, cwd=EXCLUDED.cwd, status=EXCLUDED.status,
               current_task=EXCLUDED.current_task, metadata=EXCLUDED.metadata,
               registered_at=now(), last_heartbeat_at=now(), expires_at=EXCLUDED.expires_at,
               machine_id=CASE WHEN EXCLUDED.machine_id <> '' THEN EXCLUDED.machine_id ELSE memnos_control.agent_sessions.machine_id END,
               pid=COALESCE(EXCLUDED.pid, memnos_control.agent_sessions.pid),
               tty=CASE WHEN EXCLUDED.tty <> '' THEN EXCLUDED.tty ELSE memnos_control.agent_sessions.tty END,
               run_state=COALESCE(EXCLUDED.run_state, memnos_control.agent_sessions.run_state),
               {webhook_set_sql}
             WHERE memnos_control.agent_sessions.principal_id = EXCLUDED.principal_id
                OR memnos_control.agent_sessions.status = 'offline'
             RETURNING *"
        );
        let row = c
            .query_opt(
                q.as_str(),
                params![
                    namespace,
                    name,
                    principal_id,
                    opts.harness,
                    opts.model,
                    opts.role,
                    opts.host,
                    opts.cwd,
                    opts.status,
                    opts.current_task,
                    meta,
                    ttl,
                    wh,
                    wh_secret,
                    machine_id,
                    pid,
                    tty,
                    opts.run_state
                ],
            )
            .await?;
        let Some(row) = row else {
            return Err(ControlError::Conflict(format!(
                "session name '{name}' is held by another principal"
            )));
        };
        // takeover: the previous owner's subscriptions (and cursors) don't carry over
        c.execute(
            "DELETE FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 AND principal_id<>$3",
            params![namespace, name, principal_id],
        )
        .await?;
        Self::sub_rows_on(
            c,
            namespace,
            &name,
            principal_id,
            &explicit_chans,
            false,
            true,
        )
        .await?;
        Self::sub_rows_on(
            c,
            namespace,
            &name,
            principal_id,
            &["#general".to_string()],
            false,
            false,
        )
        .await?;
        let subs: Vec<String> = c
            .query("SELECT channel FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 ORDER BY channel", params![namespace, name])
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let mut out = obj(row_to_json(&row));
        out.insert("subscribed_channels".into(), json!(subs));
        let own_ad = Advert {
            owner_session: &name,
            description: &default_channel_ad(&format!("@{name}"), &opts.role, &opts.harness).1,
            ..Default::default()
        };
        Self::channel_advertise_on(c, namespace, &format!("@{name}"), principal_id, own_ad).await?;
        Self::channel_advertise_on(
            c,
            namespace,
            &format!("status:{name}"),
            principal_id,
            Advert {
                owner_session: &name,
                ..Default::default()
            },
        )
        .await?;
        Self::channel_advertise_on(c, namespace, "#general", principal_id, Advert::default())
            .await?;
        let mut seen = BTreeSet::new();
        for x in &opts.channels {
            let ch = validate_channel(x)?;
            if !seen.insert(ch.clone()) {
                continue;
            }
            let ad = opts
                .channel_ads
                .get(x)
                .or_else(|| opts.channel_ads.get(&ch));
            let (desc, wyg, ints): (String, String, Option<Vec<String>>) = match ad {
                Some(Value::String(s)) => (s.clone(), String::new(), None),
                Some(Value::Object(o)) => (
                    o.get("description")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .into(),
                    o.get("what_you_get")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .into(),
                    o.get("intents").and_then(Value::as_array).map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(String::from))
                            .collect()
                    }),
                ),
                _ => (String::new(), String::new(), None),
            };
            Self::channel_advertise_on(
                c,
                namespace,
                &ch,
                principal_id,
                Advert {
                    owner_session: &name,
                    description: &desc,
                    what_you_get: &wyg,
                    intents: ints.as_deref(),
                    metadata: None,
                },
            )
            .await?;
        }
        let all = Self::session_list_on(c, namespace, false, None, None).await?;
        let me = all
            .iter()
            .find(|s| s["name"] == json!(name))
            .cloned()
            .unwrap_or(Value::Null);
        let inbox_pending: i64 = c
            .query_one("SELECT count(*) FROM memnos_control.agent_inbox WHERE namespace=$1 AND to_session=$2 AND status='pending'", params![namespace, name])
            .await?
            .get(0);
        out.insert("direct_channel".into(), json!(format!("@{name}")));
        out.insert("status_channel".into(), json!(format!("status:{name}")));
        out.insert(
            "peers".into(),
            Value::Array(
                all.iter()
                    .filter(|s| s["name"] != json!(name))
                    .cloned()
                    .collect(),
            ),
        );
        out.insert(
            "available_channels".into(),
            Value::Array(Self::channel_list_on(c, namespace).await?),
        );
        out.insert("channels".into(), json!(subs));
        out.insert("unread".into(), me["unread"].clone());
        let q = format!(
            "SELECT s.channel, count(DISTINCT m.id) FROM memnos_control.channel_subscriptions s
             JOIN memnos_control.channel_messages m ON {SUB_MATCH}
             WHERE s.namespace=$1 AND s.session_name=$2 GROUP BY s.channel"
        );
        let mut unread_summary = Map::new();
        for r in c.query(q.as_str(), params![namespace, name]).await? {
            unread_summary.insert(r.get::<_, String>(0), json!(r.get::<_, i64>(1)));
        }
        out.insert("unread_summary".into(), Value::Object(unread_summary));
        out.insert("inbox_pending".into(), json!(inbox_pending));
        if auto_named {
            out.insert("auto_named".into(), json!(disambiguated));
        }
        out.insert("session".into(), Value::Object(out.clone()));
        Ok(Value::Object(out))
    }

    /// Upsert a named session (one transaction). A different principal can never take over a
    /// non-offline name, even after heartbeat expiry (`Conflict`).
    pub async fn session_register(
        pool: &Pool,
        namespace: &str,
        name: &str,
        principal_id: i64,
        opts: &SessionOpts,
    ) -> CResult<Value> {
        in_tx!(pool, |tx| Self::session_register_on(
            tx,
            namespace,
            name,
            principal_id,
            opts
        )
        .await)
    }

    /// Refresh liveness; `None` if the session isn't registered to this principal.
    pub async fn session_heartbeat_on(
        c: &impl GenericClient,
        namespace: &str,
        name: &str,
        principal_id: i64,
        o: &HeartbeatOpts,
    ) -> CResult<Option<Value>> {
        let name = slug(name, "session name")?;
        let Some(prev) = c
            .query_opt(
                "SELECT current_task FROM memnos_control.agent_sessions WHERE namespace=$1 AND name=$2 AND principal_id=$3",
                params![namespace, name, principal_id],
            )
            .await?
        else {
            return Ok(None);
        };
        let prev_task: String = prev.get(0);
        let ttl = o.ttl_seconds.unwrap_or(1800).max(1) as f64;
        let row = c
            .query_one(
                "UPDATE memnos_control.agent_sessions SET
                   last_heartbeat_at=now(), expires_at=now() + ($1::float8 * interval '1 second'),
                   status=COALESCE($2::text, status), current_task=COALESCE($3::text, current_task),
                   metadata=COALESCE($4::jsonb, metadata), machine_id=COALESCE($5::text, machine_id),
                   pid=COALESCE($6::bigint, pid), tty=COALESCE($7::text, tty), run_state=COALESCE($8::text, run_state)
                 WHERE namespace=$9 AND name=$10 AND principal_id=$11 RETURNING *",
                params![ttl, o.status, o.current_task, o.metadata, o.machine_id, o.pid, o.tty, o.run_state, namespace, name, principal_id],
            )
            .await?;
        // keep this session's claimed tasks leased while it is alive
        c.execute(
            "UPDATE memnos_control.channel_messages SET claim_lease_until = GREATEST(claim_lease_until, now() + interval '15 minutes')
             WHERE claimable=true AND claim_status='claimed' AND claimed_by=$1 AND claimed_by_ns=$2",
            params![name, namespace],
        )
        .await?;
        let out = row_to_json(&row);
        if let Some(task) = o
            .current_task
            .as_deref()
            .filter(|t| o.publish_status && !t.is_empty() && *t != prev_task)
        {
            let po = PublishOpts {
                intent: "status".into(),
                metadata: json!({"status": out["status"]}),
                ..Default::default()
            };
            Self::channel_publish_on(
                c,
                namespace,
                &format!("status:{name}"),
                &name,
                principal_id,
                task,
                &po,
            )
            .await?;
        }
        Ok(Some(out))
    }

    pub async fn session_heartbeat(
        pool: &Pool,
        namespace: &str,
        name: &str,
        principal_id: i64,
        o: &HeartbeatOpts,
    ) -> CResult<Option<Value>> {
        in_tx!(pool, |tx| Self::session_heartbeat_on(
            tx,
            namespace,
            name,
            principal_id,
            o
        )
        .await)
    }

    pub async fn session_list_on(
        c: &impl GenericClient,
        namespace: &str,
        include_expired: bool,
        role: Option<&str>,
        harness: Option<&str>,
    ) -> CResult<Vec<Value>> {
        Self::reap_dead_local_sessions(c, Some(namespace)).await?;
        let q = format!(
            "SELECT a.*, p.name AS author, (a.expires_at > now() AND a.status <> 'offline') AS active,
               COALESCE((SELECT array_agg(s.channel ORDER BY s.channel) FROM memnos_control.channel_subscriptions s
                         WHERE s.namespace=a.namespace AND s.session_name=a.name), '{{}}') AS channels,
               (SELECT count(DISTINCT m.id) FROM memnos_control.channel_subscriptions s
                JOIN memnos_control.channel_messages m ON {SUB_MATCH}
                WHERE s.namespace=a.namespace AND s.session_name=a.name) AS unread
             FROM memnos_control.agent_sessions a JOIN memnos_control.principals p ON p.id = a.principal_id
             WHERE a.namespace=$1 AND ($2::bool OR (a.expires_at > now() AND a.status <> 'offline'))
               AND ($3::text IS NULL OR a.role=$3) AND ($4::text IS NULL OR a.harness=$4)
             ORDER BY a.name"
        );
        let rows = c
            .query(
                q.as_str(),
                params![namespace, include_expired, role, harness],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| {
                let mut m = obj(row_to_json(r));
                m.remove("webhook_secret"); // never expose a peer's signing secret
                                            // the URL may embed a bearer token: peers only learn that push is configured
                let has_wh = m
                    .remove("webhook")
                    .map_or(false, |v| v.as_str().is_some_and(|s| !s.is_empty()));
                m.insert("has_webhook".into(), json!(has_wh));
                let same = same_laptop(&sget(&m, "machine_id"), &sget(&m, "host"));
                m.insert("same_laptop".into(), json!(same));
                let pid = m.get("pid").and_then(Value::as_i64);
                m.insert(
                    "pid_alive".into(),
                    match (same, pid) {
                        (true, Some(p)) if p != 0 => json!(pid_alive(p)),
                        _ => Value::Null,
                    },
                );
                let name = sget(&m, "name");
                m.insert("direct_channel".into(), json!(format!("@{name}")));
                m.insert("status_channel".into(), json!(format!("status:{name}")));
                Value::Object(m)
            })
            .collect())
    }

    /// Live sessions (dead local pids reaped first), without webhook URL/secret.
    pub async fn session_list(
        pool: &Pool,
        namespace: &str,
        include_expired: bool,
        role: Option<&str>,
        harness: Option<&str>,
    ) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        Self::session_list_on(&**c, namespace, include_expired, role, harness).await
    }

    /// Mark offline + release claimed tasks back to `pending`.
    pub async fn session_unregister(
        pool: &Pool,
        namespace: &str,
        name: &str,
        principal_id: i64,
    ) -> CResult<Value> {
        let name = slug(name, "session name")?;
        in_tx!(pool, |tx| async {
            let unreg = tx
                .query_opt(
                    "UPDATE memnos_control.agent_sessions SET status='offline', expires_at=now()
                     WHERE namespace=$1 AND name=$2 AND principal_id=$3 RETURNING id",
                    params![namespace, name, principal_id],
                )
                .await?
                .is_some();
            if unreg {
                Self::release_claims_on(tx, &name, namespace).await?;
            }
            Ok::<Value, ControlError>(json!({"unregistered": unreg}))
        }
        .await)
    }

    /// Most recently heartbeating live session registered from this `(host, cwd)`.
    pub async fn resolve_session_for_cwd(
        pool: &Pool,
        namespace: &str,
        host: &str,
        cwd: &str,
    ) -> CResult<Option<String>> {
        let c = conn(pool).await?;
        Ok(c.query_opt(
            "SELECT name FROM memnos_control.agent_sessions
             WHERE namespace=$1 AND host=$2 AND cwd=$3 AND status != 'offline' AND expires_at > now()
             ORDER BY last_heartbeat_at DESC LIMIT 1",
            params![namespace, host, cwd],
        )
        .await?
        .map(|r| r.get(0)))
    }

    // ---- channels
    /// Subscribe a session. A session may not subscribe to another session's `@direct` channel.
    pub async fn channel_subscribe(
        pool: &Pool,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        channels: &[String],
        from_beginning: bool,
        description: &str,
        what_you_get: &str,
    ) -> CResult<Value> {
        let session = slug(session_name, "session name")?;
        let chans: Vec<String> = channels
            .iter()
            .map(|x| validate_channel(x))
            .collect::<CResult<_>>()?;
        if chans.is_empty() {
            return bad("channels required");
        }
        bound_channels(chans.len())?;
        bound("description", description, 2000)?;
        bound("what_you_get", what_you_get, 2000)?;
        for ch in &chans {
            if ch.starts_with('@') && *ch != format!("@{session}") {
                return bad(format!("session '{session}' cannot subscribe to another session's direct channel '{ch}'"));
            }
        }
        let res = in_tx!(pool, |tx| {
            async {
            Self::verify_session_owner(tx, namespace, &session, principal_id, true).await?;
            Self::sub_rows_on(tx, namespace, &session, principal_id, &chans, from_beginning, false).await?;
            Self::ensure_catalog_on(tx, namespace, &chans, principal_id).await?;
            let subs: Vec<String> = tx
                .query(
                    "SELECT channel FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 AND principal_id=$3 ORDER BY channel",
                    params![namespace, session, principal_id],
                )
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            Ok::<Value, ControlError>(json!({"session_name": session, "subscribed_channels": subs}))
        }
        .await
        })?;
        if !description.is_empty() || !what_you_get.is_empty() {
            let c = conn(pool).await?;
            for ch in chans.iter().filter(|c| *c != "*" && *c != "status:*") {
                Self::channel_advertise_on(
                    &**c,
                    namespace,
                    ch,
                    principal_id,
                    Advert {
                        description,
                        what_you_get,
                        ..Default::default()
                    },
                )
                .await?;
            }
        }
        Ok(res)
    }

    pub async fn channel_unsubscribe(
        pool: &Pool,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        channels: &[String],
    ) -> CResult<Value> {
        let session = slug(session_name, "session name")?;
        bound_channels(channels.len())?;
        let chans: Vec<String> = channels
            .iter()
            .map(|x| validate_channel(x))
            .collect::<CResult<_>>()?;
        let c = conn(pool).await?;
        let n = c
            .execute(
                "DELETE FROM memnos_control.channel_subscriptions
                 WHERE namespace=$1 AND session_name=$2 AND principal_id=$3 AND channel = ANY($4)",
                params![namespace, session, principal_id, chans],
            )
            .await?;
        Ok(json!({"session_name": session, "unsubscribed": n}))
    }

    /// Publish to a channel. Enforces sender ownership (and `status:<sender>` for status channels),
    /// supports `claimable` competing-consumer tasks and `reply_to`, and `pg_notify`s listeners —
    /// including other namespaces subscribed to a global topic.
    pub async fn channel_publish_on(
        c: &impl GenericClient,
        namespace: &str,
        channel: &str,
        sender_session: &str,
        principal_id: i64,
        content: &str,
        o: &PublishOpts,
    ) -> CResult<Value> {
        let channel = validate_channel_opts(channel, false)?;
        let sender = slug(sender_session, "sender_session")?;
        if content.trim().is_empty() {
            return bad("content required");
        }
        if !INTENTS.contains(&o.intent.as_str()) {
            return bad(format!("intent must be one of {}", INTENTS.join(", ")));
        }
        bound_meta(&o.metadata)?;
        bound("description", &o.description, 2000)?;
        bound("what_you_get", &o.what_you_get, 2000)?;
        if channel.starts_with("status:") && channel != format!("status:{sender}") {
            return bad(format!(
                "session '{sender}' cannot publish to another session's status channel '{channel}'"
            ));
        }
        let claim_st = if o.claimable { "pending" } else { "none" };
        Self::verify_session_owner(c, namespace, &sender, principal_id, true).await?;
        Self::ensure_catalog_on(c, namespace, &[channel.clone()], principal_id).await?;
        // reply_to rides in metadata (channel_messages has no dedicated column)
        let mut meta = obj(o.metadata.clone());
        if let Some(r) = o.reply_to {
            meta.insert("reply_to".into(), json!(r));
        }
        let meta = Value::Object(meta);
        let m = c
            .query_one(
                "INSERT INTO memnos_control.channel_messages(namespace,channel,sender_session,principal_id,intent,content,metadata,claimable,claim_status)
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id, created_at",
                params![namespace, channel, sender, principal_id, o.intent, content, meta, o.claimable, claim_st],
            )
            .await?;
        let (id, created_at): (i64, DateTime<Utc>) = (m.get(0), m.get(1));
        // publishing is proof of life
        c.execute(
            "UPDATE memnos_control.agent_sessions SET last_heartbeat_at=now(), expires_at = GREATEST(expires_at, now() + interval '30 minutes')
             WHERE namespace=$1 AND name=$2 AND principal_id=$3 AND status <> 'offline' AND expires_at > now()",
            params![namespace, sender, principal_id],
        )
        .await?;
        c.execute(
            "SELECT pg_notify('memnos_channel', $1::text)",
            params![namespace],
        )
        .await?;
        if channel.starts_with('#') && channel != "#general" {
            let others = c
                .query(
                    "SELECT DISTINCT s.namespace FROM memnos_control.channel_subscriptions s
                     WHERE s.channel=$1 AND s.namespace <> $2
                       AND EXISTS (SELECT 1 FROM memnos_control.channel_catalog cc WHERE cc.channel=$1 AND cc.global_topic=true)",
                    params![channel, namespace],
                )
                .await?;
            for r in others {
                let ns: String = r.get(0);
                c.execute("SELECT pg_notify('memnos_channel', $1::text)", params![ns])
                    .await?;
            }
        }
        let author: Option<String> = c
            .query_opt(
                "SELECT name FROM memnos_control.principals WHERE id=$1",
                params![principal_id],
            )
            .await?
            .map(|r| r.get(0));
        if !o.description.is_empty() || !o.what_you_get.is_empty() {
            Self::channel_advertise_on(
                c,
                namespace,
                &channel,
                principal_id,
                Advert {
                    description: &o.description,
                    what_you_get: &o.what_you_get,
                    ..Default::default()
                },
            )
            .await?;
        }
        Ok(
            json!({"id": id, "namespace": namespace, "channel": channel, "sender_session": sender, "author": author,
                  "intent": o.intent, "content": content, "metadata": meta, "claimable": o.claimable,
                  "claim_status": claim_st, "created_at": iso(created_at)}),
        )
    }

    pub async fn channel_publish(
        pool: &Pool,
        namespace: &str,
        channel: &str,
        sender_session: &str,
        principal_id: i64,
        content: &str,
        o: &PublishOpts,
    ) -> CResult<Value> {
        in_tx!(pool, |tx| Self::channel_publish_on(
            tx,
            namespace,
            channel,
            sender_session,
            principal_id,
            content,
            o
        )
        .await)
    }

    /// Unread messages for a session's subscriptions (own subs only). An explicit unsubscribed
    /// `channel` is auto-subscribed from the beginning. `peek` never advances cursors or claims.
    /// `claimable` messages are claimed atomically on delivery (single winner; losers skip them).
    pub async fn channel_poll_on(
        c: &impl GenericClient,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        channel: Option<&str>,
        peek: bool,
        limit: i64,
    ) -> CResult<Value> {
        let session = slug(session_name, "session name")?;
        let limit = limit.clamp(1, 500);
        let ch = channel
            .filter(|c| !c.is_empty())
            .map(validate_channel)
            .transpose()?;
        if let Some(chn) = &ch {
            if chn.starts_with('@') && *chn != format!("@{session}") {
                return bad(format!(
                    "session '{session}' cannot poll another session's direct channel '{chn}'"
                ));
            }
        }
        match Self::verify_session_owner(c, namespace, &session, principal_id, false).await {
            Err(ControlError::Conflict(_)) => {
                return Ok(
                    json!({"session_name": session, "messages": [], "count": 0, "inbox_items": []}),
                )
            }
            Err(e) => return Err(e),
            Ok(_) => {}
        }
        let target = ch.clone().unwrap_or_else(|| format!("@{session}"));
        let subbed = c
            .query_opt(
                "SELECT 1 FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 AND principal_id=$3 AND channel=$4",
                params![namespace, session, principal_id, target],
            )
            .await?;
        if subbed.is_none() {
            Self::sub_rows_on(c, namespace, &session, principal_id, &[target], true, false).await?;
        }
        let mut args: Vec<Arg> = vec![
            Box::new(namespace.to_string()),
            Box::new(session.clone()),
            Box::new(principal_id),
        ];
        let chan_clause = if let Some(chn) = &ch {
            args.push(Box::new(chn.clone()));
            "AND s.channel=$4"
        } else {
            ""
        };
        args.push(Box::new(limit));
        let q = format!(
            "SELECT s.id AS sub_id, m.id, m.namespace AS origin_namespace, m.channel, m.sender_session, m.intent, m.content,
                    m.metadata, m.created_at, m.claimable, m.claim_status, m.claimed_by, m.claimed_by_ns, p.name AS author
             FROM memnos_control.channel_subscriptions s
             JOIN memnos_control.channel_messages m ON {SUB_MATCH}
             LEFT JOIN memnos_control.principals p ON p.id = m.principal_id
             WHERE s.namespace=$1 AND s.session_name=$2 AND s.principal_id=$3 {chan_clause}
             ORDER BY m.id LIMIT ${}",
            args.len()
        );
        let rows = c.query(q.as_str(), &arg_refs(&args)).await?;
        let mut delivered: Vec<Map<String, Value>> = vec![];
        if peek {
            delivered = rows.iter().map(|r| obj(row_to_json(r))).collect();
        } else {
            let mut top: BTreeMap<i64, i64> = BTreeMap::new();
            for r in &rows {
                let (sub_id, id): (i64, i64) = (r.get("sub_id"), r.get("id"));
                let e = top.entry(sub_id).or_insert(0);
                *e = (*e).max(id);
                let mut m = obj(row_to_json(r));
                if r.get::<_, bool>("claimable") {
                    let claimed = c
                        .query_opt(
                            "UPDATE memnos_control.channel_messages
                             SET claim_status='claimed', claimed_by=$1, claimed_by_ns=$2, claimed_at=now(),
                                 claim_lease_until = now() + interval '15 minutes', claim_attempts = claim_attempts + 1
                             WHERE id=$3 AND claimable=true
                               AND (claim_status='pending'
                                    OR (claim_status='claimed' AND claim_lease_until < now() AND claim_attempts < 3)
                                    OR (claimed_by=$1 AND claimed_by_ns=$2))
                             RETURNING claim_status, claimed_by, claimed_by_ns, claim_attempts",
                            params![session, namespace, id],
                        )
                        .await?;
                    // lost the race: the cursor still advances, the message is not delivered to us
                    let Some(cl) = claimed else { continue };
                    m.extend(obj(row_to_json(&cl)));
                }
                delivered.push(m);
            }
            for (sid, mx) in top {
                c.execute("UPDATE memnos_control.channel_subscriptions SET cursor=$1, last_polled_at=now() WHERE id=$2", params![mx, sid]).await?;
            }
        }
        // a message matching two overlapping subs (e.g. #x and *) is returned once
        let mut seen = BTreeSet::new();
        let msgs: Vec<Value> = delivered
            .into_iter()
            .filter(|m| seen.insert(m.get("id").and_then(Value::as_i64).unwrap_or(-1)))
            .map(|mut m| {
                m.remove("sub_id");
                Value::Object(m)
            })
            .collect();
        let inbox_items = if Self::inbox_foreign_on(c, namespace, &session, principal_id).await? {
            vec![]
        } else {
            let rows = rows_json(
                &c.query(
                    "SELECT i.*, p.name AS author FROM memnos_control.agent_inbox i LEFT JOIN memnos_control.principals p ON p.id=i.principal_id
                     WHERE i.namespace=$1 AND i.to_session=$2 AND i.status='pending'
                     ORDER BY CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, i.id LIMIT 50",
                    params![namespace, session],
                )
                .await?,
            );
            if !peek && !rows.is_empty() {
                let ids: Vec<i64> = rows
                    .iter()
                    .filter_map(|x| x.get("id").and_then(Value::as_i64))
                    .collect();
                if !ids.is_empty() {
                    c.execute(
                        "UPDATE memnos_control.agent_inbox SET status='read', read_at=COALESCE(read_at, now()) WHERE id = ANY($1) AND status='pending'",
                        params![ids],
                    )
                    .await?;
                }
            }
            rows
        };
        let sub_rows = c
            .query(
                "SELECT channel FROM memnos_control.channel_subscriptions
                 WHERE namespace=$1 AND session_name=$2 AND principal_id=$3 AND channel LIKE '#%'
                 ORDER BY channel",
                params![namespace, session, principal_id],
            )
            .await?;
        let topic_channels: Vec<String> = sub_rows.iter().map(|r| r.get::<_, String>(0)).collect();
        Ok(json!({
            "session_name": session,
            "channels": topic_channels,
            "count": msgs.len(),
            "messages": msgs,
            "inbox_items": inbox_items
        }))
    }

    pub async fn channel_poll(
        pool: &Pool,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        channel: Option<&str>,
        peek: bool,
        limit: i64,
    ) -> CResult<Value> {
        in_tx!(pool, |tx| Self::channel_poll_on(
            tx,
            namespace,
            session_name,
            principal_id,
            channel,
            peek,
            limit
        )
        .await)
    }

    /// Complete (`done`/`acted`) or release a claimed task message. `note` is stored as the resolution.
    pub async fn channel_ack(
        pool: &Pool,
        namespace: &str,
        session_name: &str,
        principal_id: i64,
        message_id: i64,
        status: &str,
        note: &str,
    ) -> CResult<Value> {
        let session = slug(session_name, "session name")?;
        if !["done", "acted", "release"].contains(&status) {
            return bad("action must be 'done', 'acted', or 'release'");
        }
        bound("resolution", note, 4000)?;
        let c = conn(pool).await?;
        Self::verify_session_owner(&**c, namespace, &session, principal_id, false).await?;
        let row = if status == "release" {
            c.query_opt(
                "UPDATE memnos_control.channel_messages
                 SET claim_status='pending', claimed_by=NULL, claimed_by_ns=NULL, claim_lease_until=NULL, resolution=$1
                 WHERE id=$2 AND claimable=true AND claimed_by=$3 RETURNING id, channel, claim_status",
                params![note, message_id, session],
            )
            .await?
        } else {
            c.query_opt(
                "UPDATE memnos_control.channel_messages SET claim_status='done', claim_lease_until=NULL, resolution=$1
                 WHERE id=$2 AND claimable=true AND (claimed_by=$3 OR claimed_by IS NULL) RETURNING id, channel, claim_status",
                params![note, message_id, session],
            )
            .await?
        };
        let Some(row) = row else {
            return bad(format!(
                "claimable message #{message_id} not found or claimed by another session"
            ));
        };
        Ok(
            json!({"ok": true, "id": row.get::<_, i64>(0), "channel": row.get::<_, String>(1),
                  "claim_status": row.get::<_, String>(2), "resolution": note}),
        )
    }

    /// `/memnos registry <topic>`: register the session, find-or-create a GLOBAL `#topic` across all
    /// readable namespaces (natural language is slugified; 1–2 edit-distance near-matches are
    /// suggested), subscribe, and optionally publish a claimable task.
    pub async fn channel_registry(
        pool: &Pool,
        namespace: &str,
        topic: &str,
        session_name: &str,
        principal_id: i64,
        task: Option<&str>,
        from_beginning: bool,
        base: &SessionOpts,
    ) -> CResult<Value> {
        let (topic_n, slugified) = normalize_topic(topic);
        let mut description = String::new();
        if slugified {
            description = format!("Topic channel: {}", topic.trim());
        }
        let ch = validate_channel_opts(&topic_n, false)?;
        if !ch.starts_with('#') {
            return bad("registry topic must be a #topic channel (not @direct or status:)");
        }
        let session: Option<String> = if session_name.trim().is_empty() {
            None
        } else {
            Some(slug(session_name, "session name")?)
        };
        if session.is_none() && task.is_some_and(|t| !t.trim().is_empty()) {
            return bad("session_name is required to publish a task");
        }
        let ropts = SessionOpts {
            harness: base.harness.clone(),
            role: base.role.clone(),
            host: base.host.clone(),
            cwd: base.cwd.clone(),
            machine_id: base.machine_id.clone(),
            pid: base.pid,
            tty: base.tty.clone(),
            ..Default::default()
        };
        in_tx!(pool, |tx| {
            async {
            let mut readable: BTreeSet<String> = Self::readable_namespaces_on(tx, principal_id, None, None).await?.into_iter().collect();
            readable.insert(namespace.to_string());
            let readable_list: Vec<String> = readable.into_iter().collect();
            if let Some(sn) = &session {
                Self::session_register_on(tx, namespace, sn, principal_id, &ropts).await?;
            }
            let existing: BTreeSet<String> = tx
                .query(
                    "SELECT channel FROM memnos_control.channel_catalog WHERE channel LIKE '#%' AND namespace = ANY($1)
                     UNION SELECT channel FROM memnos_control.channel_messages WHERE channel LIKE '#%' AND namespace = ANY($1)",
                    params![readable_list],
                )
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            let existed = existing.contains(&ch);
            let similar: Vec<String> = if existed {
                vec![]
            } else {
                existing
                    .iter()
                    .filter(|e| *e != "#general" && (1..=2).contains(&edit_distance_1_or_2(&ch.to_lowercase(), &e.to_lowercase())))
                    .cloned()
                    .collect()
            };
            Self::channel_advertise_on(
                tx,
                namespace,
                &ch,
                principal_id,
                Advert { owner_session: session.as_deref().unwrap_or(""), description: &description, ..Default::default() },
            )
            .await?;
            tx.execute(
                "UPDATE memnos_control.channel_catalog SET global_topic = true WHERE channel = $1 AND namespace = ANY($2)",
                params![ch, readable_list],
            )
            .await?;
            let sub = match &session {
                Some(sn) => {
                    tx.query_opt(
                        "SELECT cursor FROM memnos_control.channel_subscriptions WHERE namespace=$1 AND session_name=$2 AND channel=$3",
                        params![namespace, sn, ch],
                    )
                    .await?
                }
                None => None,
            };
            if let Some(session) = &session {
              if sub.is_none() {
                tx.execute(
                    "INSERT INTO memnos_control.channel_subscriptions(namespace, session_name, principal_id, channel, cursor)
                     VALUES ($1,$2,$3,$4, CASE WHEN $5::bool THEN 0 ELSE
                        COALESCE((SELECT MAX(id) FROM memnos_control.channel_messages
                                  WHERE channel=$4 AND namespace = ANY($6) AND NOT claimable), 0) END)
                     ON CONFLICT (namespace, session_name, channel) DO NOTHING",
                    params![namespace, session, principal_id, ch, from_beginning, readable_list],
                )
                .await?;
              } else if from_beginning {
                tx.execute(
                    "UPDATE memnos_control.channel_subscriptions SET cursor=0 WHERE namespace=$1 AND session_name=$2 AND channel=$3",
                    params![namespace, session, ch],
                )
                .await?;
              }
            }
            let subscribers = rows_json(
                &tx.query(
                    "SELECT DISTINCT s.session_name, s.namespace, a.harness, a.host, a.tty, a.pid,
                            COALESCE(a.expires_at > now() AND a.status <> 'offline', false) AS active
                     FROM memnos_control.channel_subscriptions s
                     LEFT JOIN memnos_control.agent_sessions a ON a.namespace = s.namespace AND a.name = s.session_name
                     WHERE s.channel = $1 AND s.namespace = ANY($2) ORDER BY s.session_name",
                    params![ch, readable_list],
                )
                .await?,
            );
            let mut recent = rows_json(
                &tx.query(
                    "SELECT id, namespace, sender_session, intent, content, created_at FROM memnos_control.channel_messages
                     WHERE channel=$1 AND namespace = ANY($2) AND recalled_at IS NULL ORDER BY id DESC LIMIT 5",
                    params![ch, readable_list],
                )
                .await?,
            );
            recent.reverse();
            let global_channels: Vec<String> = tx
                .query(
                    "SELECT DISTINCT channel FROM memnos_control.channel_catalog WHERE global_topic = true AND namespace = ANY($1) ORDER BY channel",
                    params![readable_list],
                )
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            let published = match (task.map(str::trim).filter(|t| !t.is_empty()), &session) {
                (Some(t), Some(sn)) => {
                    let po = PublishOpts { intent: "task".into(), claimable: true, ..Default::default() };
                    Self::channel_publish_on(tx, namespace, &ch, sn, principal_id, t, &po).await?
                }
                _ => Value::Null,
            };
            let active = subscribers.iter().filter(|s| s["active"] == json!(true)).count();
            Ok::<Value, ControlError>(json!({
                "channel": ch,
                "action": if existed { "subscribed_existing" } else { "created_and_subscribed" },
                "created_or_joined": if existed { "joined" } else { "created" },
                "existed": existed,
                "global_topic": true,
                "subscribed": session.is_some(),
                "namespace": namespace,
                "session_name": session,
                "subscribers": subscribers,
                "active_subscribers": active,
                "recent_messages": recent,
                "similar_channels": similar,
                "global_channels": global_channels,
                "published_task": published,
            }))
        }
        .await
        })
    }

    pub async fn channel_list_on(c: &impl GenericClient, namespace: &str) -> CResult<Vec<Value>> {
        let rows = c
            .query(
                "SELECT u.ch AS channel, cc.kind, cc.description, cc.what_you_get, cc.intents,
                        COALESCE(cc.owner_session,'') AS owner_session,
                        COALESCE(mc.n,0) AS message_count, mc.last_at AS last_message_at,
                        COALESCE(sc.subs, '{}') AS subscribers, COALESCE(mc.pubs, '{}') AS publishers
                 FROM (SELECT channel AS ch FROM memnos_control.channel_catalog WHERE namespace=$1
                       UNION SELECT channel FROM memnos_control.channel_messages WHERE namespace=$1
                       UNION SELECT channel FROM memnos_control.channel_subscriptions WHERE namespace=$1) u
                 LEFT JOIN memnos_control.channel_catalog cc ON cc.namespace=$1 AND cc.channel=u.ch
                 LEFT JOIN (SELECT channel, count(*) n, max(created_at) last_at,
                                   array_agg(DISTINCT sender_session ORDER BY sender_session) pubs
                            FROM memnos_control.channel_messages WHERE namespace=$1 GROUP BY channel) mc ON mc.channel = u.ch
                 LEFT JOIN (SELECT channel, array_agg(session_name ORDER BY session_name) subs
                            FROM memnos_control.channel_subscriptions WHERE namespace=$1 GROUP BY channel) sc ON sc.channel = u.ch
                 WHERE u.ch NOT IN ('*', 'status:*') ORDER BY u.ch",
                params![namespace],
            )
            .await?;
        Ok(rows
            .iter()
            .map(|r| {
                let mut m = obj(row_to_json(r));
                let (kind, d, w, i) = default_channel_ad(&sget(&m, "channel"), "", "");
                let fill = |m: &mut Map<String, Value>, k: &str, dflt: Value| {
                    if m.get(k)
                        .map_or(true, |v| v.is_null() || v == &json!("") || v == &json!([]))
                    {
                        m.insert(k.into(), dflt);
                    }
                };
                fill(&mut m, "kind", json!(kind));
                fill(&mut m, "description", json!(d));
                fill(&mut m, "what_you_get", json!(w));
                fill(&mut m, "intents", json!(i));
                Value::Object(m)
            })
            .collect())
    }

    pub async fn channel_list(pool: &Pool, namespace: &str) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        Self::channel_list_on(&**c, namespace).await
    }

    /// `{namespace, active_sessions, channels, wildcard_subscriptions}`.
    pub async fn discover(pool: &Pool, namespace: &str) -> CResult<Value> {
        let c = conn(pool).await?;
        Ok(json!({
            "namespace": namespace,
            "active_sessions": Self::session_list_on(&**c, namespace, false, None, None).await?,
            "channels": Self::channel_list_on(&**c, namespace).await?,
            "wildcard_subscriptions": wildcard_ads(),
        }))
    }

    /// Reap idle subscriptions, abandoned sessions, old messages, terminal inbox items and orphaned
    /// catalog channels. `dry_run` executes the same deletes in a rolled-back transaction, so the
    /// counts are exact (including cascading orphans).
    pub async fn channel_cleanup(
        pool: &Pool,
        namespace: &str,
        idle_days: i64,
        message_retention_days: i64,
        dry_run: bool,
    ) -> CResult<Value> {
        let idle = idle_days.clamp(1, 3650) as f64;
        let keep = message_retention_days.clamp(1, 3650) as f64;
        let mut c = conn(pool).await?;
        let tx = c.transaction().await?;
        let unsub = tx
            .execute(
                "DELETE FROM memnos_control.channel_subscriptions s
                 WHERE s.namespace=$1 AND COALESCE(s.last_polled_at, s.created_at) < now() - ($2::float8 * interval '1 day')
                   AND NOT EXISTS (SELECT 1 FROM memnos_control.agent_sessions a
                                   WHERE a.namespace=s.namespace AND a.name=s.session_name AND a.status <> 'offline'
                                     AND a.last_heartbeat_at >= now() - ($2::float8 * interval '1 day'))",
                params![namespace, idle],
            )
            .await?;
        let sessions = tx
            .execute(
                "DELETE FROM memnos_control.agent_sessions
                 WHERE namespace=$1 AND (status = 'offline' OR last_heartbeat_at < now() - ($2::float8 * interval '1 day'))",
                params![namespace, idle],
            )
            .await?;
        let msgs = tx
            .execute(
                "DELETE FROM memnos_control.channel_messages
                 WHERE namespace=$1 AND (created_at < now() - ($2::float8 * interval '1 day')
                       OR (recalled_at IS NOT NULL AND recalled_at < now() - interval '1 day'))",
                params![namespace, keep],
            )
            .await?;
        let ids: Vec<i64> = tx
            .query(
                "SELECT id FROM memnos_control.agent_inbox
                 WHERE namespace=$1 AND ((status IN ('acted','recalled','superseded') AND updated_at < now() - ($2::float8 * interval '1 day'))
                       OR created_at < now() - ($2::float8 * interval '2 day'))",
                params![namespace, keep],
            )
            .await?
            .iter()
            .map(|r| r.get(0))
            .collect();
        if !ids.is_empty() {
            // self-referencing FKs have no ON DELETE: detach first
            tx.execute(
                "UPDATE memnos_control.agent_inbox SET
                   replaces_id = CASE WHEN replaces_id = ANY($1) THEN NULL ELSE replaces_id END,
                   superseded_by_id = CASE WHEN superseded_by_id = ANY($1) THEN NULL ELSE superseded_by_id END,
                   in_reply_to = CASE WHEN in_reply_to = ANY($1) THEN NULL ELSE in_reply_to END
                 WHERE replaces_id = ANY($1) OR superseded_by_id = ANY($1) OR in_reply_to = ANY($1)",
                params![ids],
            )
            .await?;
            tx.execute(
                "DELETE FROM memnos_control.agent_inbox WHERE id = ANY($1)",
                params![ids],
            )
            .await?;
        }
        let catalog = tx
            .execute(
                "DELETE FROM memnos_control.channel_catalog cc
                 WHERE cc.namespace=$1 AND cc.channel <> '#general' AND cc.updated_at < now() - ($2::float8 * interval '1 day')
                   AND NOT EXISTS (SELECT 1 FROM memnos_control.channel_subscriptions s WHERE s.namespace=cc.namespace AND s.channel=cc.channel)
                   AND NOT EXISTS (SELECT 1 FROM memnos_control.channel_messages m WHERE m.namespace=cc.namespace AND m.channel=cc.channel)
                   AND (cc.kind NOT IN ('direct','status') OR NOT EXISTS (
                        SELECT 1 FROM memnos_control.agent_sessions a WHERE a.namespace=cc.namespace
                          AND a.name = CASE WHEN cc.channel LIKE '@%' THEN substr(cc.channel, 2) ELSE substr(cc.channel, 8) END))",
                params![namespace, idle],
            )
            .await?;
        if dry_run {
            tx.rollback().await?;
        } else {
            tx.commit().await?;
        }
        Ok(
            json!({"namespace": namespace, "dry_run": dry_run, "idle_days": idle as i64, "message_retention_days": keep as i64,
                  "unsubscribed": unsub, "sessions_reaped": sessions, "channel_messages_pruned": msgs,
                  "inbox_items_pruned": ids.len(), "catalog_channels_pruned": catalog}),
        )
    }
}

// ================================================================ agent inbox
impl Control {
    /// True if `to_s`'s inbox is not this principal's: registered to another principal (unless
    /// offline), or unregistered but already holding another principal's items.
    pub async fn inbox_foreign_on(
        c: &impl GenericClient,
        namespace: &str,
        to_s: &str,
        principal_id: i64,
    ) -> CResult<bool> {
        let row = c
            .query_opt("SELECT principal_id, status FROM memnos_control.agent_sessions WHERE namespace=$1 AND name=$2", params![namespace, to_s])
            .await?
            .map(|r| (r.get::<_, i64>(0), r.get::<_, String>(1)));
        if let Some((owner, status)) = &row {
            if status != "offline" {
                return Ok(*owner != principal_id);
            }
            if *owner == principal_id {
                return Ok(false);
            }
        }
        Ok(c.query_opt(
            "SELECT 1 FROM memnos_control.agent_inbox WHERE namespace=$1 AND to_session=$2 AND principal_id <> $3 LIMIT 1",
            params![namespace, to_s, principal_id],
        )
        .await?
        .is_some())
    }

    /// Fire-and-forget enqueue into `@to_session`'s inbox. `replaces_id` / (`thread_key` +
    /// `replace_pending`) supersede the sender's own not-yet-acted prior items.
    pub async fn inbox_send_on(
        c: &impl GenericClient,
        namespace: &str,
        to_session: &str,
        from_session: &str,
        principal_id: i64,
        content: &str,
        o: &SendOpts,
    ) -> CResult<Value> {
        let to_s = slug(to_session, "to_session")?;
        let from_s = slug(from_session, "from_session")?;
        if !(1..=32000).contains(&content.chars().count()) {
            return bad("content must be 1..32000 chars");
        }
        if !INTENTS.contains(&o.intent.as_str()) {
            return bad(format!("intent must be one of {}", INTENTS.join(", ")));
        }
        if !PRIORITIES.contains(&o.priority.as_str()) {
            return bad(format!("priority must be one of {}", PRIORITIES.join(", ")));
        }
        bound("subject", &o.subject, 500)?;
        bound("thread_key", &o.thread_key, 128)?;
        bound_meta(&o.metadata)?;
        Self::verify_session_owner(c, namespace, &from_s, principal_id, true).await?;
        let item = c
            .query_one(
                "INSERT INTO memnos_control.agent_inbox(namespace,to_session,from_session,principal_id,subject,content,intent,priority,thread_key,replaces_id,in_reply_to,metadata)
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *",
                params![namespace, to_s, from_s, principal_id, o.subject, content, o.intent, o.priority, o.thread_key, o.replaces_id, o.in_reply_to, o.metadata],
            )
            .await?;
        let item_id: i64 = item.get("id");
        let mut args: Vec<Arg> = vec![
            Box::new(item_id),
            Box::new(namespace.to_string()),
            Box::new(from_s.clone()),
            Box::new(principal_id),
        ];
        let mut conds = vec![];
        if let Some(r) = o.replaces_id {
            args.push(Box::new(r));
            conds.push(format!("id=${}", args.len()));
        }
        if !o.thread_key.is_empty() && o.replace_pending {
            args.push(Box::new(o.thread_key.clone()));
            args.push(Box::new(to_s.clone()));
            conds.push(format!(
                "(thread_key=${} AND to_session=${})",
                args.len() - 1,
                args.len()
            ));
        }
        let mut superseded: Vec<i64> = vec![];
        if !conds.is_empty() {
            let q = format!(
                "UPDATE memnos_control.agent_inbox SET status='superseded', superseded_by_id=$1, updated_at=now()
                 WHERE namespace=$2 AND from_session=$3 AND principal_id=$4 AND status IN ('pending','read','in_progress')
                   AND id <> $1 AND ({}) RETURNING id",
                conds.join(" OR ")
            );
            superseded = c
                .query(q.as_str(), &arg_refs(&args))
                .await?
                .iter()
                .map(|r| r.get(0))
                .collect();
            superseded.sort_unstable();
        }
        let payload =
            json!({"namespace": namespace, "channel": format!("@{to_s}"), "inbox_id": item_id})
                .to_string();
        c.execute(
            "SELECT pg_notify('memnos_channel', $1::text)",
            params![payload],
        )
        .await?;
        let author: Option<String> = c
            .query_opt(
                "SELECT name FROM memnos_control.principals WHERE id=$1",
                params![principal_id],
            )
            .await?
            .map(|r| r.get(0));
        let mut out = obj(row_to_json(&item));
        out.insert("author".into(), json!(author));
        out.insert("superseded_ids".into(), json!(superseded));
        Ok(Value::Object(out))
    }

    pub async fn inbox_send(
        pool: &Pool,
        namespace: &str,
        to_session: &str,
        from_session: &str,
        principal_id: i64,
        content: &str,
        o: &SendOpts,
    ) -> CResult<Value> {
        in_tx!(pool, |tx| Self::inbox_send_on(
            tx,
            namespace,
            to_session,
            from_session,
            principal_id,
            content,
            o
        )
        .await)
    }

    /// Sender-side recall of an inbox item (by id, or `thread_key` + `from_session`) and/or a channel
    /// message. Already-acted items cannot be recalled.
    pub async fn inbox_recall(
        pool: &Pool,
        namespace: &str,
        principal_id: i64,
        message_id: Option<i64>,
        channel_message_id: Option<i64>,
        thread_key: &str,
        from_session: &str,
        reason: &str,
    ) -> CResult<Value> {
        if message_id.is_none() && channel_message_id.is_none() && thread_key.is_empty() {
            return bad("message_id, channel_message_id or thread_key required");
        }
        bound("reason", reason, 2000)?;
        bound("thread_key", thread_key, 128)?;
        let mut out = Map::new();
        let c = conn(pool).await?;
        if message_id.is_some() || !thread_key.is_empty() {
            let rows = if let Some(id) = message_id {
                c.query("SELECT * FROM memnos_control.agent_inbox WHERE namespace=$1 AND principal_id=$2 AND id=$3 ORDER BY id", params![namespace, principal_id, id]).await?
            } else {
                let from = slug(from_session, "from_session")?;
                c.query(
                    "SELECT * FROM memnos_control.agent_inbox WHERE namespace=$1 AND principal_id=$2 AND thread_key=$3 AND from_session=$4 ORDER BY id",
                    params![namespace, principal_id, thread_key, from],
                )
                .await?
            };
            if rows.is_empty() {
                return bad("inbox message not found or not owned by caller");
            }
            let acted: Vec<&Row> = rows
                .iter()
                .filter(|r| r.get::<_, String>("status") == "acted")
                .collect();
            if message_id.is_some() && !acted.is_empty() {
                out.insert("recalled".into(), json!(false));
                out.insert("already_acted".into(), json!(true));
                out.insert("item".into(), row_to_json(acted[0]));
            } else {
                let ids: Vec<i64> = rows.iter().map(|r| r.get("id")).collect();
                let mut done = c
                    .query(
                        "UPDATE memnos_control.agent_inbox SET status='recalled', recalled_at=now(), recall_reason=$1, updated_at=now()
                         WHERE id = ANY($2) AND status IN ('pending','read','in_progress') RETURNING *",
                        params![reason, ids],
                    )
                    .await?;
                done.sort_by_key(|r| r.get::<_, i64>("id"));
                out.insert("recalled".into(), json!(!done.is_empty()));
                out.insert(
                    "already_acted".into(),
                    json!(!acted.is_empty() && done.is_empty()),
                );
                if message_id.is_some() {
                    out.insert("item".into(), row_to_json(done.first().unwrap_or(&rows[0])));
                } else {
                    out.insert(
                        "recalled_ids".into(),
                        json!(done
                            .iter()
                            .map(|r| r.get::<_, i64>("id"))
                            .collect::<Vec<_>>()),
                    );
                }
            }
        }
        if let Some(cm) = channel_message_id {
            let ok = c
                .query_opt(
                    "UPDATE memnos_control.channel_messages SET recalled_at=now(), recall_reason=$1
                     WHERE id=$2 AND namespace=$3 AND principal_id=$4 RETURNING id",
                    params![reason, cm, namespace, principal_id],
                )
                .await?
                .is_some();
            if out.is_empty() {
                out.insert("recalled".into(), json!(ok));
            } else {
                out.insert("channel_recalled".into(), json!(ok));
            }
            out.insert("channel_message_id".into(), json!(cm));
        }
        Ok(Value::Object(out))
    }

    /// Alias of [`Control::inbox_recall`].
    #[allow(clippy::too_many_arguments)]
    pub async fn inbox_retract(
        pool: &Pool,
        namespace: &str,
        principal_id: i64,
        message_id: Option<i64>,
        channel_message_id: Option<i64>,
        thread_key: &str,
        from_session: &str,
        reason: &str,
    ) -> CResult<Value> {
        Self::inbox_recall(
            pool,
            namespace,
            principal_id,
            message_id,
            channel_message_id,
            thread_key,
            from_session,
            reason,
        )
        .await
    }

    /// List a session's inbox (priority order). Another principal's inbox yields an empty list;
    /// `mark_read` flips `pending` -> `read`.
    #[allow(clippy::too_many_arguments)]
    pub async fn inbox_list_on(
        c: &impl GenericClient,
        namespace: &str,
        to_session: &str,
        principal_id: i64,
        status: &str,
        priority: Option<&str>,
        include_recalled: bool,
        mark_read: bool,
        limit: i64,
    ) -> CResult<Value> {
        let to_s = slug(to_session, "to_session")?;
        let empty = json!({"to_session": to_s, "items": [], "count": 0});
        let limit = limit.clamp(1, 500);
        let mut args: Vec<Arg> = vec![Box::new(namespace.to_string()), Box::new(to_s.clone())];
        let cond = if status == "active" {
            args.push(Box::new(
                INBOX_LIVE
                    .iter()
                    .map(|s| s.to_string())
                    .collect::<Vec<String>>(),
            ));
            format!("i.status = ANY(${})", args.len())
        } else if status == "all" {
            if include_recalled {
                "TRUE".to_string()
            } else {
                "i.status NOT IN ('recalled','superseded')".to_string()
            }
        } else if INBOX_LIVE.contains(&status)
            || ["acted", "recalled", "superseded"].contains(&status)
        {
            args.push(Box::new(status.to_string()));
            format!("i.status = ${}", args.len())
        } else {
            return bad(
                "status must be active|pending|read|in_progress|acted|recalled|superseded|all",
            );
        };
        let mut prio_clause = String::new();
        if let Some(p) = priority {
            if !PRIORITIES.contains(&p) {
                return bad(format!("priority must be one of {}", PRIORITIES.join(", ")));
            }
            args.push(Box::new(p.to_string()));
            prio_clause = format!("AND i.priority=${}", args.len());
        }
        if Self::inbox_foreign_on(c, namespace, &to_s, principal_id).await? {
            return Ok(empty);
        }
        Self::verify_session_owner(c, namespace, &to_s, principal_id, true).await?;
        args.push(Box::new(limit));
        let q = format!(
            "SELECT i.*, p.name AS author FROM memnos_control.agent_inbox i
             LEFT JOIN memnos_control.principals p ON p.id = i.principal_id
             WHERE i.namespace=$1 AND i.to_session=$2 AND {cond} {prio_clause}
             ORDER BY CASE i.priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END, i.id ASC
             LIMIT ${}",
            args.len()
        );
        let rows = c.query(q.as_str(), &arg_refs(&args)).await?;
        let mut items = rows_json(&rows);
        if mark_read {
            let ids: Vec<i64> = rows
                .iter()
                .filter(|r| r.get::<_, String>("status") == "pending")
                .map(|r| r.get("id"))
                .collect();
            if !ids.is_empty() {
                c.execute(
                    "UPDATE memnos_control.agent_inbox SET status='read', read_at=now(), updated_at=now() WHERE id = ANY($1) AND status='pending'",
                    params![ids],
                )
                .await?;
                for it in items
                    .iter_mut()
                    .filter(|it| ids.contains(&it["id"].as_i64().unwrap_or(-1)))
                {
                    it["status"] = json!("read");
                }
            }
        }
        Ok(json!({"to_session": to_s, "count": items.len(), "items": items}))
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn inbox_list(
        pool: &Pool,
        namespace: &str,
        to_session: &str,
        principal_id: i64,
        status: &str,
        priority: Option<&str>,
        include_recalled: bool,
        mark_read: bool,
        limit: i64,
    ) -> CResult<Value> {
        in_tx!(pool, |tx| Self::inbox_list_on(
            tx,
            namespace,
            to_session,
            principal_id,
            status,
            priority,
            include_recalled,
            mark_read,
            limit
        )
        .await)
    }

    /// Act on an inbox item. `action`: `read|in_progress|acted|done` (Python names) or the aliases
    /// `ack`(=read), `start`(=in_progress), `done`(=acted), `dismiss`(=acted, resolution "dismissed"),
    /// `defer`(=read, resolution "deferred"). An optional `reply` enqueues an in-thread answer back
    /// to the original sender. Recalled/superseded items report `cancelled`.
    pub async fn inbox_act(
        pool: &Pool,
        namespace: &str,
        to_session: &str,
        principal_id: i64,
        message_id: i64,
        action: &str,
        resolution: &str,
        reply: &str,
    ) -> CResult<Value> {
        let to_s = slug(to_session, "to_session")?;
        let (status, default_res) = match action {
            "read" | "ack" => ("read", ""),
            "in_progress" | "start" => ("in_progress", ""),
            "acted" | "done" => ("acted", ""),
            "dismiss" => ("acted", "dismissed"),
            "defer" => ("read", "deferred"),
            _ => return bad("action must be read|in_progress|acted|done|ack|start|dismiss|defer"),
        };
        bound("resolution", resolution, 4000)?;
        in_tx!(pool, |tx| {
            async {
            if Self::inbox_foreign_on(tx, namespace, &to_s, principal_id).await? {
                return bad("inbox message not found");
            }
            Self::verify_session_owner(tx, namespace, &to_s, principal_id, true).await?;
            let Some(row) = tx
                .query_opt(
                    "SELECT * FROM memnos_control.agent_inbox WHERE id=$1 AND namespace=$2 AND to_session=$3",
                    params![message_id, namespace, to_s],
                )
                .await?
            else {
                return bad("inbox message not found");
            };
            let cur_status: String = row.get("status");
            if cur_status == "recalled" || cur_status == "superseded" {
                return Ok(json!({"ok": false, "cancelled": true, "status": cur_status, "recall_reason": row.get::<_, String>("recall_reason"),
                                 "superseded_by_id": row.get::<_, Option<i64>>("superseded_by_id"), "item": row_to_json(&row)}));
            }
            let prior_res: String = row.get("resolution");
            let res = if !resolution.is_empty() { resolution.to_string() } else if !default_res.is_empty() { default_res.to_string() } else { prior_res };
            let updated = tx
                .query_one(
                    "UPDATE memnos_control.agent_inbox SET status=$1, resolution=$2, read_at=COALESCE(read_at, now()),
                       acted_at=CASE WHEN $1 = 'acted' THEN now() ELSE acted_at END, updated_at=now()
                     WHERE id=$3 RETURNING *",
                    params![status, res, message_id],
                )
                .await?;
            let mut out = json!({"ok": true, "cancelled": false, "item": row_to_json(&updated)});
            if !reply.is_empty() {
                let (from, subject, thread): (String, String, String) = (row.get("from_session"), row.get("subject"), row.get("thread_key"));
                let so = SendOpts {
                    subject: if subject.is_empty() { String::new() } else { format!("Re: {subject}") },
                    intent: "answer".into(),
                    thread_key: thread,
                    in_reply_to: Some(message_id),
                    ..Default::default()
                };
                out["reply_item"] = Self::inbox_send_on(tx, namespace, &from, &to_s, principal_id, reply, &so).await?;
            }
            Ok::<Value, ControlError>(out)
        }
        .await
        })
    }
}

// ================================================================ namespace subscriptions + webhook push
async fn post_json(url: &str, payload: &Value, secret: Option<&str>) -> Result<(), String> {
    let body = serde_json::to_vec(payload).map_err(|e| e.to_string())?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let mut req = client.post(url).header("Content-Type", "application/json");
    if let Some(s) = secret.filter(|s| !s.is_empty()) {
        req = req.header(
            "X-Memnos-Signature",
            format!("sha256={}", sign_body(s, &body)),
        );
    }
    let resp = req.body(body).send().await.map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    Ok(())
}

impl Control {
    /// Create a namespace subscription; the cursor starts at the namespace's current max raw_turn id.
    pub async fn subscribe(
        pool: &Pool,
        principal_id: i64,
        namespace: &str,
        webhook: Option<&str>,
    ) -> CResult<Value> {
        let c = conn(pool).await?;
        let cur: i64 = c
            .query_one(
                "SELECT COALESCE(max(id),0) FROM tenant_memnos.raw_turns WHERE namespace=$1",
                params![namespace],
            )
            .await?
            .get(0);
        let sid: i64 = c
            .query_one(
                "INSERT INTO memnos_control.subscriptions(principal_id,namespace,webhook,cursor) VALUES($1,$2,$3,$4) RETURNING id",
                params![principal_id, namespace, webhook, cur],
            )
            .await?
            .get(0);
        Ok(
            json!({"subscription_id": sid, "namespace": namespace, "cursor": cur, "webhook": webhook}),
        )
    }

    /// Poll new memories since the subscription cursor and advance it. `None` if the subscription
    /// isn't owned by this principal/namespace.
    pub async fn feed(
        pool: &Pool,
        principal_id: i64,
        subscription_id: i64,
        namespace: &str,
        limit: i64,
    ) -> CResult<Option<Value>> {
        let c = conn(pool).await?;
        let Some(sub) = c
            .query_opt("SELECT principal_id, namespace, cursor FROM memnos_control.subscriptions WHERE id=$1", params![subscription_id])
            .await?
        else {
            return Ok(None);
        };
        if sub.get::<_, i64>(0) != principal_id || sub.get::<_, String>(1) != namespace {
            return Ok(None);
        }
        let cursor: i64 = sub.get(2);
        let rows = c
            .query(
                "SELECT id, speaker, text AS content, observed_at FROM tenant_memnos.raw_turns WHERE namespace=$1 AND id > $2 ORDER BY id LIMIT $3",
                params![namespace, cursor, limit],
            )
            .await?;
        let items = rows_json(&rows);
        let newcur = items
            .last()
            .and_then(|i| i["id"].as_i64())
            .unwrap_or(cursor);
        c.execute(
            "UPDATE memnos_control.subscriptions SET cursor=$1, last_polled_at=now() WHERE id=$2",
            params![newcur, subscription_id],
        )
        .await?;
        Ok(Some(
            json!({"subscription_id": subscription_id, "items": items, "cursor": newcur}),
        ))
    }

    pub async fn list_subscriptions(pool: &Pool, principal_id: i64) -> CResult<Vec<Value>> {
        let c = conn(pool).await?;
        let rows = c
            .query(
                "SELECT id, namespace, webhook, cursor, active, delivery_failures, created_at, last_polled_at, last_delivery_at
                 FROM memnos_control.subscriptions WHERE principal_id=$1 ORDER BY id",
                params![principal_id],
            )
            .await?;
        Ok(rows_json(&rows))
    }

    pub async fn unsubscribe(
        pool: &Pool,
        principal_id: i64,
        subscription_id: i64,
    ) -> CResult<bool> {
        let c = conn(pool).await?;
        Ok(c.execute(
            "DELETE FROM memnos_control.subscriptions WHERE id=$1 AND principal_id=$2",
            params![subscription_id, principal_id],
        )
        .await?
            > 0)
    }

    /// Webhook push for namespace subscriptions, at-least-once: the cursor advances only after a 2xx.
    /// A failure bumps `delivery_failures` (cursor untouched, retried next tick); after
    /// `max_failures` the subscription is deactivated. No pool connection is held across the POST.
    pub async fn deliver_pending(
        pool: &Pool,
        batch: i64,
        max_failures: i32,
    ) -> CResult<Vec<Value>> {
        let subs = conn(pool)
            .await?
            .query(
                "SELECT id, namespace, webhook, cursor, delivery_failures FROM memnos_control.subscriptions
                 WHERE webhook IS NOT NULL AND active=true ORDER BY id",
                &[],
            )
            .await?;
        let mut results = vec![];
        for sub in subs {
            let (id, ns, url, cursor, fails0): (i64, String, String, i64, i32) =
                (sub.get(0), sub.get(1), sub.get(2), sub.get(3), sub.get(4));
            let items = conn(pool)
                .await?
                .query(
                    "SELECT id, speaker, text AS content, observed_at FROM tenant_memnos.raw_turns WHERE namespace=$1 AND id > $2 ORDER BY id LIMIT $3",
                    params![ns, cursor, batch],
                )
                .await?;
            if items.is_empty() {
                continue;
            }
            let events: Vec<Value> = items.iter().map(row_to_json).collect();
            let payload = json!({"subscription_id": id, "namespace": ns, "events": events});
            match post_json(&url, &payload, None).await {
                Ok(()) => {
                    let newcur: i64 = items.last().map_or(cursor, |r| r.get(0));
                    conn(pool)
                        .await?
                        .execute(
                            "UPDATE memnos_control.subscriptions SET cursor=$1, delivery_failures=0, last_delivery_at=now() WHERE id=$2",
                            params![newcur, id],
                        )
                        .await?;
                    results.push(
                        json!({"subscription_id": id, "delivered": items.len(), "cursor": newcur}),
                    );
                }
                Err(e) => {
                    let fails = fails0 + 1;
                    let still = fails < max_failures;
                    conn(pool).await?.execute("UPDATE memnos_control.subscriptions SET delivery_failures=$1, active=$2 WHERE id=$3", params![fails, still, id]).await?;
                    results.push(json!({"subscription_id": id, "error": e.chars().take(200).collect::<String>(), "failures": fails, "active": still}));
                }
            }
        }
        Ok(results)
    }

    /// Webhook push for named-session channel/inbox events (push-side of `/channel/wait`).
    /// At-least-once: peek channel messages + pending inbox, POST (signed with
    /// `X-Memnos-Signature: sha256=<hmac>` when the session has a secret), and only after a 2xx
    /// advance cursors and mark inbox items read. A failed POST leaves them pending for the next tick;
    /// after `max_failures` the webhook is deactivated (re-register to re-arm).
    /// shortcut: the commit step re-polls instead of acking the exact ids posted, so a message
    /// arriving between peek and commit is marked delivered without its own POST; upgrade to
    /// id-scoped commits if that window matters.
    pub async fn deliver_pending_sessions(
        pool: &Pool,
        batch: i64,
        max_failures: i32,
    ) -> CResult<Vec<Value>> {
        let sessions = conn(pool)
            .await?
            .query(
                "SELECT namespace, name, principal_id, webhook, webhook_secret, webhook_failures FROM memnos_control.agent_sessions
                 WHERE webhook IS NOT NULL AND webhook_active=true AND status != 'offline' AND expires_at > now() ORDER BY id",
                &[],
            )
            .await?;
        let mut results = vec![];
        for s in sessions {
            let (ns, name, pid, url): (String, String, i64, String) =
                (s.get(0), s.get(1), s.get(2), s.get(3));
            let (secret, fails0): (Option<String>, i32) = (s.get(4), s.get(5));
            let attempt: CResult<Option<usize>> = async {
                let polled = Self::channel_poll(pool, &ns, &name, pid, None, true, batch).await?;
                let inbox = Self::inbox_list(pool, &ns, &name, pid, "pending", None, false, false, batch).await?;
                let n_msgs = polled["count"].as_u64().unwrap_or(0) as usize;
                let n_inbox = inbox["items"].as_array().map_or(0, Vec::len);
                if n_msgs == 0 && n_inbox == 0 {
                    return Ok(None);
                }
                let payload = json!({"session_name": name, "namespace": ns, "channel_messages": polled["messages"], "inbox_items": inbox["items"]});
                post_json(&url, &payload, secret.as_deref()).await.map_err(ControlError::Db)?;
                Self::channel_poll(pool, &ns, &name, pid, None, false, batch).await?;
                Self::inbox_list(pool, &ns, &name, pid, "pending", None, false, true, batch).await?;
                conn(pool).await?.execute("UPDATE memnos_control.agent_sessions SET webhook_failures=0 WHERE namespace=$1 AND name=$2", params![ns, name]).await?;
                Ok(Some(n_msgs + n_inbox))
            }
            .await;
            match attempt {
                Ok(None) => {}
                Ok(Some(n)) => results.push(json!({"session_name": name, "delivered": n})),
                Err(e) => {
                    let fails = fails0 + 1;
                    let still = fails < max_failures;
                    conn(pool)
                        .await?
                        .execute(
                            "UPDATE memnos_control.agent_sessions SET webhook_failures=$1, webhook_active=$2 WHERE namespace=$3 AND name=$4",
                            params![fails, still, ns, name],
                        )
                        .await?;
                    results.push(json!({"session_name": name, "error": e.to_string().chars().take(200).collect::<String>(), "failures": fails, "active": still}));
                }
            }
        }
        Ok(results)
    }
}

// ================================================================ tests (pure logic; DB paths need a live Postgres)
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hash_token_is_sha256_hex() {
        assert_eq!(
            hash_token("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(hash_token("").len(), 64);
        assert_ne!(hash_token("mnk_a"), hash_token("mnk_b"));
    }

    #[test]
    fn slug_accepts_valid_and_rejects_invalid() {
        assert_eq!(slug("agy-webapp", "n").unwrap(), "agy-webapp");
        assert_eq!(slug("  @auth-redesign ", "n").unwrap(), "auth-redesign");
        assert!(slug("a.b_c-d", "n").is_ok());
        for bad in ["", "-x", "has space", "a/b", &"x".repeat(65)] {
            assert!(
                matches!(slug(bad, "n"), Err(ControlError::BadRequest(_))),
                "{bad:?} should be rejected"
            );
        }
        assert!(slug(&"x".repeat(64), "n").is_ok());
    }

    #[test]
    fn validate_channel_forms() {
        assert_eq!(
            validate_channel("#payments-sync").unwrap(),
            "#payments-sync"
        );
        assert_eq!(validate_channel("payments-sync").unwrap(), "#payments-sync");
        assert_eq!(validate_channel("@agy-webapp").unwrap(), "@agy-webapp");
        assert_eq!(
            validate_channel("status:agy-webapp").unwrap(),
            "status:agy-webapp"
        );
        assert_eq!(validate_channel("*").unwrap(), "*");
        assert_eq!(validate_channel("status:*").unwrap(), "status:*");
        assert_eq!(validate_channel("#pay*").unwrap(), "#pay*");
        assert!(validate_channel_opts("*", false).is_err());
        assert!(validate_channel_opts("status:*", false).is_err());
        assert!(validate_channel_opts("#pay*", false).is_err());
        for bad in ["#", "#has space", "@", "status:", "#a/b", "##x"] {
            assert!(validate_channel(bad).is_err(), "{bad:?} should be rejected");
        }
    }

    #[test]
    fn ancestors_are_nearest_first_and_prefix_only() {
        assert_eq!(
            namespace_ancestors("acme:payments:api"),
            vec!["acme:payments", "acme"]
        );
        assert_eq!(namespace_ancestors("acme:payments"), vec!["acme"]);
        assert!(namespace_ancestors("acme").is_empty());
        assert!(namespace_ancestors("").is_empty());
        // siblings sharing a leaf are not related
        assert!(!namespace_ancestors("a:x:api").contains(&"b:x".to_string()));
    }

    #[test]
    fn edit_distance_matches_near_topics() {
        assert_eq!(edit_distance_1_or_2("#payments-sync", "#payments-sync"), 0);
        assert_eq!(edit_distance_1_or_2("#payment-sync", "#payments-sync"), 1);
        assert_eq!(edit_distance_1_or_2("#paymnts-sync", "#payments-sync"), 1);
        assert_eq!(edit_distance_1_or_2("#auth", "#authxx"), 2);
        assert_eq!(edit_distance_1_or_2("#auth", "#billing-pipeline"), 99);
        assert!(edit_distance_1_or_2("#auth-redesign", "#payments-sync") > 2);
    }

    #[test]
    fn topic_slugification() {
        assert_eq!(
            normalize_topic("payments sync"),
            ("#payments-sync".into(), true)
        );
        assert_eq!(
            normalize_topic("  Auth Redesign!! "),
            ("#auth-redesign".into(), true)
        );
        assert_eq!(
            normalize_topic("#payments-sync"),
            ("#payments-sync".into(), false)
        );
        assert_eq!(normalize_topic("agy-webapp"), ("agy-webapp".into(), false));
        assert_eq!(normalize_topic("!!!"), ("!!!".into(), false)); // nothing slug-able: left for validation to reject
        let long = normalize_topic(&"word ".repeat(40)).0;
        assert!(long.len() <= 61);
    }

    #[test]
    fn grant_matching_exact_prefix_and_star() {
        assert!(Control::grant_matches("*", "any:thing"));
        assert!(Control::grant_matches("team:eng:*", "team:eng:api"));
        assert!(!Control::grant_matches("team:eng:*", "team:engineering"));
        assert!(Control::grant_matches("proj:memnos", "proj:memnos"));
        assert!(!Control::grant_matches("proj:memnos", "proj:memnos:sub"));
    }

    #[test]
    fn signature_is_hmac_sha256() {
        // RFC 4231 test case 2
        assert_eq!(
            sign_body("Jefe", b"what do ya want for nothing?"),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn default_ads_cover_every_kind() {
        assert_eq!(
            default_channel_ad("@agy-webapp", "dev", "claude-code").0,
            "direct"
        );
        assert_eq!(default_channel_ad("status:agy-webapp", "", "").0, "status");
        assert_eq!(default_channel_ad("#payments-sync", "", "").0, "topic");
        assert_eq!(default_channel_ad("*", "", "").0, "wildcard");
        assert!(default_channel_ad("@agy-webapp", "dev", "claude-code")
            .1
            .contains("role=dev"));
    }

    /// End-to-end against a REAL Postgres: exercises every SQL family.
    /// Use a SCRATCH database, never a live one (it runs `init` and leaves test principals behind):
    /// `MEMNOS_TEST_DSN=postgresql://.../scratch cargo test --lib control::tests::live_db_smoke`.
    /// Skipped (passes trivially) when the env var is unset. `tenant_memnos.*` paths run only if
    /// those tables exist in the target database.
    #[tokio::test]
    async fn live_db_smoke() {
        let Ok(dsn) = std::env::var("MEMNOS_TEST_DSN") else {
            return;
        };
        // own pool (with a runtime set): db::build_pool() currently sets a wait_timeout without one
        let cfg: tokio_postgres::Config = dsn.parse().unwrap();
        let mgr = deadpool_postgres::Manager::new(cfg, tokio_postgres::NoTls);
        let pool = Pool::builder(mgr)
            .max_size(4)
            .runtime(deadpool_postgres::Runtime::Tokio1)
            .build()
            .unwrap();
        Control::init(&pool).await.unwrap();
        Control::init(&pool).await.unwrap(); // idempotent

        let sfx = format!("{:08x}", rand::random::<u32>());
        let (p1, p2) = (
            Control::create_principal(&pool, &format!("ctl-a-{sfx}"), "user")
                .await
                .unwrap(),
            Control::create_principal(&pool, &format!("ctl-b-{sfx}"), "user")
                .await
                .unwrap(),
        );

        // ---- tokens + ACL
        let tok = Control::mint_token(&pool, p1, Some("test"), Some(1))
            .await
            .unwrap();
        assert!(tok.starts_with("mnk_"));
        assert_eq!(Control::authenticate(&pool, &tok).await.unwrap(), Some(p1));
        assert_eq!(Control::list_tokens(&pool, p1).await.unwrap().len(), 1);
        Control::revoke_token(&pool, &tok).await.unwrap();
        assert_eq!(Control::authenticate(&pool, &tok).await.unwrap(), None);
        assert!(Control::list_principals(&pool)
            .await
            .unwrap()
            .iter()
            .any(|p| p["id"] == p1));
        assert_eq!(
            Control::principal_info(&pool, p1).await.unwrap().unwrap()["kind"],
            "user"
        );

        let ns = format!("test:ctl-{sfx}");
        let child = format!("{ns}:child");
        Control::grant(&pool, p1, &format!("{ns}:*"), true, true)
            .await
            .unwrap();
        assert!(Control::authorize(&pool, p1, &child, true).await.unwrap());
        assert!(!Control::authorize(&pool, p2, &ns, false).await.unwrap());
        assert_eq!(
            Control::authorized_namespaces(&pool, p1)
                .await
                .unwrap()
                .len(),
            1
        );
        assert!(!Control::is_admin(&pool, p1).await.unwrap());

        // ---- roles
        let role = format!("role-{sfx}");
        let role_ns = format!("shared:{sfx}");
        Control::create_role(&pool, &role, Some("d"), Some(p1))
            .await
            .unwrap();
        Control::grant_role(&pool, &role, &role_ns, true, false)
            .await
            .unwrap();
        Control::add_role_member(&pool, &role, p2).await.unwrap();
        assert!(Control::authorize(&pool, p2, &role_ns, false)
            .await
            .unwrap());
        assert!(!Control::authorize(&pool, p2, &role_ns, true).await.unwrap());
        assert_eq!(
            Control::list_role_grants(&pool, &role).await.unwrap().len(),
            1
        );
        assert_eq!(
            Control::list_role_members(&pool, &role)
                .await
                .unwrap()
                .len(),
            1
        );
        assert!(Control::list_roles(&pool)
            .await
            .unwrap()
            .iter()
            .any(|r| r["name"] == role.as_str()));
        Control::grant_role(&pool, &role, "*", true, false)
            .await
            .unwrap();
        assert!(Control::is_admin(&pool, p2).await.unwrap());
        Control::revoke_role_grant(&pool, &role, "*").await.unwrap();
        assert!(Control::remove_role_member(&pool, &role, p2).await.unwrap());
        assert!(Control::delete_role(&pool, &role).await.unwrap());

        // ---- namespaces, links (cycle rejected), ancestors
        Control::create_namespace(&pool, &ns, Some(p1), Some("scratch"))
            .await
            .unwrap();
        Control::set_namespace_kind(&pool, &ns, "knowledge")
            .await
            .unwrap();
        assert!(Control::set_namespace_kind(&pool, &ns, "bogus")
            .await
            .is_err());
        assert!(Control::readable_namespaces(&pool, p1, None, None)
            .await
            .unwrap()
            .contains(&ns));
        assert_eq!(
            Control::effective_ancestors(&pool, &child).await.unwrap(),
            vec![ns.clone(), "test".to_string()]
        );
        Control::set_namespace_inherit_ancestors(&pool, &child, false)
            .await
            .unwrap();
        assert!(Control::effective_ancestors(&pool, &child)
            .await
            .unwrap()
            .is_empty());
        let (la, lb) = (format!("lnk:{sfx}:a"), format!("lnk:{sfx}:b"));
        Control::link_namespaces(&pool, &la, &lb, Some(p1), "link")
            .await
            .unwrap();
        assert!(matches!(
            Control::link_namespaces(&pool, &lb, &la, Some(p1), "link").await,
            Err(ControlError::BadRequest(_))
        ));
        assert_eq!(
            Control::linked_namespaces(&pool, &la).await.unwrap(),
            vec![lb.clone()]
        );
        assert_eq!(
            Control::list_links(&pool, Some(&la)).await.unwrap().len(),
            1
        );
        assert!(Control::unlink_namespaces(&pool, &la, &lb).await.unwrap());

        // ---- bindings, hosts, nudges
        let key = "github.com/acme/payments-sync";
        let b1 = Control::upsert_binding(&pool, p1, "repo", key, &ns, None)
            .await
            .unwrap();
        let b2 = Control::upsert_binding(&pool, p1, "repo", key, &child, None)
            .await
            .unwrap();
        assert_eq!(b1["id"], b2["id"]);
        Control::upsert_binding(&pool, p1, "host_path", "/work/x", &ns, Some("hostA"))
            .await
            .unwrap();
        assert_eq!(
            Control::resolve_binding(&pool, p1, Some(key), None, None)
                .await
                .unwrap()
                .unwrap()["namespace"],
            child.as_str()
        );
        assert!(
            Control::resolve_binding(&pool, p1, None, Some("hostA"), Some("/work/x"))
                .await
                .unwrap()
                .is_some()
        );
        assert_eq!(Control::list_bindings(&pool, p1).await.unwrap().len(), 2);
        assert!(
            Control::delete_binding(&pool, p1, b1["id"].as_i64().unwrap())
                .await
                .unwrap()
        );
        assert!(
            !Control::delete_binding(&pool, p2, b2["id"].as_i64().unwrap())
                .await
                .unwrap()
        ); // not p2's
        Control::upsert_host(&pool, p1, "laptop-a", Some("Dev laptop"))
            .await
            .unwrap();
        assert_eq!(Control::list_hosts(&pool, p1).await.unwrap().len(), 1);
        Control::record_nudge(&pool, p1, "a", "b", "looks like b", Some(7))
            .await
            .unwrap();
        assert_eq!(
            Control::take_pending_nudges(&pool, p1, 10)
                .await
                .unwrap()
                .len(),
            1
        );
        assert!(Control::take_pending_nudges(&pool, p1, 10)
            .await
            .unwrap()
            .is_empty());
        Control::write_recap(&pool, p1, 7, 10).await.unwrap();

        // ---- sessions + channels + competing consumers
        let topic = format!("#payments-sync-{sfx}");
        let opts = SessionOpts {
            channels: vec![topic.clone()],
            ..Default::default()
        };
        let reg = Control::session_register(&pool, &ns, "agy-a", p1, &opts)
            .await
            .unwrap();
        assert!(
            reg["unread_summary"].is_object()
                && reg["subscribed_channels"].as_array().unwrap().len() >= 3
        );
        for n in ["agy-b", "agy-c"] {
            Control::session_register(&pool, &ns, n, p1, &opts)
                .await
                .unwrap();
        }
        let e = Control::session_register(&pool, &ns, "agy-a", p2, &SessionOpts::default()).await;
        assert!(matches!(e, Err(ControlError::Conflict(_))), "{e:?}"); // live name can't be taken over
                                                                       // auto_named disambiguation across two terminals
        let mid = nsresolve::machine_id();
        let t1 = SessionOpts {
            machine_id: mid.clone(),
            pid: Some(std::process::id() as i64),
            tty: "ttys001".into(),
            auto_named: true,
            ..Default::default()
        };
        let t2 = SessionOpts {
            tty: "ttys002".into(),
            pid: Some(std::process::id() as i64 + 1),
            ..t1.clone()
        };
        assert_eq!(
            Control::session_register(&pool, &ns, "claude-x", p1, &t1)
                .await
                .unwrap()["name"],
            "claude-x"
        );
        assert_eq!(
            Control::session_register(&pool, &ns, "claude-x", p1, &t2)
                .await
                .unwrap()["name"],
            "claude-x-2"
        );

        let hb = HeartbeatOpts {
            current_task: Some("syncing ledger".into()),
            ..Default::default()
        };
        assert!(Control::session_heartbeat(&pool, &ns, "agy-a", p1, &hb)
            .await
            .unwrap()
            .is_some());
        assert!(Control::session_heartbeat(&pool, &ns, "nobody", p2, &hb)
            .await
            .unwrap()
            .is_none());
        assert!(Control::touch_session_pool(&pool, &ns, "agy-a", p1)
            .await
            .unwrap());

        let po = PublishOpts {
            intent: "task".into(),
            claimable: true,
            ..Default::default()
        };
        let m = Control::channel_publish(&pool, &ns, &topic, "agy-a", p1, "rotate keys", &po)
            .await
            .unwrap();
        let id = m["id"].as_i64().unwrap();
        assert_eq!(m["claim_status"], "pending");
        let peek = Control::channel_poll(&pool, &ns, "agy-b", p1, Some(&topic), true, 50)
            .await
            .unwrap();
        assert_eq!(peek["count"], 1); // peek doesn't claim or advance
        let (b, c) = (
            Control::channel_poll(&pool, &ns, "agy-b", p1, Some(&topic), false, 50)
                .await
                .unwrap(),
            Control::channel_poll(&pool, &ns, "agy-c", p1, Some(&topic), false, 50)
                .await
                .unwrap(),
        );
        assert_eq!(
            b["count"].as_u64().unwrap() + c["count"].as_u64().unwrap(),
            1,
            "single winner: {b} {c}"
        );
        let (winner, loser) = if b["count"] == 1 {
            ("agy-b", "agy-c")
        } else {
            ("agy-c", "agy-b")
        };
        let rel = Control::channel_ack(&pool, &ns, winner, p1, id, "release", "")
            .await
            .unwrap();
        assert_eq!(rel["claim_status"], "pending");
        let again = Control::channel_poll(&pool, &ns, loser, p1, Some(&topic), false, 50)
            .await
            .unwrap();
        assert_eq!(again["messages"][0]["claimed_by"], loser); // released task is claimable again
        let done = Control::channel_ack(&pool, &ns, loser, p1, id, "done", "ok")
            .await
            .unwrap();
        assert_eq!(done["claim_status"], "done");
        assert!(
            Control::channel_ack(&pool, &ns, winner, p1, id + 99999, "done", "")
                .await
                .is_err()
        );

        Control::channel_subscribe(&pool, &ns, "agy-b", p1, &["#extra".into()], false, "d", "w")
            .await
            .unwrap();
        assert!(Control::channel_subscribe(
            &pool,
            &ns,
            "agy-b",
            p1,
            &["@agy-a".into()],
            false,
            "",
            ""
        )
        .await
        .is_err());
        assert_eq!(
            Control::channel_unsubscribe(&pool, &ns, "agy-b", p1, &["#extra".into()])
                .await
                .unwrap()["unsubscribed"],
            1
        );
        Control::channel_advertise(
            &pool,
            &ns,
            "#adv",
            p1,
            Advert {
                owner_session: "agy-a",
                description: "adv",
                ..Default::default()
            },
        )
        .await
        .unwrap();
        let disc = Control::discover(&pool, &ns).await.unwrap();
        assert!(disc["channels"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["channel"] == topic.as_str()));

        // dead local pid reaped + same_laptop flags
        let dead = SessionOpts {
            machine_id: mid,
            pid: Some(999_999),
            ..Default::default()
        };
        Control::session_register(&pool, &ns, "ghost", p1, &dead)
            .await
            .unwrap();
        let live = Control::session_list(&pool, &ns, false, None, None)
            .await
            .unwrap();
        assert!(
            live.iter().all(|s| s["name"] != "ghost"),
            "ghost should be reaped"
        );
        let me = live.iter().find(|s| s["name"] == "claude-x").unwrap();
        assert_eq!(me["same_laptop"], true);
        assert!(me.get("webhook_secret").is_none() && me["has_webhook"] == false);
        assert!(Control::resolve_session_for_cwd(&pool, &ns, "h", "/c")
            .await
            .unwrap()
            .is_none());

        // ---- inbox: send, replace, list/mark_read, act+reply, recall
        let i1 = Control::inbox_send(
            &pool,
            &ns,
            "agy-b",
            "agy-a",
            p1,
            "review PR",
            &SendOpts {
                thread_key: "t1".into(),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        let so = SendOpts {
            thread_key: "t1".into(),
            replace_pending: true,
            priority: "high".into(),
            ..Default::default()
        };
        let i2 = Control::inbox_send(&pool, &ns, "agy-b", "agy-a", p1, "review PR v2", &so)
            .await
            .unwrap();
        assert_eq!(i2["superseded_ids"], json!([i1["id"]]));
        let inbox = Control::inbox_list(&pool, &ns, "agy-b", p1, "active", None, false, true, 50)
            .await
            .unwrap();
        assert_eq!(inbox["count"], 1);
        assert_eq!(inbox["items"][0]["status"], "read");
        let acted = Control::inbox_act(
            &pool,
            &ns,
            "agy-b",
            p1,
            i2["id"].as_i64().unwrap(),
            "done",
            "shipped",
            "LGTM",
        )
        .await
        .unwrap();
        assert_eq!(acted["item"]["status"], "acted");
        assert_eq!(acted["reply_item"]["to_session"], "agy-a");
        let foreign =
            Control::inbox_list(&pool, &ns, "agy-b", p2, "active", None, false, false, 50)
                .await
                .unwrap();
        assert_eq!(foreign["count"], 0);
        let i3 = Control::inbox_send(
            &pool,
            &ns,
            "agy-b",
            "agy-a",
            p1,
            "oops",
            &SendOpts::default(),
        )
        .await
        .unwrap();
        let rc = Control::inbox_retract(&pool, &ns, p1, i3["id"].as_i64(), None, "", "", "mistake")
            .await
            .unwrap();
        assert_eq!(rc["recalled"], true);
        let dry = Control::channel_cleanup(&pool, &ns, 1, 1, true)
            .await
            .unwrap();
        assert_eq!(dry["dry_run"], true);
        assert!(
            Control::channel_cleanup(&pool, &ns, 1, 1, false)
                .await
                .unwrap()["dry_run"]
                == false
        );

        // ---- registry (global topics, near-match suggestions, optional session)
        let reg = Control::channel_registry(
            &pool,
            &ns,
            "payments sync",
            "agy-a",
            p1,
            Some("do the thing"),
            false,
            &SessionOpts::default(),
        )
        .await
        .unwrap();
        assert_eq!(reg["channel"], "#payments-sync");
        assert_eq!(reg["created_or_joined"], "created");
        assert_eq!(reg["published_task"]["claimable"], true);
        let near = Control::channel_registry(
            &pool,
            &ns,
            "payment-sync",
            "",
            p1,
            None,
            false,
            &SessionOpts::default(),
        )
        .await
        .unwrap();
        assert_eq!(near["subscribed"], false);
        assert!(near["similar_channels"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c == "#payments-sync"));
        assert_eq!(
            Control::session_unregister(&pool, &ns, "agy-a", p1)
                .await
                .unwrap()["unregistered"],
            true
        );

        // ---- global topic across namespaces: one claimable task, claimed from the sibling namespace
        let sib = format!("{ns}:subproj");
        let joined = Control::channel_registry(
            &pool,
            &sib,
            "#payments-sync",
            "agy-sib",
            p1,
            None,
            true,
            &SessionOpts::default(),
        )
        .await
        .unwrap();
        assert_eq!(joined["existed"], true);
        assert_eq!(joined["created_or_joined"], "joined");
        Control::session_register(&pool, &ns, "agy-w", p1, &SessionOpts::default())
            .await
            .unwrap();
        let t = Control::channel_publish(
            &pool,
            &ns,
            "#payments-sync",
            "agy-w",
            p1,
            "cross-ns job",
            &po,
        )
        .await
        .unwrap();
        let got = Control::channel_poll(
            &pool,
            &sib,
            "agy-sib",
            p1,
            Some("#payments-sync"),
            false,
            50,
        )
        .await
        .unwrap();
        let hit = got["messages"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["id"] == t["id"])
            .expect("sibling namespace sees the global topic");
        assert_eq!(
            (
                hit["origin_namespace"].as_str(),
                hit["claimed_by"].as_str(),
                hit["claimed_by_ns"].as_str()
            ),
            (Some(ns.as_str()), Some("agy-sib"), Some(sib.as_str()))
        );
        // a principal with no grant on `ns` must NOT see it
        let reg_other = Control::channel_registry(
            &pool,
            &format!("other:{sfx}"),
            "#payments-sync",
            "agy-x",
            p2,
            None,
            true,
            &SessionOpts::default(),
        )
        .await
        .unwrap();
        let _ = reg_other;
        let leak = Control::channel_poll(
            &pool,
            &format!("other:{sfx}"),
            "agy-x",
            p2,
            Some("#payments-sync"),
            true,
            50,
        )
        .await
        .unwrap();
        assert!(
            leak["messages"]
                .as_array()
                .unwrap()
                .iter()
                .all(|m| m["origin_namespace"] != ns.as_str()),
            "ACL leak: {leak}"
        );
        Control::delete_namespace(&pool, &sib, false).await.unwrap();
        Control::delete_namespace(&pool, &format!("other:{sfx}"), false)
            .await
            .unwrap();

        // ---- leases
        let l1 = Control::lease_acquire(&pool, &ns, "deploy", "h1", Some(p1), 60.0)
            .await
            .unwrap();
        assert_eq!(l1["acquired"], true);
        assert_eq!(
            Control::lease_acquire(&pool, &ns, "deploy", "h2", Some(p1), 60.0)
                .await
                .unwrap()["acquired"],
            false
        );
        assert_eq!(
            Control::lease_acquire(&pool, &ns, "deploy", "h1", Some(p1), 60.0)
                .await
                .unwrap()["acquired"],
            true
        );
        assert_eq!(
            Control::lease_heartbeat(&pool, &ns, "deploy", "h1", 60.0)
                .await
                .unwrap()["renewed"],
            true
        );
        assert_eq!(
            Control::lease_who_holds(&pool, &ns, "deploy")
                .await
                .unwrap()["held"],
            true
        );
        assert_eq!(Control::lease_list(&pool, &ns).await.unwrap().len(), 1);
        assert_eq!(
            Control::lease_release(&pool, &ns, "deploy", "h1")
                .await
                .unwrap()["released"],
            true
        );
        assert_eq!(
            Control::lease_who_holds(&pool, &ns, "deploy")
                .await
                .unwrap()["held"],
            false
        );

        // ---- audit, stats, usage, health
        Control::audit(
            &pool,
            Some(p1),
            "recall",
            Some(&ns),
            true,
            Some(json!({"q": "x"})),
            Some(12),
            Some(0),
            Some(200),
        )
        .await
        .unwrap();
        Control::audit(
            &pool,
            Some(p1),
            "remember",
            Some(&ns),
            false,
            None,
            Some(30),
            None,
            Some(500),
        )
        .await
        .unwrap();
        assert!(!Control::recent_audit(&pool, 5, 0).await.unwrap().is_empty());
        assert!(Control::audit_total(&pool).await.unwrap() >= 0);
        assert!(!Control::stats(&pool, 24).await.unwrap().is_empty());
        assert!(!Control::recent_errors(&pool, 24, 5)
            .await
            .unwrap()
            .is_empty());
        assert!(
            Control::health(&pool, 24)
                .await
                .unwrap()
                .as_array()
                .unwrap()
                .len()
                >= 1
        );
        Control::record_usage(&pool, Some(p1), &ns, "recall", "m", 10, 5, 0.0123)
            .await
            .unwrap();
        let us = Control::usage_summary(&pool, 30, Some(&ns)).await.unwrap();
        assert_eq!(us["total_tokens"], 15);
        assert!(us["by_op"]["recall"]["usd"].as_f64().unwrap() > 0.012);
        assert_eq!(
            Control::budget_status(&pool, Some(100.0), None)
                .await
                .unwrap()["exceeded"],
            false
        );
        assert!(!Control::usage_rollup(&pool, Some(24))
            .await
            .unwrap()
            .is_empty());
        Control::record_eval(&pool, "e", &format!("m{sfx}"), 0.9, Some(3), None)
            .await
            .unwrap();
        assert_eq!(
            Control::eval_trend(&pool, "e", &format!("m{sfx}"), 5)
                .await
                .unwrap()
                .len(),
            1
        );
        Control::record_feedback(&pool, Some(p1), &ns, "q", true, None)
            .await
            .unwrap();

        // ---- constraints, overrides, corpus
        assert!(
            Control::add_constraint_enforcement(&pool, &ns, "r", "block", "", None)
                .await
                .is_err()
        );
        let cid = Control::add_constraint_enforcement(
            &pool,
            &ns,
            "no force push",
            "block",
            "Bash(git push*)",
            Some(p1),
        )
        .await
        .unwrap();
        assert!(
            Control::list_constraint_enforcement_fanout(&pool, &format!("{ns}:other"))
                .await
                .unwrap()
                .iter()
                .any(|r| r["id"] == cid)
        );
        assert!(Control::remove_constraint_enforcement(&pool, cid)
            .await
            .unwrap());
        assert!(Control::list_constraint_enforcement(&pool, Some(&ns), true)
            .await
            .unwrap()
            .is_empty());
        assert!(Control::add_constraint_override(&pool, "x:y", "z", None)
            .await
            .is_err());
        let oid = Control::add_constraint_override(&pool, &child, &ns, Some(p1))
            .await
            .unwrap();
        assert_eq!(
            oid,
            Control::add_constraint_override(&pool, &child, &ns, Some(p1))
                .await
                .unwrap()
        );
        assert!(Control::get_constraint_overrides(&pool, &[ns.clone()])
            .await
            .unwrap()
            .contains(&(child.clone(), ns.clone())));
        assert_eq!(
            Control::list_constraint_overrides(&pool, Some(&ns))
                .await
                .unwrap()
                .len(),
            1
        );
        assert!(Control::remove_constraint_override(&pool, oid)
            .await
            .unwrap());
        Control::corpus_record(&pool, &child, "arch.md", Some("lld"), Some("abc"), 3)
            .await
            .unwrap();
        assert_eq!(Control::corpus_list(&pool, &child).await.unwrap().len(), 1);
        assert_eq!(
            Control::corpus_descendants(&pool, &ns).await.unwrap().len(),
            1
        );
        Control::corpus_deviation_record(&pool, &child, 1, "legacy", "bob", Some("2.0"), Some(p1))
            .await
            .unwrap();

        // ---- tenant-schema paths (only when the data tables exist)
        let has_tenant: bool = conn(&pool).await.unwrap()
            .query_one("SELECT to_regclass('tenant_memnos.raw_turns') IS NOT NULL AND to_regclass('tenant_memnos.semantic') IS NOT NULL AND to_regclass('tenant_memnos.entities') IS NOT NULL", &[])
            .await.unwrap().get(0);
        if has_tenant {
            let sub = Control::subscribe(&pool, p1, &ns, None).await.unwrap();
            conn(&pool).await.unwrap()
                .execute("INSERT INTO tenant_memnos.raw_turns(namespace, speaker, text) VALUES ($1,'u','hello')", params![ns])
                .await.unwrap();
            let f = Control::feed(&pool, p1, sub["subscription_id"].as_i64().unwrap(), &ns, 10)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(f["items"].as_array().unwrap().len(), 1);
            assert_eq!(
                Control::list_subscriptions(&pool, p1).await.unwrap().len(),
                1
            );
            assert!(
                Control::unsubscribe(&pool, p1, sub["subscription_id"].as_i64().unwrap())
                    .await
                    .unwrap()
            );
            assert!(!Control::list_namespaces(&pool).await.unwrap().is_empty());
            assert!(!Control::memory_feed(&pool, 10, 0, Some(&ns), None)
                .await
                .unwrap()
                .is_empty());
            assert!(Control::writable_namespaces(&pool, p1, 10)
                .await
                .unwrap()
                .contains(&ns));
            Control::suggest_namespace(
                &pool,
                p1,
                &ns,
                &["Payments".into(), "Ledger".into()],
                2,
                0.6,
                12,
            )
            .await
            .unwrap();
        }
        Control::delete_namespace(&pool, &ns, has_tenant)
            .await
            .unwrap();
    }
}
