//! Hybrid pgvector `BrainStore` (`tenant_memnos` schema): bi-temporal semantic facts, verbatim
//! raw turns, episodes, an entity graph, pinned constraints, RRF recall and reconcile governance.
//!
//! Schema parity with the Python engine (`core/schema.sql`): tables `raw_turns`, `episodic`,
//! `semantic`, `entities`, `mentions`, `edges`, `provenance`, `entity_dossiers`. The embedding
//! column type (`vector` or `halfvec`) and dimension are probed from an existing database so a
//! production DB is never altered; only brand-new databases pick them from the installed pgvector
//! and the embedder.
//!
//! Row reads go through `to_jsonb` so no custom `FromSql` is needed, and vectors cross the wire as
//! text (`$n::text::vector`), so no pgvector binary codec is needed either.

use crate::code_anchors;
use crate::embed::{canonical_embed_family, vlit, Embedder};
use crate::extract::{extract_developer_signals, ExtractedFact, Extractor};
use crate::predicates::supersedable;
use crate::redact::redact;
use crate::temporal;
use chrono::{DateTime, Utc};
use deadpool_postgres::Pool;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tokio_postgres::types::ToSql;
use tokio_postgres::{Client, GenericClient};

type Prm<'a> = &'a [&'a (dyn ToSql + Sync)];

const EXPLAIN_CAP: usize = 200;
const RRF_K: f64 = 60.0;
const CONTRA_PENALTY_MAX: f64 = 40.0;
const CONTRA_RATIO_SATURATION: f64 = 0.10;

static IDENT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[a-zA-Z_][a-zA-Z0-9_]*$").unwrap());

/// Check a SQL identifier before it is formatted into a statement.
pub fn check_ident(s: &str) -> Result<(), String> {
    if IDENT_RE.is_match(s) {
        Ok(())
    } else {
        Err(format!("unsafe schema identifier: {:?}", s))
    }
}

fn de(e: tokio_postgres::Error) -> String {
    match e.as_db_error() {
        Some(d) => format!("{}: {}", d.code().code(), d.message()),
        None => e.to_string(),
    }
}

/// Run `sql` (SELECT, or DML with RETURNING) and collect every row as one JSON object.
async fn jq_g<C: GenericClient>(c: &C, sql: &str, p: Prm<'_>) -> Result<Vec<Value>, String> {
    let wrapped = format!(
        "WITH w AS ({}) SELECT COALESCE(jsonb_agg(to_jsonb(w)), '[]'::jsonb) AS j FROM w",
        sql
    );
    let row = c.query_one(wrapped.as_str(), p).await.map_err(de)?;
    let v: Value = row.try_get("j").map_err(|e| e.to_string())?;
    Ok(v.as_array().cloned().unwrap_or_default())
}

/// `jq_g` on a plain client (a concrete type, so a pooled connection coerces to it).
async fn jq(c: &Client, sql: &str, p: Prm<'_>) -> Result<Vec<Value>, String> {
    jq_g(c, sql, p).await
}

fn s_of<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}
fn i_of(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(0)
}
fn f_of(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}
fn clean_text(s: &str) -> String {
    s.replace('\0', "")
}
fn parse_ts(v: &Value, k: &str) -> Option<DateTime<Utc>> {
    v.get(k)
        .and_then(|x| x.as_str())
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.with_timezone(&Utc))
}
fn utc_date_of(v: &Value, k: &str) -> Option<String> {
    if let Some(dt) = parse_ts(v, k) {
        return Some(dt.format("%Y-%m-%d").to_string());
    }
    let s = s_of(v, k);
    if s.len() >= 10 {
        Some(s[..10].to_string())
    } else {
        None
    }
}
fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

// ---------------------------------------------------------------------------------------------
// Schema DDL (mirrors core/schema.sql; additive, idempotent; index names unchanged on purpose)
// ---------------------------------------------------------------------------------------------

const DDL: &str = r#"
CREATE SCHEMA IF NOT EXISTS @S@;
CREATE TABLE IF NOT EXISTS @S@.raw_turns(
  id bigserial PRIMARY KEY, namespace text NOT NULL, session_id text, speaker text,
  text text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), embedding @COL@,
  fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED);
CREATE TABLE IF NOT EXISTS @S@.episodic(
  id bigserial PRIMARY KEY, namespace text NOT NULL, session_id text, text text NOT NULL,
  summary text, t_start timestamptz, t_end timestamptz, observed_at timestamptz NOT NULL DEFAULT now(),
  salience real NOT NULL DEFAULT 0, last_access timestamptz, access_count int NOT NULL DEFAULT 0,
  consolidated boolean NOT NULL DEFAULT false, source_turn_ids bigint[], embedding @COL@,
  fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED);
CREATE TABLE IF NOT EXISTS @S@.semantic(
  id bigserial PRIMARY KEY, namespace text NOT NULL, kind text NOT NULL, statement text NOT NULL,
  subject_entity text, predicate text, object text, valid_from timestamptz, valid_to timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), expired_at timestamptz,
  observed_at timestamptz NOT NULL DEFAULT now(), confidence real DEFAULT 1.0,
  salience real NOT NULL DEFAULT 0, embedding @COL@,
  fts tsvector GENERATED ALWAYS AS (to_tsvector('english', statement)) STORED);
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS source_turn_ids bigint[];
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS author_principal text;
ALTER TABLE @S@.episodic ADD COLUMN IF NOT EXISTS author_principal text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS author_principal text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS superseded_by bigint;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS restatements int NOT NULL DEFAULT 0;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS source_speaker text;
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS memory_type text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS memory_type text;
ALTER TABLE @S@.episodic ADD COLUMN IF NOT EXISTS memory_type text;
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS constraint_subject text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS constraint_subject text;
ALTER TABLE @S@.episodic ADD COLUMN IF NOT EXISTS constraint_subject text;
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz;
ALTER TABLE @S@.episodic ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz;
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS constraint_retired_by text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS constraint_retired_by text;
ALTER TABLE @S@.episodic ADD COLUMN IF NOT EXISTS constraint_retired_by text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS constraint_since text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS constraint_until text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS inference_confidence text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS inference_basis text;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS source_fact_ids bigint[];
ALTER TABLE @S@.raw_turns ADD COLUMN IF NOT EXISTS code_anchors jsonb;
ALTER TABLE @S@.semantic ADD COLUMN IF NOT EXISTS code_anchors jsonb;
CREATE TABLE IF NOT EXISTS @S@.entities(
  id bigserial PRIMARY KEY, namespace text NOT NULL, name text NOT NULL, embedding @COL@,
  UNIQUE(namespace, name));
CREATE TABLE IF NOT EXISTS @S@.mentions(
  entity_id bigint NOT NULL, memory_id bigint NOT NULL, memory_kind text NOT NULL,
  UNIQUE(entity_id, memory_id, memory_kind));
CREATE TABLE IF NOT EXISTS @S@.edges(
  id bigserial PRIMARY KEY, namespace text NOT NULL, src_entity bigint NOT NULL,
  dst_entity bigint NOT NULL, weight real DEFAULT 1.0, UNIQUE(namespace, src_entity, dst_entity));
CREATE TABLE IF NOT EXISTS @S@.provenance(
  semantic_id bigint NOT NULL, episodic_id bigint NOT NULL, UNIQUE(semantic_id, episodic_id));
CREATE INDEX IF NOT EXISTS raw_hnsw ON @S@.raw_turns USING hnsw (embedding @OPS@);
CREATE INDEX IF NOT EXISTS raw_fts ON @S@.raw_turns USING gin (fts);
CREATE INDEX IF NOT EXISTS epi_hnsw ON @S@.episodic USING hnsw (embedding @OPS@);
CREATE INDEX IF NOT EXISTS epi_fts ON @S@.episodic USING gin (fts);
CREATE INDEX IF NOT EXISTS epi_time ON @S@.episodic (observed_at);
CREATE INDEX IF NOT EXISTS sem_hnsw ON @S@.semantic USING hnsw (embedding @OPS@);
CREATE INDEX IF NOT EXISTS sem_fts ON @S@.semantic USING gin (fts);
CREATE INDEX IF NOT EXISTS sem_valid ON @S@.semantic (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS sem_live ON @S@.semantic (valid_from) WHERE expired_at IS NULL AND valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ent_hnsw ON @S@.entities USING hnsw (embedding @OPS@);
CREATE INDEX IF NOT EXISTS men_ent ON @S@.mentions (entity_id);
CREATE INDEX IF NOT EXISTS men_mem ON @S@.mentions (memory_id);
CREATE INDEX IF NOT EXISTS prov_sem ON @S@.provenance (semantic_id);
CREATE INDEX IF NOT EXISTS raw_ns ON @S@.raw_turns (namespace);
CREATE INDEX IF NOT EXISTS epi_ns ON @S@.episodic (namespace);
CREATE INDEX IF NOT EXISTS sem_ns ON @S@.semantic (namespace);
CREATE INDEX IF NOT EXISTS sem_supersede_pred ON @S@.semantic (namespace, lower(subject_entity), lower(predicate)) WHERE valid_to IS NULL AND expired_at IS NULL;
CREATE INDEX IF NOT EXISTS sem_supersede_subj ON @S@.semantic (namespace, subject_entity) WHERE valid_to IS NULL AND expired_at IS NULL;
CREATE INDEX IF NOT EXISTS raw_mtype ON @S@.raw_turns (namespace, memory_type) WHERE memory_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS sem_mtype ON @S@.semantic (namespace, memory_type) WHERE memory_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS epi_mtype ON @S@.episodic (namespace, memory_type) WHERE memory_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS raw_cons_subj ON @S@.raw_turns (namespace, constraint_subject) WHERE memory_type='constraint' AND constraint_retired_at IS NULL;
CREATE INDEX IF NOT EXISTS sem_cons_subj ON @S@.semantic (namespace, constraint_subject) WHERE memory_type='constraint' AND constraint_retired_at IS NULL;
CREATE INDEX IF NOT EXISTS epi_cons_subj ON @S@.episodic (namespace, constraint_subject) WHERE memory_type='constraint' AND constraint_retired_at IS NULL;
CREATE INDEX IF NOT EXISTS sem_src_turns ON @S@.semantic USING gin (source_turn_ids);
CREATE TABLE IF NOT EXISTS @S@.entity_dossiers(
  id bigserial PRIMARY KEY, entity_id bigint NOT NULL, namespace text NOT NULL,
  dossier_text text NOT NULL, generated_at timestamptz NOT NULL DEFAULT now(), model_used text);
CREATE UNIQUE INDEX IF NOT EXISTS entity_dossiers_entity_ns ON @S@.entity_dossiers(entity_id, namespace);
"#;

// ---------------------------------------------------------------------------------------------
// Pure helpers (unit-tested)
// ---------------------------------------------------------------------------------------------

/// Reciprocal Rank Fusion over ranked key lists. Ties break by first appearance, so output order
/// is deterministic.
pub fn rrf_fuse(lists: &[Vec<String>], k: f64) -> Vec<(String, f64)> {
    let mut acc: HashMap<&str, (f64, usize)> = HashMap::new();
    let mut seen = 0usize;
    for l in lists {
        for (i, key) in l.iter().enumerate() {
            let e = acc.entry(key.as_str()).or_insert_with(|| {
                seen += 1;
                (0.0, seen)
            });
            e.0 += 1.0 / (k + (i + 1) as f64);
        }
    }
    let mut out: Vec<(&str, (f64, usize))> = acc.into_iter().collect();
    out.sort_by(|a, b| b.1 .0.partial_cmp(&a.1 .0).unwrap().then(a.1 .1.cmp(&b.1 .1)));
    out.into_iter().map(|(k, (s, _))| (k.to_string(), s)).collect()
}

/// `auto:<slug>-<sha1[:10]>`: default `constraint_subject` for a constraint written without one.
pub fn derive_constraint_subject(text: &str) -> String {
    static WORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"[a-z0-9]+").unwrap());
    let norm = text.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ");
    let words: Vec<&str> = WORD.find_iter(&norm).take(6).map(|m| m.as_str()).collect();
    let mut slug: String = words.join("-").chars().take(60).collect();
    slug = slug.trim_matches('-').to_string();
    if slug.is_empty() {
        slug = "constraint".into();
    }
    let digest = hex::encode(Sha1::digest(norm.as_bytes()));
    format!("auto:{}-{}", slug, &digest[..10])
}

pub fn effective_constraint_subject(
    memory_type: Option<&str>,
    subject: Option<&str>,
    text: &str,
) -> Option<String> {
    if memory_type != Some("constraint") {
        return None;
    }
    let cs = subject.unwrap_or("").trim().to_lowercase();
    Some(if cs.is_empty() { derive_constraint_subject(text) } else { cs })
}

/// `parent` is a `:`-prefix ancestor of `child` (`org:acme` of `org:acme:widgets`).
pub fn is_ancestor_ns(parent: &str, child: &str) -> bool {
    child.len() > parent.len() && child.starts_with(parent) && child.as_bytes()[parent.len()] == b':'
}

/// Closer descendant namespace beats ancestor on the same `constraint_subject`, unless an
/// explicit `(child, parent)` override reverses it. A precedence cycle fails open. Rows need
/// `constraint_subject`, `namespace`, `id`, `kind`. Returns `(winners, losers)`.
pub fn resolve_constraint_precedence(
    rows: Vec<Value>,
    overrides: &HashSet<(String, String)>,
) -> (Vec<Value>, Vec<Value>) {
    let mut winners = Vec::new();
    let mut order: Vec<String> = Vec::new();
    let mut by_subject: HashMap<String, Vec<Value>> = HashMap::new();
    for r in rows {
        let subj = s_of(&r, "constraint_subject").to_string();
        if subj.is_empty() {
            winners.push(r);
            continue;
        }
        if !by_subject.contains_key(&subj) {
            order.push(subj.clone());
        }
        by_subject.entry(subj).or_default().push(r);
    }
    let mut losers = Vec::new();
    for subj in order {
        let group = by_subject.remove(&subj).unwrap_or_default();
        let mut ns_order: Vec<String> = Vec::new();
        let mut ns_rows: HashMap<String, Vec<Value>> = HashMap::new();
        for r in group.iter() {
            let n = s_of(r, "namespace").to_string();
            if !ns_rows.contains_key(&n) {
                ns_order.push(n.clone());
            }
            ns_rows.entry(n).or_default().push(r.clone());
        }
        if ns_order.len() <= 1 {
            winners.extend(group);
            continue;
        }
        // loser ns -> (beater namespace, beater row)
        let mut beaten_by: HashMap<String, (String, Value)> = HashMap::new();
        for (i, a) in ns_order.iter().enumerate() {
            for b in ns_order.iter().skip(i + 1) {
                let (parent, child) = if is_ancestor_ns(a, b) {
                    (a, b)
                } else if is_ancestor_ns(b, a) {
                    (b, a)
                } else {
                    continue;
                };
                let (win, lose) = if overrides.contains(&(child.clone(), parent.clone())) {
                    (child, parent)
                } else {
                    (parent, child)
                };
                let beater = ns_rows[win][0].clone();
                let better = match beaten_by.get(lose) {
                    None => true,
                    Some((pn, _)) => (win.len(), win.as_str()) > (pn.len(), pn.as_str()),
                };
                if better {
                    beaten_by.insert(lose.clone(), (win.clone(), beater));
                }
            }
        }
        if beaten_by.len() >= ns_order.len() {
            winners.extend(group); // cycle: fail open
            continue;
        }
        for n in ns_order.iter() {
            let rlist = ns_rows.remove(n).unwrap_or_default();
            match beaten_by.get(n) {
                None => winners.extend(rlist),
                Some((wn, wrow)) => {
                    for row in rlist {
                        losers.push(json!({
                            "subject": subj, "namespace": n, "id": row["id"], "kind": row["kind"],
                            "winner_namespace": wn, "winner_id": wrow["id"], "winner_kind": wrow["kind"],
                        }));
                    }
                }
            }
        }
    }
    (winners, losers)
}

/// Split a unified diff into per-hunk `(added_text, removed_text)`. A new hunk starts at each
/// genuine `--- `/`+++ ` header pair and at each `@@` line; a lone `--- foo` content line is real
/// content, not a header.
pub fn split_diff_hunks(diff: &str) -> Vec<(String, String)> {
    let lines: Vec<&str> = diff.lines().collect();
    let mut hunks: Vec<(Vec<&str>, Vec<&str>)> = vec![(vec![], vec![])];
    let (mut i, n) = (0usize, lines.len());
    while i < n {
        let line = lines[i];
        let is_old = line.starts_with("--- ") || line == "---";
        if is_old && i + 1 < n && (lines[i + 1].starts_with("+++ ") || lines[i + 1] == "+++") {
            hunks.push((vec![], vec![]));
            i += 2;
            continue;
        }
        if line.starts_with("@@") {
            hunks.push((vec![], vec![]));
            i += 1;
            continue;
        }
        let cur = hunks.last_mut().unwrap();
        if let Some(rest) = line.strip_prefix('+') {
            cur.0.push(rest);
        } else if let Some(rest) = line.strip_prefix('-') {
            cur.1.push(rest);
        }
        i += 1;
    }
    hunks.into_iter().map(|(a, r)| (a.join("\n"), r.join("\n"))).collect()
}

static QUOTED_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r#""[^"]{2,}"|“[^”]{2,}”"#).unwrap());
static VERBATIM_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(exactly|verbatim|quote[ds]?|quoting|word[ -]for[ -]word|literally|precisely|say|says|said|saying|tell|tells|told|mention|mentions|mentioned|wrote|write|phrase[d]?|wording|describe[d]?|express(ed)?)\b").unwrap()
});
static BROAD_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(where (are|do) we|where (do|does) (things|it|this|that) stand|where do we stand|status|state of|progress|update[sd]? on|latest on|the latest|overview|catch (me|us) up|summar(y|ies|ize[d]?|ise[d]?)|recap|big picture|how (is|are) .{0,40}(going|coming along|shaping up|doing)|so far|current(ly)? (state|situation)|bring me up to (speed|date)|fill me in|what.{0,12}(going on|happening) with)\b").unwrap()
});
static LIST_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(how many|how much|which|what (?:projects?|agents?|services?|systems?|tools?|tickets?|tasks?|items?|repos?|repositories|hosts?|servers?|clients?|vendors?|people|teams?|things?|files?|components?|modules?|features?|pets?|hobbies|languages?|frameworks?|members?|roles?|responsibilities|kinds?|types?|activities|countries|places|cities)\b|\b(list|enumerate|all of the|all the)\b|\bwhat are\b|\bwho (?:all |are )\b)").unwrap()
});
static ERROR_Q_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(error|exception|traceback|stack ?trace|fail(?:s|ed|ing|ure)?|crash(?:es|ed)?|bug|debug|broken|fix|segfault|panic|timeout|500|404)\b").unwrap()
});
static PROPER_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b[A-Z][a-zA-Z]{2,}\b").unwrap());
const Q_CAPS: &[&str] = &[
    "What", "Whats", "Where", "When", "Who", "Whom", "Whose", "Why", "How", "Which", "Give",
    "Tell", "Can", "Could", "Would", "Should", "Did", "Does", "The", "Are", "Is", "Was", "Were",
    "Has", "Have", "Any", "Please",
];

/// `verbatim | list | broad | neutral`: deterministic, no LLM. Drives arm order only.
pub fn query_intent(query: &str) -> &'static str {
    let ents = PROPER_RE.find_iter(query).filter(|m| !Q_CAPS.contains(&m.as_str())).count();
    if QUOTED_RE.is_match(query) || VERBATIM_RE.is_match(query) {
        return "verbatim";
    }
    if LIST_RE.is_match(query) {
        return "list";
    }
    if BROAD_RE.is_match(query) && query.split_whitespace().count() <= 20 && ents <= 1 {
        return "broad";
    }
    "neutral"
}

pub fn is_error_query(q: &str) -> bool {
    ERROR_Q_RE.is_match(q)
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// Render recall rows into a paste-ready block. Pinned constraints always render in full and never
/// count against `max_chars`; ranked rows that don't fit are skipped, not truncated. A line is
/// tagged `by <author>` only when the author differs from `viewer`.
pub fn render_context(rows: &[Value], max_chars: usize, viewer: Option<&str>, query: Option<&str>) -> String {
    let mut ordered: Vec<&Value> = rows.iter().collect();
    if let Some(q) = query {
        if matches!(query_intent(q), "list" | "broad") {
            let pinned = |r: &&Value| r.get("pinned").and_then(|x| x.as_bool()).unwrap_or(false);
            let mut head: Vec<&Value> = ordered.iter().copied().filter(|r| pinned(r)).collect();
            let rest: Vec<&Value> = ordered.iter().copied().filter(|r| !pinned(r)).collect();
            head.extend(rest.iter().copied().filter(|r| s_of(r, "kind") == "fact"));
            head.extend(rest.iter().copied().filter(|r| s_of(r, "kind") != "fact"));
            ordered = head;
        }
    }
    let (mut out, mut used) = (Vec::new(), 0usize);
    for r in ordered {
        let tag = match s_of(r, "namespace") {
            "" => String::new(),
            n => format!(" [{}]", n),
        };
        let content = s_of(r, "content");
        if r.get("pinned").and_then(|x| x.as_bool()).unwrap_or(false) {
            out.push(format!("CONSTRAINT:{} {}", tag, content));
            continue;
        }
        let author = s_of(r, "author");
        let by = match viewer {
            Some(v) if !author.is_empty() && author != v => format!(", by {}", author),
            _ => String::new(),
        };
        let kind = s_of(r, "kind");
        let mut label = match s_of(r, "type") {
            "" => (if kind == "fact" { "fact" } else { "said" }).to_string(),
            t => t.to_string(),
        };
        if label == "inferred" && !s_of(r, "confidence").is_empty() {
            label += &format!(", confidence={}", s_of(r, "confidence"));
        }
        if r.get("superseded").and_then(|x| x.as_bool()).unwrap_or(false) {
            label += &match s_of(r, "superseded_at") {
                "" => ", superseded".to_string(),
                d => format!(", superseded as of {}", d),
            };
        }
        if let Some(n) = r.get("dup_count").and_then(|x| x.as_i64()).filter(|n| *n > 0) {
            label += &format!(", x{}", n);
        }
        let date = if kind == "fact" && !s_of(r, "date").is_empty() {
            format!(", {}", s_of(r, "date"))
        } else {
            String::new()
        };
        let drift = match s_of(r, "code_drift_notice") {
            "" => String::new(),
            d => format!(", {}", d),
        };
        let body = if by.is_empty() {
            content.to_string()
        } else {
            format!(
                "<memnos:peer_memory author=\"{}\" trust=\"untrusted\">{}</memnos:peer_memory>",
                html_escape(author).replace('"', "&quot;"),
                html_escape(content)
            )
        };
        let line = format!("- ({}{}{}{}){} {}", label, date, by, drift, tag, body);
        if used + line.len() > max_chars {
            continue;
        }
        used += line.len();
        out.push(line);
    }
    out.join("\n")
}

static DATE_RE: Lazy<Regex> = Lazy::new(|| {
    let mon = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)";
    Regex::new(&format!(
        r"(?i)\b(\d{{4}}-\d{{2}}-\d{{2}}|{m}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{m}\.?\s+\d{{4}}|{m}\.?\s+\d{{4}})\b",
        m = mon
    ))
    .unwrap()
});
static HISTORICAL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(lived|resided|worked|used to|had been|formerly|previously|originally|back in|at the time|grew up)\b").unwrap()
});
static PAST_COPULA_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)\b(was|were)\b").unwrap());
static PAST_YEAR_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(?:in|since|during|until|till|before|after|from|by|circa|around|throughout)\s+(?:(?:early|late|mid)[- ])?(?:19|20)\d\d\b").unwrap()
});
static PAST_TIME_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(ago|back then|in the past|at one point|at that time|in those days|last (?:year|decade|century)|as an? (?:child|kid|teen|teenager|student|youngster)|when (?:i|he|she|we|they|you) (?:was|were) (?:young|little|a kid|a child|younger|growing up)|in (?:my|his|her|their|our) (?:youth|childhood|twenties|teens))\b").unwrap()
});
static CHANGE_OF_STATE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(?:was|were|has been|have been|is now|are now|got)\s+(?:changed|updated|raised|increased|lowered|reduced|set|switched|moved|renamed|bumped|upgraded|downgraded|adjusted|revised)\b").unwrap()
});
static REVERSAL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(no longer|not anymore|declined|rejected|not recommended|instead|switched to|switching to|resolved|unblocked|cancelled|canceled|called off|reverted|rolled back)\b").unwrap()
});
static TOKEN_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[a-z0-9][a-z0-9_-]{3,}").unwrap());
const TOKEN_STOP: &[&str] = &[
    "this", "that", "with", "from", "have", "been", "were", "will", "longer", "anymore", "after",
    "before", "into", "over", "under", "they", "them", "their", "there", "when", "what", "which",
    "while", "would", "could", "should", "about", "because", "very", "more", "most", "some",
    "such", "than", "then", "only", "also", "just", "like", "being", "does", "doing", "still",
    "instead", "switched", "switching", "resolved", "unblocked", "cancelled", "canceled",
    "called", "reverted", "rolled", "declined", "rejected", "recommended",
];

fn salient_tokens(text: &str) -> HashSet<String> {
    TOKEN_RE
        .find_iter(&text.to_lowercase())
        .map(|m| m.as_str().to_string())
        .filter(|t| !TOKEN_STOP.contains(&t.as_str()))
        .collect()
}

/// A statement describing a PAST state, which must not displace the current value unless it
/// carries an explicit newer event date. A change-of-state phrase ("was changed to") is never
/// historical.
pub fn is_historical(stmt: &str) -> bool {
    if CHANGE_OF_STATE_RE.is_match(stmt) {
        return false;
    }
    if HISTORICAL_RE.is_match(stmt) {
        return true;
    }
    PAST_COPULA_RE.is_match(stmt)
        && (PAST_TIME_RE.is_match(stmt) || DATE_RE.is_match(stmt) || PAST_YEAR_RE.is_match(stmt))
}

/// Reversal/negation close-out guard: candidates (`dist`, `subject_entity`, `statement`, `id`)
/// the reversal statement closes. Needs distance <= `thresh`, subject agreement, and at least two
/// shared salient tokens of which one is not part of a subject name.
fn negation_targets(stmt: &str, subj: Option<&str>, cands: &[Value], thresh: f64) -> Vec<i64> {
    let new_toks = salient_tokens(stmt);
    let subj_toks = salient_tokens(subj.unwrap_or(""));
    let mut out = Vec::new();
    for c in cands {
        let Some(dist) = c.get("dist").and_then(|d| d.as_f64()) else { continue };
        if dist > thresh {
            continue;
        }
        let cs = s_of(c, "subject_entity");
        if let Some(s) = subj {
            if !cs.is_empty() && cs.to_lowercase() != s.to_lowercase() {
                continue;
            }
        }
        let mut cand_subj = subj_toks.clone();
        cand_subj.extend(salient_tokens(cs));
        let overlap: HashSet<String> =
            new_toks.intersection(&salient_tokens(s_of(c, "statement"))).cloned().collect();
        if overlap.len() < 2 || overlap.difference(&cand_subj).next().is_none() {
            continue;
        }
        out.push(i_of(c, "id"));
    }
    out
}

const LITERAL_STOPWORDS: &[&str] = &[
    "true", "false", "null", "none", "nil", "nan", "undefined", "unknown", "yes", "no", "ok",
    "okay", "n/a", "na", "tbd", "wip", "todo", "done", "pass", "passed", "fail", "failed", "green",
    "red", "active", "inactive", "enabled", "disabled", "open", "closed", "higher", "lower",
    "same", "other", "all", "any", "both", "either", "neither", "today", "yesterday", "tomorrow",
    "now", "later", "soon", "always", "never", "sometimes", "often",
];
static NUMERIC_OR_DATE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^([$€£¥₹]?\s*[+-]?\d[\d,._/%*\s+-]*(?:[kmb]|ms|s|m|h|d|gb|mb|kb|tb|%|x)?|(?:\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,4})?)|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?)|(?:at\s+\d{1,2}:\d{2}.*)|(?::\d{2,5}))$").unwrap()
});
static CLAUSE_START_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(?:not\s+|no\s+longer\s+|because\s+|since\s+|after\s+|before\s+|when\s+|while\s+|where\s+|that\s+|which\s+|who\s+|how\s+|whether\s+|if\s+|is\s+|are\s+|was\s+|were\s+|has\s+|have\s+|had\s+|will\s+|would\s+|can\s+|could\s+|should\s+|must\s+|waiting\s+|running\s+|using\s+|fixing\s+|testing\s+)").unwrap()
});
static PREP_START_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)^(?:at|in|on|by|for|with|from|to|into)\s+").unwrap());
static CORP_SUFFIX_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(?:Inc|Ltd|LLC|Corp|Co|Jr|Sr|Dr|Mr|Mrs|Ms|St)\.$").unwrap()
});
static COMMA_CORP_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)^[^,]+\s*,\s*(?:Inc|Ltd|LLC|Corp|Co|Jr|Sr)\.?$").unwrap());
static CURRENCY_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[$€£¥₹]\s*\d").unwrap());
static NAME_OK_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[A-Za-z0-9_.-]{2,}$").unwrap());
const CLAUSE_INTERIOR: &[&str] =
    &["the", "a", "an", "this", "that", "these", "those", "my", "our", "your", "their", "his", "her", "its"];

/// A legitimate named entity/concept node, as opposed to a literal value or a free-text clause
/// that belongs on `semantic.object` and must not pollute `entities`/`edges`.
pub fn is_valid_entity_name(name: &str, is_object: bool) -> bool {
    let s = name.split_whitespace().collect::<Vec<_>>().join(" ");
    if s.is_empty() || s.chars().count() > if is_object { 50 } else { 60 } {
        return false;
    }
    if s.chars().filter(|c| c.is_ascii_alphabetic()).count() < 2 && !NAME_OK_RE.is_match(&s) {
        return false;
    }
    if LITERAL_STOPWORDS.contains(&s.to_lowercase().as_str()) || NUMERIC_OR_DATE_RE.is_match(&s) {
        return false;
    }
    if s.chars().any(|c| ";\n\r?!=<>{}[]".contains(c)) {
        return false;
    }
    if s.ends_with('.') && !CORP_SUFFIX_RE.is_match(&s) {
        return false;
    }
    let words: Vec<&str> = s.split(' ').collect();
    if words.len() > if is_object { 5 } else { 6 } {
        return false;
    }
    if s.contains(',') && !COMMA_CORP_RE.is_match(&s) {
        return false;
    }
    if CURRENCY_RE.is_match(&s) || CLAUSE_START_RE.is_match(&s) {
        return false;
    }
    if PREP_START_RE.is_match(&s) {
        let title = s.chars().next().map_or(false, |c| c.is_uppercase())
            && words.iter().skip(1).all(|w| w.chars().next().map_or(true, |c| !c.is_alphabetic() || c.is_uppercase()))
            && !words.iter().skip(1).any(|w| CLAUSE_INTERIOR.contains(&w.to_lowercase().as_str()));
        if !title {
            return false;
        }
    }
    true
}

pub fn canonicalize_entity_name(name: &str, is_object: bool) -> String {
    let s = name.split_whitespace().collect::<Vec<_>>().join(" ");
    let low = s.to_lowercase();
    if !is_object && matches!(low.as_str(), "user" | "the user" | "me" | "i" | "myself") {
        return "user".into();
    }
    match low.as_str() {
        "user" | "the user" => "user".into(),
        "assistant" | "the assistant" => "assistant".into(),
        _ => s,
    }
}

fn contradiction_penalty(ratio: f64) -> i64 {
    if ratio <= 0.0 {
        return 0;
    }
    (CONTRA_PENALTY_MAX * (ratio / CONTRA_RATIO_SATURATION).min(1.0).sqrt()).round() as i64
}

/// Dotted-numeric version, zero-padded to 6 components so `2.1` equals `2.1.0`.
pub fn parse_semver_key(v: &str) -> Result<[u64; 6], String> {
    let s = v.trim();
    let bad = || format!("invalid version string: {:?} (expected dotted numeric, e.g. '2.1.0')", v);
    if s.is_empty() || !s.split('.').all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit())) {
        return Err(bad());
    }
    let parts: Vec<u64> = s.split('.').map(|p| p.parse().map_err(|_| bad())).collect::<Result<_, _>>()?;
    if parts.len() > 6 {
        return Err(format!("version string has too many components: {:?}", v));
    }
    let mut k = [0u64; 6];
    k[..parts.len()].copy_from_slice(&parts);
    Ok(k)
}

/// Replace every non-alphanumeric character with a space and cap the word count, so a pathological
/// query (hyphen runs, operators, thousands of tokens) can never overflow Postgres' tsquery parser.
fn fts_clamp(q: &str) -> String {
    let max = std::env::var("MEMNOS_FTS_MAX_TOKENS").ok().and_then(|v| v.parse().ok()).unwrap_or(64usize);
    let flat: String = q.chars().map(|c| if c.is_alphanumeric() { c } else { ' ' }).collect();
    flat.split_whitespace().take(max).collect::<Vec<_>>().join(" ")
}

// ---------------------------------------------------------------------------------------------
// BrainStore
// ---------------------------------------------------------------------------------------------

/// Options for the recall family.
#[derive(Clone, Debug)]
pub struct RecallOpts {
    pub k: usize,
    pub raw_quota: usize,
    pub fact_quota: usize,
    pub max_chars: usize,
    /// Ancestor namespaces whose pinned constraints are injected (nearest first).
    pub ancestors: Vec<String>,
    /// Ancestor namespaces skipped due to missing read grant.
    pub inherited_skipped: Vec<String>,
    /// Linked knowledge namespaces consulted for grounded recall.
    pub grounded_in: Vec<String>,
    /// Linked namespaces skipped due to missing read grant.
    pub links_skipped: Vec<String>,
    /// `(child, parent)` explicit precedence-reversal edges.
    pub overrides: HashSet<(String, String)>,
    /// Hard-scope facts to one subject entity.
    pub subject: Option<String>,
    /// Filter ranked rows to a single memory_type (pinned constraints are exempt).
    pub type_filter: Option<String>,
    /// Filter ranked rows to a single writing principal.
    pub author_filter: Option<String>,
    /// Cap on injected pinned constraints (`None` = unbounded, `Some(0)` = disabled).
    pub constraint_cap: Option<usize>,
    /// Optional wall-clock deadline (`Instant`); if expired, recall returns degraded best-available results.
    pub deadline: Option<Instant>,
    /// Name of the calling principal, so only other authors are attributed in the context.
    pub viewer: Option<String>,
    /// When set, rows with code anchors are annotated with code drift notices.
    pub repo_root: Option<PathBuf>,
}

impl Default for RecallOpts {
    fn default() -> Self {
        Self {
            k: 40,
            raw_quota: 11,
            fact_quota: 8,
            max_chars: 9000,
            ancestors: vec![],
            inherited_skipped: vec![],
            grounded_in: vec![],
            links_skipped: vec![],
            overrides: HashSet::new(),
            subject: None,
            type_filter: None,
            author_filter: None,
            constraint_cap: None,
            deadline: None,
            viewer: None,
            repo_root: None,
        }
    }
}

pub struct BrainStore {
    pool: Pool,
    schema: String,
    embedder: Arc<Embedder>,
    extractor: Arc<Extractor>,
    /// `(vector type, dimension)` of the embedding columns: probed by `init_schema`.
    vcol: Mutex<(String, usize)>,
    explain: Mutex<VecDeque<Value>>,
    repo_root: Option<PathBuf>,
}

impl BrainStore {
    /// `schema` defaults to `tenant_memnos`; it is validated before it is ever formatted into SQL.
    pub fn new(
        pool: Pool,
        schema: Option<&str>,
        embedder: Arc<Embedder>,
        extractor: Arc<Extractor>,
    ) -> Result<Self, String> {
        let schema = schema.unwrap_or("tenant_memnos").to_string();
        check_ident(&schema)?;
        let dim = embedder.dim;
        Ok(Self {
            pool,
            schema,
            embedder,
            extractor,
            vcol: Mutex::new(("vector".into(), dim)),
            explain: Mutex::new(VecDeque::new()),
            repo_root: std::env::var("MEMNOS_REPO_ROOT").ok().filter(|s| !s.is_empty()).map(PathBuf::from),
        })
    }

    pub fn schema(&self) -> &str {
        &self.schema
    }

    fn vt(&self) -> String {
        self.vcol.lock().unwrap().0.clone()
    }
    fn col_dim(&self) -> usize {
        self.vcol.lock().unwrap().1
    }

    async fn conn(&self) -> Result<deadpool_postgres::Object, String> {
        self.pool.get().await.map_err(|e| format!("postgres pool: {}", e))
    }

    async fn q(&self, sql: &str, p: Prm<'_>) -> Result<Vec<Value>, String> {
        let c = self.conn().await?;
        jq(&c, sql, p).await
    }

    /// Embed passage text to the column dimension, failing loudly on any model or dimension mismatch.
    async fn embed(&self, text: &str) -> Result<Vec<f32>, String> {
        let (v, _) = self.embedder.try_embed(text).await?;
        let dim = self.col_dim();
        if v.len() != dim {
            return Err(format!(
                "embedding dimension mismatch on schema '{}': column is {}-d, embedder '{}' produced {}-d",
                self.schema,
                dim,
                self.embedder.model_name(),
                v.len()
            ));
        }
        Ok(v)
    }

    async fn embed_lit(&self, text: &str) -> Result<String, String> {
        Ok(vlit(&self.embed(text).await?))
    }

    async fn embed_query_lit(&self, text: &str) -> Result<String, String> {
        let (v, _) = self.embedder.try_embed_query(text).await?;
        let dim = self.col_dim();
        if v.len() != dim {
            return Err(format!(
                "embedding dimension mismatch on schema '{}': column is {}-d, embedder '{}' produced {}-d",
                self.schema,
                dim,
                self.embedder.model_name(),
                v.len()
            ));
        }
        Ok(vlit(&v))
    }

    // --- schema ----------------------------------------------------------------------------

    /// Returns `true` if this database already has an established embedding configuration
    /// (recorded `COMMENT ON SCHEMA` model metadata, existing embedded rows, or a 1536-d column),
    /// so upgrades on existing installations proceed automatically without prompting.
    pub async fn has_existing_embed_lineage(&self) -> Result<bool, String> {
        let c = self.conn().await?;
        let ns_probe = jq(
            &c,
            "SELECT obj_description(oid, 'pg_namespace') AS d FROM pg_namespace WHERE nspname = $1",
            &[&self.schema],
        )
        .await?;
        let Some(ns_row) = ns_probe.first() else {
            return Ok(false);
        };
        if let Some(desc) = ns_row.get("d").and_then(|v| v.as_str()) {
            if let Ok(j) = serde_json::from_str::<Value>(desc) {
                if j.get("embed_model").and_then(|m| m.as_str()).is_some() {
                    return Ok(true);
                }
            }
        }
        let col_probe = jq(
            &c,
            "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a \
             JOIN pg_class k ON k.oid=a.attrelid JOIN pg_namespace n ON n.oid=k.relnamespace \
             WHERE n.nspname=$1 AND k.relname='raw_turns' AND a.attname='embedding' AND NOT a.attisdropped",
            &[&self.schema],
        )
        .await?;
        let Some(col_row) = col_probe.first() else {
            return Ok(false);
        };
        if s_of(col_row, "t").contains("(1536)") {
            return Ok(true);
        }
        let pop = jq(
            &c,
            &format!(
                "SELECT (EXISTS(SELECT 1 FROM {s}.raw_turns WHERE embedding IS NOT NULL LIMIT 1) \
                 OR EXISTS(SELECT 1 FROM {s}.semantic WHERE embedding IS NOT NULL LIMIT 1)) AS p",
                s = self.schema
            ),
            &[],
        )
        .await?;
        Ok(pop
            .first()
            .and_then(|r| r.get("p"))
            .and_then(|b| b.as_bool())
            .unwrap_or(false))
    }

    /// Create the extension, schema, tables and indexes. Probes an existing `raw_turns.embedding`
    /// column for its type/dimension and enforces `(model, dim)` compatibility via PostgreSQL
    /// `COMMENT ON SCHEMA` catalog metadata (zero extra tables or columns).
    pub async fn init_schema(&self) -> Result<(), String> {
        let c = self.conn().await?;
        c.batch_execute(
            "BEGIN; SELECT pg_advisory_xact_lock(7819204502); CREATE EXTENSION IF NOT EXISTS vector; COMMIT;",
        )
        .await
        .map_err(de)?;
        let probe = jq(
            &c,
            "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a \
             JOIN pg_class k ON k.oid=a.attrelid JOIN pg_namespace n ON n.oid=k.relnamespace \
             WHERE n.nspname=$1 AND k.relname='raw_turns' AND a.attname='embedding' AND NOT a.attisdropped",
            &[&self.schema],
        )
        .await?;
        static COL_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(?:\w+\.)?(vector|halfvec)\((\d+)\)$").unwrap());
        let existing = match probe.first() {
            None => None,
            Some(r) => Some(
                COL_RE
                    .captures(s_of(r, "t"))
                    .map(|m| (m[1].to_string(), m[2].parse::<usize>().unwrap_or(0)))
                    .ok_or_else(|| format!("{}.raw_turns.embedding has unsupported type {:?}", self.schema, s_of(r, "t")))?,
            ),
        };
        let allow_mismatch = matches!(
            std::env::var("MEMNOS_ALLOW_EMBED_MISMATCH")
                .unwrap_or_default()
                .trim()
                .to_ascii_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on"
        );
        if let Some((ref ex_vtype, ex_dim)) = existing {
            if ex_dim != self.embedder.dim && !allow_mismatch {
                return Err(format!(
                    "embedding dimension mismatch on schema '{}': existing database column is {}({}), \
                     but active embedder '{}' produces {}-d vectors. Provide the matching embedder \
                     (e.g. unlock/set OPENAI_API_KEY for 1536-d, or unset it for 384-d local models), \
                     or set MEMNOS_ALLOW_EMBED_MISMATCH=1 to override.",
                    self.schema,
                    ex_vtype,
                    ex_dim,
                    self.embedder.model_name(),
                    self.embedder.dim
                ));
            }
        }
        let (vtype, dim) = match existing {
            Some(x) => x,
            None => {
                let ver = jq(&c, "SELECT extversion AS v FROM pg_extension WHERE extname='vector'", &[]).await?;
                let nums: Vec<u32> = ver
                    .first()
                    .map(|r| s_of(r, "v").split('.').map(|p| p.parse().unwrap_or(0)).collect())
                    .unwrap_or_default();
                let half = nums.len() >= 2 && (nums[0], nums[1]) >= (0, 7);
                ((if half { "halfvec" } else { "vector" }).to_string(), self.embedder.dim)
            }
        };
        *self.vcol.lock().unwrap() = (vtype.clone(), dim);
        let ops = if vtype == "halfvec" { "halfvec_cosine_ops" } else { "vector_cosine_ops" };
        let ddl = DDL
            .replace("@S@", &self.schema)
            .replace("@COL@", &format!("{}({})", vtype, dim))
            .replace("@OPS@", ops);
        let locked_ddl = format!(
            "BEGIN;\nSELECT pg_advisory_xact_lock(7819204503);\n{}\nCOMMIT;",
            ddl
        );
        c.batch_execute(&locked_ddl).await.map_err(de)?;

        // Check per-schema embedder model lineage stored in pg_description (`COMMENT ON SCHEMA`)
        let desc_rows = jq(
            &c,
            "SELECT obj_description(to_regnamespace($1), 'pg_namespace') AS d",
            &[&self.schema],
        )
        .await?;
        let recorded_model: Option<String> = desc_rows
            .first()
            .and_then(|r| r.get("d"))
            .and_then(|v| v.as_str())
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .and_then(|j| j.get("embed_model").and_then(|m| m.as_str()).map(String::from));

        let pop_rows = jq(
            &c,
            &format!(
                "SELECT (EXISTS(SELECT 1 FROM {s}.raw_turns WHERE embedding IS NOT NULL LIMIT 1) \
                 OR EXISTS(SELECT 1 FROM {s}.semantic WHERE embedding IS NOT NULL LIMIT 1)) AS p",
                s = self.schema
            ),
            &[],
        )
        .await?;
        let populated = pop_rows
            .first()
            .and_then(|r| r.get("p"))
            .and_then(|b| b.as_bool())
            .unwrap_or(false);

        // Seamless backward compatibility: if an existing 384-d database already has vectors
        // written by `BAAI/bge-small-en-v1.5` (either explicitly recorded or pre-dating schema
        // comments), automatically preserve `BAAI/bge-small-en-v1.5` unless the caller forced a
        // different model. Brand-new keyless databases default to `intfloat/multilingual-e5-small`.
        if populated && dim == 384 && !self.embedder.is_openai() && !self.embedder.explicit_local_override {
            match recorded_model.as_deref().map(canonical_embed_family) {
                None | Some("BAAI/bge-small-en-v1.5") => {
                    self.embedder.switch_local_model(crate::rerank::BGE_REPO_ID);
                }
                Some("intfloat/multilingual-e5-small") => {
                    self.embedder
                        .switch_local_model(crate::rerank::E5_MULTI_REPO_ID);
                }
                _ => {}
            }
        }

        let active_model = self.embedder.model_name();
        if populated && !allow_mismatch {
            if let Some(ref rec) = recorded_model {
                if canonical_embed_family(rec) != canonical_embed_family(&active_model) {
                    return Err(format!(
                        "embedding model mismatch on schema '{}': existing vectors were written with '{}', \
                         but active embedder is '{}'. Mixing incompatible {}-d vector spaces corrupts recall; \
                         configure the matching embedder or run `memnos migrate-embeddings`.",
                        self.schema, rec, active_model, dim
                    ));
                }
            } else if dim == 384 && canonical_embed_family(&active_model) != "BAAI/bge-small-en-v1.5" {
                return Err(format!(
                    "embedding model mismatch on schema '{}': pre-existing 384-d vectors were written with \
                     'BAAI/bge-small-en-v1.5', but active embedder is '{}'. Unset MEMNOS_LOCAL_MODEL or run \
                     `memnos migrate-embeddings`.",
                    self.schema, active_model
                ));
            }
        }

        if recorded_model.is_none() && dim == self.embedder.dim {
            let meta = json!({
                "embed_model": active_model,
                "embed_dim": dim,
            })
            .to_string()
            .replace('\'', "''");
            let comment_sql = format!("COMMENT ON SCHEMA {} IS '{}'", self.schema, meta);
            let _ = c.batch_execute(&comment_sql).await;
        }
        Ok(())
    }

    /// One trivial ANN probe per HNSW table so the first real recall after a restart does not pay
    /// the cold page-in cost.
    pub async fn warm_indexes(&self) -> Result<(), String> {
        let c = self.conn().await?;
        let dim = self.col_dim();
        let mut unit = vec![0.0f32; dim.max(1)];
        unit[0] = 1.0;
        let lit = vlit(&unit);
        let vt = self.vt();
        for t in ["raw_turns", "semantic", "episodic"] {
            let sql = format!(
                "SELECT id FROM {}.{} ORDER BY embedding <=> $1::text::{} LIMIT 1",
                self.schema, t, vt
            );
            c.query(sql.as_str(), &[&lit]).await.map_err(de)?;
        }
        Ok(())
    }

    /// Row counts per table.
    pub async fn counts(&self) -> Result<Value, String> {
        let mut out = Map::new();
        for t in ["raw_turns", "episodic", "semantic", "entities", "mentions", "edges", "provenance"] {
            let r = self.q(&format!("SELECT count(*)::bigint AS n FROM {}.{}", self.schema, t), &[]).await?;
            out.insert(t.into(), json!(r.first().map(|x| i_of(x, "n")).unwrap_or(0)));
        }
        Ok(Value::Object(out))
    }

    fn push_explain(&self, entry: Value) {
        let mut g = self.explain.lock().unwrap();
        if g.len() >= EXPLAIN_CAP {
            g.pop_front();
        }
        g.push_back(entry);
    }

    /// Most recent recall audit entries, newest first (backs `/recall/explain`).
    pub fn recall_explain(&self, limit: usize) -> Vec<Value> {
        self.explain.lock().unwrap().iter().rev().take(limit.min(EXPLAIN_CAP)).cloned().collect()
    }
}

// ---------------------------------------------------------------------------------------------
// Write path
// ---------------------------------------------------------------------------------------------

static KNOWN_NS: Lazy<Mutex<HashSet<String>>> = Lazy::new(|| Mutex::new(HashSet::new()));

struct PreparedFact {
    f: ExtractedFact,
    vec: Vec<f32>,
    /// `(canonical name, is_object, embedding)` entity nodes to upsert for this fact.
    ents: Vec<(String, bool, Vec<f32>)>,
}

struct NewSem<'a> {
    ns: &'a str,
    kind: &'a str,
    statement: &'a str,
    subject: Option<&'a str>,
    predicate: Option<&'a str>,
    object: Option<&'a str>,
    valid_from: Option<DateTime<Utc>>,
    vec: Option<String>,
    salience: f32,
    src_turns: Vec<i64>,
    author: Option<&'a str>,
    memory_type: Option<&'a str>,
    observed: Option<DateTime<Utc>>,
    speaker: Option<&'a str>,
    anchors: Option<String>,
}

fn nonempty(s: &str, cap: usize) -> Option<String> {
    let t: String = s.trim().chars().take(cap).collect();
    if t.is_empty() {
        None
    } else {
        Some(t)
    }
}

pub fn format_fix_memory(symptom: &str, cause: &str, fix: &str, verify_cmd: Option<&str>) -> String {
    let mut parts = vec![
        format!("Symptom: {}", symptom.trim()),
        format!("Cause: {}", cause.trim()),
        format!("Fix: {}", fix.trim()),
    ];
    if let Some(v) = verify_cmd.map(str::trim).filter(|v| !v.is_empty()) {
        parts.push(format!("Verify: {}", v));
    }
    parts.join("\n")
}

impl BrainStore {
    fn ensure_vt_cast(&self, n: usize) -> String {
        format!("${}::text::{}", n, self.vt())
    }

    fn derive_anchors(&self, text: &str) -> Option<Value> {
        let root = self.repo_root.as_ref()?;
        let a = code_anchors::extract_code_anchors(text, root, 5);
        if a.is_empty() {
            None
        } else {
            serde_json::to_value(a).ok()
        }
    }

    /// LLM/regex fact extraction (no DB held): model facts when an extractor is configured, plus
    /// deterministic developer signals (PR refs, SHAs, versions); de-duplicated by statement.
    async fn extract(&self, text: &str, observed: DateTime<Utc>) -> Vec<ExtractedFact> {
        let date = observed.format("%Y-%m-%d").to_string();
        let mut out = if self.extractor.enabled() {
            self.extractor.extract_facts(text, &date).await.0
        } else {
            vec![]
        };
        out.extend(extract_developer_signals(text));
        let mut seen = HashSet::new();
        out.retain(|f| !f.statement.trim().is_empty() && seen.insert(f.statement.trim().to_lowercase()));
        out
    }

    async fn prepare_facts(&self, facts: Vec<ExtractedFact>) -> Result<Vec<PreparedFact>, String> {
        let mut out = Vec::with_capacity(facts.len());
        for f in facts {
            let vec = self.embed(&f.statement).await?;
            let mut ents = vec![];
            if let Some(subj) = nonempty(&f.subject, 100) {
                let cs = canonicalize_entity_name(&subj, false);
                if is_valid_entity_name(&cs, false) {
                    ents.push((cs.clone(), false, self.embed(&cs).await?));
                    if let Some(o) = nonempty(&f.object, 100) {
                        let co = canonicalize_entity_name(&o, true);
                        if is_valid_entity_name(&co, true) {
                            ents.push((co.clone(), true, self.embed(&co).await?));
                        }
                    }
                }
            }
            out.push(PreparedFact { f, vec, ents });
        }
        Ok(out)
    }

    /// Store a message: redacted verbatim raw turn, then (unless it is a constraint) extracted
    /// facts with bi-temporal supersession, restatement dedupe and the entity graph.
    #[allow(clippy::too_many_arguments)]
    pub async fn remember(
        &self,
        namespace: &str,
        text: &str,
        speaker: Option<&str>,
        memory_type: Option<&str>,
        constraint_subject: Option<&str>,
        author: Option<&str>,
        turn_ts: Option<DateTime<Utc>>,
        code_anchors_opt: Option<Value>,
    ) -> Result<Value, String> {
        self.remember_ex(
            namespace,
            text,
            speaker,
            None,
            memory_type,
            constraint_subject,
            author,
            turn_ts,
            code_anchors_opt,
            true,
            false,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn remember_ex(
        &self,
        namespace: &str,
        text: &str,
        speaker: Option<&str>,
        session_id: Option<&str>,
        memory_type: Option<&str>,
        constraint_subject: Option<&str>,
        author: Option<&str>,
        turn_ts: Option<DateTime<Utc>>,
        code_anchors_opt: Option<Value>,
        do_extract: bool,
        async_extract: bool,
    ) -> Result<Value, String> {
        if namespace.trim().is_empty() {
            return Err("namespace required".into());
        }
        let observed = turn_ts.unwrap_or_else(Utc::now);
        let (clean, n_red) = redact(&clean_text(text));
        if clean.trim().is_empty() {
            return Err("text required".into());
        }
        let anchors = match code_anchors_opt {
            Some(v) if v.as_array().map_or(false, |a| !a.is_empty()) => Some(v),
            Some(_) => None,
            None => self.derive_anchors(&clean),
        };
        let anchors_json = anchors.as_ref().map(|v| v.to_string());
        let cs_derived = memory_type == Some("constraint")
            && constraint_subject.map(str::trim).unwrap_or("").is_empty();
        let cs = effective_constraint_subject(memory_type, constraint_subject, &clean);
        // Every network/CPU step happens before a pool connection is taken.
        let turn_vec = vlit(&self.embed(&clean).await?);
        // A constraint is a guardrail, not prose to summarize: extraction is bypassed entirely.
        let facts = if async_extract || !do_extract || memory_type == Some("constraint") {
            vec![]
        } else {
            self.extract(&clean, observed).await
        };
        let prepared = if facts.is_empty() {
            vec![]
        } else {
            self.prepare_facts(facts).await?
        };

        let mut c = self.conn().await?;
        if cs.is_some() {
            self.backfill_constraint_subjects(&c, namespace).await?;
        }
        let cast = self.ensure_vt_cast(8);
        let sql = format!(
            "INSERT INTO {}.raw_turns(namespace,session_id,speaker,text,observed_at,author_principal,memory_type,embedding,constraint_subject,code_anchors) \
             VALUES($1,$2,$3,$4,$5,$6,$7,{},$9,$10::text::jsonb) RETURNING id",
            self.schema, cast
        );
        let row = c
            .query_one(
                sql.as_str(),
                &[&namespace, &session_id, &speaker, &clean, &observed, &author, &memory_type, &turn_vec, &cs, &anchors_json],
            )
            .await
            .map_err(de)?;
        let turn_id: i64 = row.get(0);
        self.register_namespace(&c, namespace).await;
        let retired = match &cs {
            Some(s) => self.retire_constraints(&c, namespace, s, "turn", turn_id).await?,
            None => vec![],
        };

        if async_extract {
            let mut out = json!({
                "ok": true,
                "turn_id": turn_id,
                "facts": Value::Null,
                "facts_written": 0,
                "fact_ids": [],
                "redacted": n_red > 0,
                "superseded": 0,
                "facts_restated": 0,
                "extraction": "queued",
                "namespace": namespace,
            });
            if let Some(s) = &cs {
                out["constraint_subject"] = json!(s);
                out["constraint_subject_derived"] = json!(cs_derived);
            }
            if !retired.is_empty() {
                out["constraints_retired"] = json!(retired);
            }
            return Ok(out);
        }

        let spk: Option<String> = speaker.map(|x| x.chars().take(40).collect());
        let (mut ids, mut superseded, mut restated) = (Vec::new(), 0usize, 0usize);
        let mut facts_error: Option<String> = None;
        for pf in &prepared {
            match self.write_fact(&mut c, namespace, pf, observed, turn_id, memory_type, spk.as_deref(), author, &anchors_json).await {
                Ok((Some(id), n)) => {
                    ids.push(id);
                    superseded += n;
                }
                Ok((None, _)) => restated += 1,
                // The turn is already committed: report the failure but return Ok so a retrying
                // caller does not insert the turn a second time.
                Err(e) => {
                    facts_error = Some(e);
                    break;
                }
            }
        }
        let mut out = json!({
            "ok": true,
            "turn_id": turn_id,
            "facts": ids.len(),
            "facts_written": ids.len(),
            "fact_ids": ids,
            "redacted": n_red > 0,
            "superseded": superseded,
            "facts_restated": restated,
            "namespace": namespace,
        });
        if let Some(e) = facts_error {
            out["facts_error"] = json!(e);
        }
        if let Some(s) = &cs {
            out["constraint_subject"] = json!(s);
            out["constraint_subject_derived"] = json!(cs_derived);
        }
        if !retired.is_empty() {
            out["constraints_retired"] = json!(retired);
        }
        Ok(out)
    }

    /// `/memory/write` is an alias of `remember`.
    #[allow(clippy::too_many_arguments)]
    pub async fn memory_write(
        &self,
        namespace: &str,
        text: &str,
        speaker: Option<&str>,
        memory_type: Option<&str>,
        constraint_subject: Option<&str>,
        author: Option<&str>,
    ) -> Result<Value, String> {
        self.remember(namespace, text, speaker, memory_type, constraint_subject, author, None, None).await
    }

    /// Record a debugging fix as a `fix` memory (boosted on error-shaped recall queries).
    pub async fn record_fix(
        &self,
        namespace: &str,
        symptom: &str,
        cause: &str,
        fix: &str,
        verify_cmd: Option<&str>,
        author: Option<&str>,
    ) -> Result<Value, String> {
        if symptom.trim().is_empty() || fix.trim().is_empty() {
            return Err("symptom and fix required".into());
        }
        let text = format_fix_memory(symptom, cause, fix, verify_cmd);
        let mut out = self.remember(namespace, &text, None, Some("fix"), None, author, None, None).await?;
        out["memory_type"] = json!("fix");
        Ok(out)
    }

    /// Chunk a file's text into paragraph-sized turns and remember each (speaker `file`). `content`
    /// wins over reading `path`; the caller owns the decision to allow reading a local path.
    pub async fn ingest_file(
        &self,
        namespace: &str,
        path: &str,
        content: Option<&str>,
        author: Option<&str>,
    ) -> Result<Value, String> {
        let text = match content {
            Some(c) => c.to_string(),
            None => std::fs::read_to_string(path).map_err(|e| format!("cannot read {}: {}", path, e))?,
        };
        self.ingest_file_opts(namespace, path, &text, 1500, true, author).await
    }

    pub async fn ingest_file_opts(
        &self,
        namespace: &str,
        filename: &str,
        text: &str,
        chunk_size: usize,
        do_extract: bool,
        author: Option<&str>,
    ) -> Result<Value, String> {
        let cap = chunk_size.clamp(200, 10_000);
        let label = if filename.trim().is_empty() { "document" } else { filename.trim() };
        let mut chunks: Vec<String> = vec![];
        let mut cur = String::new();
        for para in text.split("\n\n") {
            let p = para.trim();
            if p.is_empty() {
                continue;
            }
            if !cur.is_empty() && cur.len() + p.len() > cap {
                chunks.push(std::mem::take(&mut cur));
            }
            if !cur.is_empty() {
                cur.push_str("\n\n");
            }
            cur.push_str(p);
        }
        if !cur.is_empty() {
            chunks.push(cur);
        }
        chunks.truncate(500);
        let (mut turns, mut facts) = (Vec::new(), 0usize);
        for ch in &chunks {
            let body = format!("[{}]\n{}", label, ch);
            let r = self
                .remember_ex(namespace, &body, Some("file"), None, None, None, author, None, None, do_extract, false)
                .await?;
            turns.push(r["turn_id"].clone());
            facts += r["facts_written"].as_u64().unwrap_or(0) as usize;
        }
        Ok(json!({
            "ok": true,
            "filename": label,
            "path": label,
            "chunks": chunks.len(),
            "facts_written": facts,
            "turn_ids": turns,
        }))
    }

    /// Register a first-seen namespace in the control-plane registry. Best effort: the turn is
    /// already committed, and the registry table belongs to the control module.
    async fn register_namespace(&self, c: &Client, ns: &str) {
        if KNOWN_NS.lock().unwrap().contains(ns) {
            return;
        }
        let r = c
            .execute(
                "INSERT INTO memnos_control.namespaces(name, auto_registered) VALUES($1, true) ON CONFLICT (name) DO NOTHING",
                &[&ns],
            )
            .await;
        if r.is_ok() {
            KNOWN_NS.lock().unwrap().insert(ns.to_string());
        }
    }

    async fn insert_semantic(&self, c: &Client, n: &NewSem<'_>) -> Result<i64, String> {
        self.insert_semantic_g(c, n).await
    }

    async fn insert_semantic_g<C: GenericClient>(&self, c: &C, n: &NewSem<'_>) -> Result<i64, String> {
        let src: Option<Vec<i64>> = if n.src_turns.is_empty() { None } else { Some(n.src_turns.clone()) };
        let sql = format!(
            "INSERT INTO {}.semantic(namespace,kind,statement,subject_entity,predicate,object,valid_from,salience,embedding,\
             source_turn_ids,author_principal,memory_type,observed_at,source_speaker,code_anchors) \
             VALUES($1,$2,$3,$4,$5,$6,$7,$8,{},$10,$11,$12,COALESCE($13::timestamptz, now()),$14,$15::text::jsonb) RETURNING id",
            self.schema,
            self.ensure_vt_cast(9)
        );
        let row = c
            .query_one(
                sql.as_str(),
                &[&n.ns, &n.kind, &n.statement, &n.subject, &n.predicate, &n.object, &n.valid_from, &n.salience, &n.vec, &src, &n.author, &n.memory_type, &n.observed, &n.speaker, &n.anchors],
            )
            .await
            .map_err(de)?;
        Ok(row.get(0))
    }

    /// Write ONE extracted fact. Returns `(Some(new_id), superseded)` or `(None, 0)` when the fact
    /// was a near-duplicate restatement that only reinforced an existing live fact.
    #[allow(clippy::too_many_arguments)]
    async fn write_fact(
        &self,
        c: &mut deadpool_postgres::Object,
        ns: &str,
        pf: &PreparedFact,
        observed: DateTime<Utc>,
        turn_id: i64,
        memory_type: Option<&str>,
        speaker: Option<&str>,
        author: Option<&str>,
        anchors: &Option<String>,
    ) -> Result<(Option<i64>, usize), String> {
        let stmt = pf.f.statement.trim();
        let subj = nonempty(&pf.f.subject, 100);
        let pred = nonempty(&pf.f.predicate, 200);
        let obj = nonempty(&pf.f.object, 500);
        let ev = temporal::parse_event_date(stmt, Some(observed)).unwrap_or(observed);
        let vec_lit = vlit(&pf.vec);

        // Restatement of a LIVE fact reinforces it instead of inserting a duplicate. The object must
        // not contradict, or "lives in Austin" -> "lives in Seattle" would be eaten as a duplicate.
        // 0.03 cosine distance matches the Python engine. A looser value (0.06) was measured to
        // swallow two distinct statements differing in one identifier (distance 0.045), and a
        // reversal ("no longer blocked") is never a restatement of what it reverses.
        let thresh = env_f64("MEMNOS_DEDUPE_THRESHOLD", 0.03);
        if thresh > 0.0 && !REVERSAL_RE.is_match(stmt) {
            static HARD_ID_RE: Lazy<Regex> =
                Lazy::new(|| Regex::new(r"[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*").unwrap());
            let ids_of = |s: &str| -> HashSet<String> {
                HARD_ID_RE
                    .find_iter(s)
                    .map(|m| m.as_str().trim_matches('.').to_ascii_lowercase())
                    .filter(|t| !t.is_empty())
                    .collect()
            };
            let new_ids = ids_of(stmt);
            let cast = self.ensure_vt_cast(4);
            let sql = format!(
                "SELECT id, statement FROM {s}.semantic WHERE namespace=$1 AND kind='fact' AND valid_to IS NULL AND expired_at IS NULL \
                 AND embedding IS NOT NULL AND ($2::text IS NULL OR subject_entity IS NULL OR lower(subject_entity)=lower($2)) \
                 AND ($3::text IS NULL OR object IS NULL OR lower(object)=lower($3)) \
                 AND (embedding <=> {v}) < $5::float8 ORDER BY embedding <=> {v} LIMIT 5",
                s = self.schema,
                v = cast
            );
            let cands = jq(c, &sql, &[&ns, &subj, &obj, &vec_lit, &thresh]).await?;
            if let Some(d) = cands
                .iter()
                .find(|r| ids_of(s_of(r, "statement")) == new_ids)
            {
                self.bump_restatement(c, i_of(d, "id"), &[turn_id]).await?;
                return Ok((None, 0));
            }
        }

        let hist = is_historical(stmt);
        let explicit_ev = if hist { temporal::parse_event_date(stmt, None) } else { None };
        let new = NewSem {
            ns,
            kind: "fact",
            statement: stmt,
            subject: subj.as_deref(),
            predicate: pred.as_deref(),
            object: obj.as_deref(),
            valid_from: Some(ev),
            vec: Some(vec_lit.clone()),
            salience: 0.5,
            src_turns: vec![turn_id],
            author,
            memory_type,
            observed: Some(observed),
            speaker,
            anchors: anchors.clone(),
        };
        let mut n_super = 0usize;
        let fid;
        let belief_change = subj.is_some()
            && pred.is_some()
            && supersedable(pred.as_deref().unwrap_or(""), obj.as_deref())
            && !(hist && explicit_ev.is_none());
        if belief_change {
            // One transaction under an advisory lock so concurrent writers on the same
            // (namespace, subject, predicate) cannot both land live.
            // `Transaction` rolls back on drop, so a cancelled future cannot leak an open
            // transaction (or its advisory lock) back into the pool.
            let tx = c.transaction().await.map_err(de)?;
            let (id, n) = self.supersede_and_insert(&*tx, &new, ev, observed, hist, explicit_ev).await?;
            tx.commit().await.map_err(de)?;
            fid = id;
            n_super = n;
        } else {
            fid = self.insert_semantic(c, &new).await?;
        }

        // Reversal/negation ("no longer blocked") has no usable predicate, so close out the nearest
        // live facts when the guard passes (cue + distance + subject + salient-token overlap).
        let neg = env_f64("MEMNOS_NEGATION_THRESHOLD", 0.40);
        if n_super == 0 && neg > 0.0 && REVERSAL_RE.is_match(stmt) {
            let cast = self.ensure_vt_cast(2);
            let sql = format!(
                "SELECT id, statement, subject_entity, (embedding <=> {v}) AS dist FROM {s}.semantic \
                 WHERE namespace=$1 AND kind='fact' AND valid_to IS NULL AND expired_at IS NULL AND embedding IS NOT NULL \
                 AND id <> $3 AND observed_at <= $4 ORDER BY embedding <=> {v} LIMIT 8",
                s = self.schema,
                v = cast
            );
            let cands = jq(c, &sql, &[&ns, &vec_lit, &fid, &observed]).await?;
            for id in negation_targets(stmt, subj.as_deref(), &cands, neg) {
                n_super += self.close_out(c, ns, id, ev, Some(fid)).await?;
            }
        }

        // Entity graph from the SPO triple: valid named concepts only, never literals/clauses.
        let mut subj_id: Option<i64> = None;
        for (name, is_obj, v) in &pf.ents {
            let eid = self.upsert_entity(c, ns, name, Some(vlit(v))).await?;
            self.add_mention(c, eid, fid, "semantic").await?;
            if *is_obj {
                if let Some(sid) = subj_id {
                    self.bump_edge(c, ns, sid, eid, 1.0).await?;
                }
            } else {
                subj_id = Some(eid);
            }
        }
        Ok((Some(fid), n_super))
    }

    async fn supersede_and_insert<C: GenericClient>(
        &self,
        c: &C,
        new: &NewSem<'_>,
        ev: DateTime<Utc>,
        observed: DateTime<Utc>,
        hist: bool,
        explicit_ev: Option<DateTime<Utc>>,
    ) -> Result<(i64, usize), String> {
        let (ns, subj, pred) = (new.ns, new.subject, new.predicate);
        let lock_key = format!("{}\x01{}\x01{}", ns, subj.unwrap_or("").to_lowercase(), pred.unwrap_or("").to_lowercase());
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", &[&lock_key]).await.map_err(de)?;
        let s = &self.schema;
        let sup = jq_g(
            c,
            &format!(
                "UPDATE {s}.semantic SET valid_to=GREATEST(coalesce(valid_from,$5::timestamptz),$5::timestamptz) \
                 WHERE namespace=$1 AND valid_to IS NULL AND expired_at IS NULL \
                 AND lower(subject_entity)=lower($2) AND lower(predicate)=lower($3) \
                 AND lower(coalesce(object,''))<>lower(coalesce($4::text,'')) \
                 AND observed_at <= $6::timestamptz \
                 AND (NOT $7::bool OR valid_from IS NULL OR ($8::timestamptz IS NOT NULL AND valid_from <= $8::timestamptz)) \
                 RETURNING id"
            ),
            &[&ns, &subj, &pred, &new.object, &ev, &observed, &hist, &explicit_ev],
        )
        .await?;
        let sup_ids: Vec<i64> = sup.iter().map(|r| i_of(r, "id")).collect();
        // Mirror image: an already-live fact observed STRICTLY later dominates the arriving one.
        let dom = jq_g(
            c,
            &format!(
                "SELECT id, valid_from FROM {s}.semantic WHERE namespace=$1 AND valid_to IS NULL AND expired_at IS NULL \
                 AND lower(subject_entity)=lower($2) AND lower(predicate)=lower($3) \
                 AND lower(coalesce(object,''))<>lower(coalesce($4::text,'')) AND observed_at > $5::timestamptz \
                 AND (NOT $6::bool OR valid_from IS NULL OR ($7::timestamptz IS NOT NULL AND valid_from <= $7::timestamptz)) \
                 ORDER BY observed_at DESC LIMIT 1"
            ),
            &[&ns, &subj, &pred, &new.object, &observed, &hist, &explicit_ev],
        )
        .await?;
        let fid = self.insert_semantic_g(c, new).await?;
        if !sup_ids.is_empty() {
            c.execute(format!("UPDATE {s}.semantic SET superseded_by=$1 WHERE id = ANY($2)").as_str(), &[&fid, &sup_ids])
                .await
                .map_err(de)?;
        }
        if let Some(d) = dom.first() {
            let vt = parse_ts(d, "valid_from").unwrap_or(observed);
            self.close_out_g(c, ns, fid, vt, Some(i_of(d, "id"))).await?;
        }
        Ok((fid, sup_ids.len()))
    }

    /// Close out (never delete) a live fact: `valid_to` clamped to never precede its own start.
    async fn close_out(&self, c: &Client, ns: &str, id: i64, valid_to: DateTime<Utc>, by: Option<i64>) -> Result<usize, String> {
        self.close_out_g(c, ns, id, valid_to, by).await
    }

    async fn close_out_g<C: GenericClient>(&self, c: &C, ns: &str, id: i64, valid_to: DateTime<Utc>, by: Option<i64>) -> Result<usize, String> {
        let n = c
            .execute(
                format!(
                    "UPDATE {s}.semantic SET valid_to=GREATEST(coalesce(valid_from,$3::timestamptz),$3::timestamptz), \
                     superseded_by=COALESCE($4::bigint, superseded_by) \
                     WHERE id=$1 AND namespace=$2 AND valid_to IS NULL AND expired_at IS NULL",
                    s = self.schema
                )
                .as_str(),
                &[&id, &ns, &valid_to, &by],
            )
            .await
            .map_err(de)?;
        Ok(n as usize)
    }

    /// Reinforce a live fact: provenance union always; restatement counter and salience only when
    /// a restating turn is not an assistant echo (assistant echoes are not independent evidence).
    async fn bump_restatement(&self, c: &Client, fact_id: i64, turns: &[i64]) -> Result<(), String> {
        let ids: Vec<i64> = turns.to_vec();
        let s = &self.schema;
        c.execute(
            format!(
                "WITH corr AS (SELECT (cardinality($1::bigint[]) = 0 OR EXISTS (SELECT 1 FROM {s}.raw_turns rt \
                 WHERE rt.id = ANY($1::bigint[]) AND coalesce(lower(rt.speaker),'') <> 'assistant')) AS ok) \
                 UPDATE {s}.semantic SET \
                 restatements = restatements + CASE WHEN (SELECT ok FROM corr) THEN 1 ELSE 0 END, \
                 salience = CASE WHEN (SELECT ok FROM corr) THEN LEAST(1.0, salience + 0.1) ELSE salience END, \
                 source_turn_ids = (SELECT ARRAY(SELECT DISTINCT t FROM unnest(coalesce(source_turn_ids,'{{}}'::bigint[]) || $1::bigint[]) AS t ORDER BY t)) \
                 WHERE id=$2"
            )
            .as_str(),
            &[&ids, &fact_id],
        )
        .await
        .map_err(de)?;
        Ok(())
    }

    async fn upsert_entity(&self, c: &Client, ns: &str, name: &str, vec_lit: Option<String>) -> Result<i64, String> {
        let clean = name.split_whitespace().collect::<Vec<_>>().join(" ");
        let s = &self.schema;
        let ex = jq(
            c,
            &format!("SELECT id, embedding IS NOT NULL AS has_emb FROM {s}.entities WHERE namespace=$1 AND lower(name)=lower($2) ORDER BY id LIMIT 1"),
            &[&ns, &clean],
        )
        .await?;
        if let Some(e) = ex.first() {
            let id = i_of(e, "id");
            if !e["has_emb"].as_bool().unwrap_or(true) {
                if let Some(v) = &vec_lit {
                    let sql = format!("UPDATE {s}.entities SET embedding={} WHERE id=$2", self.ensure_vt_cast(1));
                    c.execute(sql.as_str(), &[v, &id]).await.map_err(de)?;
                }
            }
            return Ok(id);
        }
        let sql = format!(
            "INSERT INTO {s}.entities(namespace,name,embedding) VALUES($1,$2,{}) \
             ON CONFLICT (namespace,name) DO UPDATE SET embedding = COALESCE({s}.entities.embedding, EXCLUDED.embedding) RETURNING id",
            self.ensure_vt_cast(3)
        );
        let row = c.query_one(sql.as_str(), &[&ns, &clean, &vec_lit]).await.map_err(de)?;
        Ok(row.get(0))
    }

    async fn add_mention(&self, c: &Client, entity_id: i64, memory_id: i64, kind: &str) -> Result<(), String> {
        c.execute(
            format!("INSERT INTO {}.mentions(entity_id,memory_id,memory_kind) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", self.schema).as_str(),
            &[&entity_id, &memory_id, &kind],
        )
        .await
        .map_err(de)?;
        Ok(())
    }

    /// Co-mention edge; weight accumulates.
    async fn bump_edge(&self, c: &Client, ns: &str, a: i64, b: i64, w: f32) -> Result<(), String> {
        if a == b {
            return Ok(());
        }
        let (lo, hi) = if a < b { (a, b) } else { (b, a) };
        let s = &self.schema;
        c.execute(
            format!(
                "INSERT INTO {s}.edges(namespace,src_entity,dst_entity,weight) VALUES($1,$2,$3,$4) \
                 ON CONFLICT (namespace,src_entity,dst_entity) DO UPDATE SET weight = {s}.edges.weight + EXCLUDED.weight"
            )
            .as_str(),
            &[&ns, &lo, &hi, &w],
        )
        .await
        .map_err(de)?;
        Ok(())
    }

    // --- pinned constraints ----------------------------------------------------------------

    /// Give every live untagged constraint in `ns` (all three stores) a derived subject so it is
    /// retirable.
    async fn backfill_constraint_subjects(&self, c: &Client, ns: &str) -> Result<usize, String> {
        let mut n = 0;
        for (tbl, col) in [("raw_turns", "text"), ("semantic", "statement"), ("episodic", "text")] {
            let rows = jq(
                c,
                &format!(
                    "SELECT id, {col} AS content FROM {s}.{tbl} WHERE namespace=$1 AND memory_type='constraint' \
                     AND constraint_retired_at IS NULL AND (constraint_subject IS NULL OR constraint_subject='')",
                    s = self.schema
                ),
                &[&ns],
            )
            .await?;
            for r in rows {
                let subj = derive_constraint_subject(s_of(&r, "content"));
                let id = i_of(&r, "id");
                c.execute(
                    format!("UPDATE {}.{} SET constraint_subject=$1 WHERE id=$2", self.schema, tbl).as_str(),
                    &[&subj, &id],
                )
                .await
                .map_err(de)?;
                n += 1;
            }
        }
        Ok(n)
    }

    /// Retire every OTHER live constraint in `ns` sharing `subject`, superseded by `keep_kind:keep_id`.
    async fn retire_constraints(&self, c: &Client, ns: &str, subject: &str, keep_kind: &str, keep_id: i64) -> Result<Vec<Value>, String> {
        let by = format!("{}:{}", keep_kind, keep_id);
        let mut retired = vec![];
        for (tbl, kind) in [("raw_turns", "turn"), ("semantic", "fact"), ("episodic", "episode")] {
            let excl = if kind == keep_kind { "AND id <> $4" } else { "AND $4::bigint IS NOT NULL" };
            let rows = jq(
                c,
                &format!(
                    "UPDATE {s}.{tbl} SET constraint_retired_at=now(), constraint_retired_by=$3 \
                     WHERE namespace=$1 AND memory_type='constraint' AND constraint_subject=$2 \
                     AND constraint_retired_at IS NULL {excl} RETURNING id",
                    s = self.schema
                ),
                &[&ns, &subject, &by, &keep_id],
            )
            .await?;
            retired.extend(rows.iter().map(|r| json!({"kind": kind, "id": r["id"]})));
        }
        Ok(retired)
    }

    /// Standalone retirement of a live pinned constraint. `id` is a digit string or `kind:id`
    /// (`turn|fact|episode`); without a kind every store in `namespace` is tried. Returns the rows
    /// retired as `{kind,id,content,constraint_subject}`.
    pub async fn retire_constraint_rows(&self, namespace: &str, id: &str, retired_by: &str) -> Result<Vec<Value>, String> {
        let (kind, num) = match id.split_once(':') {
            Some((k, n)) => (Some(k.trim().to_lowercase()), n.trim()),
            None => (None, id.trim()),
        };
        let cid: i64 = num.parse().map_err(|_| format!("bad constraint id: {:?}", id))?;
        if let Some(k) = &kind {
            if !matches!(k.as_str(), "turn" | "fact" | "episode") {
                return Err(format!("bad constraint kind: {:?} (turn|fact|episode)", k));
            }
        }
        let c = self.conn().await?;
        let mut out = vec![];
        for (tbl, col, k) in [("raw_turns", "text", "turn"), ("semantic", "statement", "fact"), ("episodic", "text", "episode")] {
            if kind.as_deref().map_or(false, |x| x != k) {
                continue;
            }
            let rows = jq(
                &c,
                &format!(
                    "UPDATE {s}.{tbl} SET constraint_retired_at=now(), constraint_retired_by=$3 \
                     WHERE id=$1 AND namespace=$2 AND memory_type='constraint' AND constraint_retired_at IS NULL \
                     RETURNING id, {col} AS content, constraint_subject",
                    s = self.schema
                ),
                &[&cid, &namespace, &retired_by],
            )
            .await?;
            for mut r in rows {
                r["kind"] = json!(k);
                out.push(r);
            }
        }
        Ok(out)
    }

    pub async fn retire_constraint_by_id(&self, namespace: &str, id: &str) -> Result<bool, String> {
        Ok(!self.retire_constraint_rows(namespace, id, "manual").await?.is_empty())
    }

    /// Live (unretired) pinned constraints in `namespace`, all three stores.
    pub async fn list_pinned_constraints(&self, namespace: &str) -> Result<Vec<Value>, String> {
        let c = self.conn().await?;
        let mut out = vec![];
        for (tbl, col, k) in [("raw_turns", "text", "turn"), ("semantic", "statement", "fact"), ("episodic", "text", "episode")] {
            let rows = jq(
                &c,
                &format!(
                    "SELECT id, {col} AS content, constraint_subject FROM {s}.{tbl} WHERE namespace=$1 \
                     AND memory_type='constraint' AND constraint_retired_at IS NULL ORDER BY id",
                    s = self.schema
                ),
                &[&namespace],
            )
            .await?;
            for mut r in rows {
                r["kind"] = json!(k);
                out.push(r);
            }
        }
        Ok(out)
    }

    /// Expire (system-time, never hard-delete) a semantic fact, or retire a pinned constraint that
    /// lives in `raw_turns` (local mode has no extraction, so the verbatim turn IS the constraint).
    pub async fn memory_delete(&self, namespace: &str, id: i64) -> Result<Value, String> {
        let s = &self.schema;
        let existing = self
            .q(&format!("SELECT id, statement, memory_type FROM {s}.semantic WHERE id=$1 AND namespace=$2"), &[&id, &namespace])
            .await?;
        if let Some(e) = existing.first() {
            self.q(
                &format!("UPDATE {s}.semantic SET expired_at=now(), valid_to=COALESCE(valid_to, now()) WHERE id=$1 AND namespace=$2 RETURNING id"),
                &[&id, &namespace],
            )
            .await?;
            if s_of(e, "memory_type") == "constraint" {
                self.retire_constraint_rows(namespace, &format!("fact:{}", id), "memory_delete").await?;
            }
            return Ok(json!({"ok": true, "deleted": true, "id": id, "statement": e["statement"]}));
        }
        let retired = self.retire_constraint_rows(namespace, &id.to_string(), "memory_delete").await?;
        if retired.is_empty() {
            return Ok(json!({"ok": true, "deleted": false, "id": id}));
        }
        Ok(json!({"ok": true, "deleted": true, "id": id, "statement": retired[0]["content"], "constraints_retired": retired}))
    }

    /// Exact, non-ranked enumeration of raw turns tagged `memory_type`, oldest first.
    pub async fn list_by_type(&self, namespace: &str, memory_type: &str, limit: i64) -> Result<Value, String> {
        let rows = self
            .q(
                &format!(
                    "SELECT id, text AS content, observed_at, author_principal AS author, memory_type FROM {}.raw_turns \
                     WHERE namespace=$1 AND memory_type=$2 ORDER BY id ASC LIMIT $3",
                    self.schema
                ),
                &[&namespace, &memory_type, &limit.clamp(1, 1000)],
            )
            .await?;
        Ok(json!({"namespace": namespace, "memory_type": memory_type, "count": rows.len(), "memories": rows}))
    }
}

// ---------------------------------------------------------------------------------------------
// Hybrid retrieval
// ---------------------------------------------------------------------------------------------

const SEM_COLS: &str = "s.id, s.statement AS content, s.valid_from, s.valid_to, s.author_principal AS author, s.memory_type, \
     s.restatements, s.salience, s.inference_confidence, s.inference_basis, s.subject_entity, s.source_speaker, \
     s.code_anchors, s.namespace";
const RAW_COLS: &str = "t.id, t.text AS content, t.observed_at, t.author_principal AS author, t.memory_type, t.speaker, \
     t.code_anchors, t.namespace";
const EPI_COLS: &str = "e.id, COALESCE(e.summary, e.text) AS content, e.observed_at, e.author_principal AS author, \
     e.memory_type, e.namespace";

fn keys(rows: &[Value], prefix: &str) -> Vec<String> {
    rows.iter().map(|r| format!("{}:{}", prefix, i_of(r, "id"))).collect()
}

/// Fuse several ranked row lists of one kind into scored rows (RRF, k = 60).
fn fuse_rows(lists: Vec<Vec<Value>>, prefix: &str) -> Vec<(Value, f64)> {
    let key_lists: Vec<Vec<String>> = lists.iter().map(|l| keys(l, prefix)).collect();
    let mut by_key: HashMap<String, Value> = HashMap::new();
    for l in lists {
        for r in l {
            by_key.entry(format!("{}:{}", prefix, i_of(&r, "id"))).or_insert(r);
        }
    }
    rrf_fuse(&key_lists, RRF_K)
        .into_iter()
        .filter_map(|(k, s)| by_key.remove(&k).map(|r| (r, s)))
        .collect()
}

fn git_weight(r: &Value) -> f64 {
    let spk = [s_of(r, "speaker"), s_of(r, "source_speaker")]
        .iter()
        .find(|s| !s.is_empty())
        .map(|s| s.trim().to_lowercase())
        .unwrap_or_default();
    if spk == "git" {
        env_f64("MEMNOS_GIT_SPEAKER_WEIGHT", 0.85).clamp(0.1, 1.0)
    } else {
        1.0
    }
}

/// Rank-time shaping (content and storage untouched, every factor bounded): long discursive turns
/// are penalised, restated/salient facts nudged up, facts get a small preference over turns for
/// non-verbatim intents, and fix/skill memories are boosted on error-shaped queries.
fn adjust_score(kind: &str, r: &Value, score: f64, err_q: bool, verbatim: bool) -> f64 {
    let mut s = score;
    match kind {
        "turn" => {
            let len = s_of(r, "content").len().max(1) as f64;
            s /= 1.0 + 0.15 * (len / 400.0).ln().max(0.0);
        }
        "fact" => {
            let rest = f_of(r, "restatements");
            let sal = f_of(r, "salience");
            s *= 1.0 + (0.05 * (rest.ln_1p() + (sal - 0.5).max(0.0))).min(0.25);
            if !verbatim {
                s *= 1.06;
            }
        }
        _ => {}
    }
    if err_q && matches!(s_of(r, "memory_type"), "fix" | "skill") {
        s *= 1.0 + env_f64("MEMNOS_FIX_BOOST", 0.10).clamp(0.0, 0.25);
    }
    s * git_weight(r)
}

fn shape_row(kind: &str, r: &Value, score: f64, primary_ns: Option<&str>) -> Value {
    let mut o = Map::new();
    o.insert("id".into(), r["id"].clone());
    o.insert("kind".into(), json!(kind));
    o.insert("content".into(), r["content"].clone());
    o.insert("score".into(), json!(score));
    if kind == "fact" {
        if let Some(d) = utc_date_of(r, "valid_from") {
            o.insert("date".into(), json!(d));
        }
    }
    for (src, dst) in [("author", "author"), ("memory_type", "type"), ("speaker", "speaker")] {
        if !s_of(r, src).is_empty() {
            o.insert(dst.into(), r[src].clone());
        }
    }
    let row_ns = s_of(r, "namespace");
    if !row_ns.is_empty() {
        match primary_ns {
            None => {
                o.insert("namespace".into(), r["namespace"].clone());
            }
            Some(pns) if row_ns != pns => {
                o.insert("namespace".into(), r["namespace"].clone());
            }
            _ => {}
        }
    }
    if s_of(r, "memory_type") == "inferred" {
        if !s_of(r, "inference_confidence").is_empty() {
            o.insert("confidence".into(), r["inference_confidence"].clone());
        }
        if !s_of(r, "inference_basis").is_empty() {
            o.insert("basis".into(), r["inference_basis"].clone());
        }
    }
    if let Some(a) = r.get("code_anchors").filter(|a| !a.is_null()) {
        o.insert("code_anchors".into(), a.clone());
    }
    if let Some(n) = r.get("dup_count") {
        o.insert("dup_count".into(), n.clone());
    }
    if let Some(sub) = r.get("subject_entity").filter(|a| !a.is_null()) {
        o.insert("subject_entity".into(), sub.clone());
    }
    Value::Object(o)
}

fn norm_content(s: &str) -> String {
    s.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ")
}

fn degrade(res: Result<Vec<Value>, String>, ns: &str, arm: &str, reasons: &mut Vec<Value>) -> Vec<Value> {
    match res {
        Ok(r) => r,
        Err(e) => {
            reasons.push(json!({"namespace": ns, "arm": arm, "error": e}));
            vec![]
        }
    }
}

struct RecallOut {
    pins: Vec<Value>,
    ranked: Vec<Value>,
    reasons: Vec<Value>,
    degraded: bool,
}

impl BrainStore {
    /// Pinned constraints across `nss`: every live `constraint` row in semantic, raw turns and
    /// episodes regardless of query similarity, oldest first, content-deduped, precedence-resolved
    /// BEFORE the cap so a loser never takes a slot a winner needed. Returns `(pins, suppressed)`.
    pub async fn pinned_constraints(
        &self,
        nss: &[String],
        cap: usize,
        overrides: &HashSet<(String, String)>,
    ) -> Result<(Vec<Value>, Vec<Value>), String> {
        if nss.is_empty() || cap == 0 {
            return Ok((vec![], vec![]));
        }
        let s = &self.schema;
        let lim = (cap * 3).max(50) as i64;
        let sql = format!(
            "SELECT id, content, kind, ts, author, namespace, constraint_subject FROM ( \
               SELECT id, statement AS content, 'fact'::text AS kind, COALESCE(valid_from, created_at) AS ts, \
                      author_principal AS author, namespace, constraint_subject FROM {s}.semantic \
                 WHERE namespace = ANY($1) AND memory_type='constraint' AND valid_to IS NULL AND expired_at IS NULL \
                   AND constraint_retired_at IS NULL AND (source_turn_ids IS NULL OR cardinality(source_turn_ids)=0) \
               UNION ALL \
               SELECT id, text, 'turn'::text, observed_at, author_principal, namespace, constraint_subject FROM {s}.raw_turns \
                 WHERE namespace = ANY($1) AND memory_type='constraint' AND constraint_retired_at IS NULL \
               UNION ALL \
               SELECT id, text, 'episode'::text, COALESCE(t_start, observed_at), author_principal, namespace, constraint_subject \
                 FROM {s}.episodic WHERE namespace = ANY($1) AND memory_type='constraint' AND constraint_retired_at IS NULL \
             ) u ORDER BY ts, content LIMIT $2"
        );
        let nss_v = nss.to_vec();
        let rows = self.q(&sql, &[&nss_v, &lim]).await?;
        let mut seen = HashSet::new();
        let rows: Vec<Value> = rows.into_iter().filter(|r| seen.insert(s_of(r, "content").to_string())).collect();
        let (mut winners, losers) = resolve_constraint_precedence(rows, overrides);
        winners.sort_by(|a, b| (s_of(a, "ts"), s_of(a, "content")).cmp(&(s_of(b, "ts"), s_of(b, "content"))));
        winners.truncate(cap);
        Ok((winners, losers))
    }

    async fn ann_fts(
        &self,
        table: &str,
        alias: &str,
        cols: &str,
        extra: &str,
        nss: &[String],
        qlit: &str,
        qt: &str,
        k: i64,
    ) -> (Result<Vec<Value>, String>, Result<Vec<Value>, String>) {
        let (s, vt) = (&self.schema, self.vt());
        let nss_v = nss.to_vec();
        let vec_sql = format!(
            "SELECT {cols} FROM {s}.{table} {a} WHERE {a}.namespace = ANY($1) {extra} AND {a}.embedding IS NOT NULL \
             ORDER BY {a}.embedding <=> $2::text::{vt} LIMIT $3",
            a = alias
        );
        let fts_sql = format!(
            "SELECT {cols} FROM {s}.{table} {a} WHERE {a}.namespace = ANY($1) {extra} \
             AND {a}.fts @@ websearch_to_tsquery('english',$2) \
             ORDER BY ts_rank({a}.fts, websearch_to_tsquery('english',$2)) DESC, {a}.id LIMIT $3",
            a = alias
        );
        let (vp, fp): (Prm, Prm) = (&[&nss_v, &qlit, &k], &[&nss_v, &qt, &k]);
        let v = self.q(&vec_sql, vp);
        let f = async {
            if qt.is_empty() {
                Ok(vec![])
            } else {
                self.q(&fts_sql, fp).await
            }
        };
        tokio::join!(v, f)
    }

    async fn recall_core(&self, namespaces: &[String], query: &str, opts: &RecallOpts, wide: bool) -> Result<RecallOut, String> {
        let t0 = Instant::now();
        let primary = namespaces.first().ok_or("namespace required")?.clone();
        let s = self.schema.clone();
        let intent = temporal::analyze(query, Utc::now());
        let historical = intent.temporal && !intent.current;
        let valid = if historical { "" } else { "AND s.valid_to IS NULL" };
        let qi = query_intent(query);
        let verbatim = qi == "verbatim";
        let err_q = is_error_query(query);
        let qlit = self.embed_query_lit(query).await?;
        let qt = fts_clamp(query);
        let k = opts.k.max(1) as i64;
        let ents: Vec<String> = temporal::query_entities(query).iter().map(|e| e.to_lowercase()).collect();
        let pin_cap = opts.constraint_cap.unwrap_or(500);
        let mut pin_nss: Vec<String> = if wide {
            vec![primary.clone()]
        } else {
            let mut v = namespaces.to_vec();
            for g in &opts.grounded_in {
                if !v.contains(g) {
                    v.push(g.clone());
                }
            }
            for a in &opts.ancestors {
                if !v.contains(a) {
                    v.push(a.clone());
                }
            }
            v
        };
        if pin_cap == 0 {
            pin_nss.clear();
        }
        let mut nss: Vec<String> = namespaces.to_vec();
        if !wide {
            for g in &opts.grounded_in {
                if !nss.contains(g) {
                    nss.push(g.clone());
                }
            }
        }
        let primary_opt = if wide { None } else { Some(primary.as_str()) };

        let ent_arm = async {
            if ents.is_empty() {
                return Ok(vec![]);
            }
            let sql = format!(
                "WITH RECURSIVE seeds AS (SELECT id FROM {s}.entities WHERE namespace = ANY($1) AND lower(name) = ANY($2)), \
                 reach(id, hop) AS (SELECT id, 0 FROM seeds UNION \
                   SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1 \
                   FROM reach r JOIN {s}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id) WHERE r.hop < 2) \
                 SELECT {SEM_COLS} FROM {s}.semantic s WHERE s.namespace = ANY($1) AND s.expired_at IS NULL {valid} \
                 AND s.id IN (SELECT m.memory_id FROM {s}.mentions m WHERE m.memory_kind='semantic' \
                              AND m.entity_id IN (SELECT id FROM reach)) \
                 ORDER BY s.valid_from DESC NULLS LAST LIMIT 20"
            );
            self.q(&sql, &[&nss, &ents]).await
        };
        let tl_arm = async {
            let (Some(a), Some(b)) = (intent.start, intent.end) else { return Ok(vec![]) };
            let dir = if intent.order == Some("desc") { "DESC" } else { "ASC" };
            let sql = format!(
                "SELECT {SEM_COLS} FROM {s}.semantic s WHERE s.namespace = ANY($1) AND s.expired_at IS NULL \
                 AND s.valid_from >= $2 AND s.valid_from <= $3 ORDER BY s.valid_from {dir} LIMIT 12"
            );
            self.q(&sql, &[&nss, &a, &b]).await
        };
        let epi_extra = String::new();
        let ((pins_res, sem), (raw, epi), ent, tl) = tokio::join!(
            async {
                let p = self.pinned_constraints(&pin_nss, pin_cap, &opts.overrides).await;
                let sem = self
                    .ann_fts("semantic", "s", SEM_COLS, valid, &nss, &qlit, &qt, k)
                    .await;
                (p, sem)
            },
            async {
                let raw = self.ann_fts("raw_turns", "t", RAW_COLS, "", &nss, &qlit, &qt, k).await;
                let epi = self.ann_fts("episodic", "e", EPI_COLS, &epi_extra, &nss, &qlit, &qt, k).await;
                (raw, epi)
            },
            ent_arm,
            tl_arm
        );
        let mut reasons = vec![];
        let (pins, suppressed) = match pins_res {
            Ok(x) => x,
            Err(e) => {
                reasons.push(json!({"namespace": primary, "arm": "pinned_constraints", "error": e}));
                (vec![], vec![])
            }
        };
        let (sem_v, sem_f) = (
            degrade(sem.0, &primary, "semantic_vec", &mut reasons),
            degrade(sem.1, &primary, "semantic_fts", &mut reasons),
        );
        let (raw_v, raw_f) = (
            degrade(raw.0, &primary, "raw_vec", &mut reasons),
            degrade(raw.1, &primary, "raw_fts", &mut reasons),
        );
        let (epi_v, epi_f) = (
            degrade(epi.0, &primary, "episodic_vec", &mut reasons),
            degrade(epi.1, &primary, "episodic_fts", &mut reasons),
        );
        let (ent, tl) = (
            degrade(ent, &primary, "entity_graph", &mut reasons),
            degrade(tl, &primary, "temporal", &mut reasons),
        );
        let arm_counts = json!({
            "pinned": pins.len(), "semantic_vec": sem_v.len(), "semantic_fts": sem_f.len(), "raw_vec": raw_v.len(),
            "raw_fts": raw_f.len(), "episodic_vec": epi_v.len(), "episodic_fts": epi_f.len(),
            "entity_graph": ent.len(), "temporal": tl.len(),
        });
        let has_tl = !tl.is_empty();

        // --- facts + raw turns (RRF fusion + optional cross-encoder rerank) ------------------
        let mut past_deadline = opts.deadline.map_or(false, |dl| Instant::now() >= dl);
        let warming = crate::rerank::rerank_should_degrade_warming();
        let use_rerank = crate::rerank::rerank_enabled() && !warming && !past_deadline;

        let subject_scope = opts.subject.as_ref().map(|x| x.trim().to_lowercase()).filter(|x| !x.is_empty());
        let mut raw_fact_cands: Vec<(Value, f64)> = fuse_rows(vec![sem_v, sem_f, tl, ent], "fact")
            .into_iter()
            .filter(|(r, _)| match &subject_scope {
                None => true,
                Some(sl) => {
                    let se = s_of(r, "subject_entity").trim().to_lowercase();
                    sl == &se || se.contains(sl.as_str()) || s_of(r, "content").to_lowercase().contains(sl.as_str())
                }
            })
            .collect();
        if use_rerank && !raw_fact_cands.is_empty() {
            let q_owned = query.to_string();
            let texts: Vec<String> = raw_fact_cands.iter().map(|(r, _)| s_of(r, "content").to_string()).collect();
            if let Ok(Ok(rr_scores)) = tokio::task::spawn_blocking(move || {
                let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
                crate::rerank::rerank_candidates(&q_owned, &refs)
            })
            .await
            {
                for (orig_idx, sig) in rr_scores {
                    if let Some(entry) = raw_fact_cands.get_mut(orig_idx) {
                        entry.1 = sig;
                    }
                }
            }
        }
        let mut facts: Vec<Value> = raw_fact_cands
            .into_iter()
            .map(|(r, sc)| shape_row("fact", &r, adjust_score("fact", &r, sc, err_q, verbatim), primary_opt))
            .collect();
        facts.sort_by(|a, b| f_of(b, "score").partial_cmp(&f_of(a, "score")).unwrap());

        let mut dedup_turns: Vec<(Value, f64)> = vec![];
        let mut seen_idx: HashMap<String, usize> = HashMap::new();
        let mut scored: Vec<(Value, f64)> = fuse_rows(vec![raw_v, raw_f], "turn");
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        for (r, sc) in scored {
            let key = norm_content(s_of(&r, "content"));
            if let Some(&i) = seen_idx.get(&key) {
                let n = dedup_turns[i].0.get("dup_count").and_then(|x| x.as_i64()).unwrap_or(1) + 1;
                dedup_turns[i].0["dup_count"] = json!(n);
                continue;
            }
            seen_idx.insert(key, dedup_turns.len());
            dedup_turns.push((r, sc));
        }
        if let Some(dl) = opts.deadline {
            if Instant::now() >= dl {
                past_deadline = true;
            }
        }
        if use_rerank && !past_deadline && !dedup_turns.is_empty() {
            let q_owned = query.to_string();
            let texts: Vec<String> = dedup_turns.iter().map(|(r, _)| s_of(r, "content").to_string()).collect();
            if let Ok(Ok(rr_scores)) = tokio::task::spawn_blocking(move || {
                let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
                crate::rerank::rerank_candidates(&q_owned, &refs)
            })
            .await
            {
                for (orig_idx, sig) in rr_scores {
                    if let Some(entry) = dedup_turns.get_mut(orig_idx) {
                        entry.1 = sig;
                    }
                }
            }
        }
        let mut turns: Vec<Value> = dedup_turns
            .into_iter()
            .map(|(r, sc)| shape_row("turn", &r, adjust_score("turn", &r, sc, err_q, verbatim), primary_opt))
            .collect();
        turns.sort_by(|a, b| f_of(b, "score").partial_cmp(&f_of(a, "score")).unwrap());
        if let Some(dl) = opts.deadline {
            if Instant::now() >= dl {
                past_deadline = true;
            }
        }
        let turn_ids: Vec<i64> = turns.iter().map(|t| i_of(t, "id")).collect();
        if !turn_ids.is_empty() && !past_deadline {
            let sql = format!(
                "SELECT t.tid AS turn_id, max(s.valid_to) AS closed_at FROM unnest($1::bigint[]) AS t(tid) \
                 JOIN {s}.semantic s ON s.source_turn_ids @> ARRAY[t.tid] AND s.kind='fact' AND s.expired_at IS NULL \
                 GROUP BY t.tid HAVING bool_and(s.valid_to IS NOT NULL OR s.superseded_by IS NOT NULL)"
            );
            let stale = degrade(self.q(&sql, &[&turn_ids]).await, &primary, "stale_turns", &mut reasons);
            let stale: HashMap<i64, String> = stale
                .iter()
                .map(|r| (i_of(r, "turn_id"), utc_date_of(r, "closed_at").unwrap_or_default()))
                .collect();
            for t in turns.iter_mut() {
                if let Some(at) = stale.get(&i_of(t, "id")) {
                    t["superseded"] = json!(true);
                    if !at.is_empty() {
                        t["superseded_at"] = json!(at);
                    }
                }
            }
        }
        if let Some(dl) = opts.deadline {
            if Instant::now() >= dl {
                past_deadline = true;
            }
        }
        let is_stale = |t: &Value| t.get("superseded").and_then(|x| x.as_bool()).unwrap_or(false);
        let stale_turns: Vec<Value> = turns.iter().filter(|t| is_stale(t)).cloned().collect();
        turns.retain(|t| !is_stale(t));

        // --- episodes (summaries, capped) -----------------------------------------------------
        let mut episodes: Vec<Value> = fuse_rows(vec![epi_v, epi_f], "episode")
            .into_iter()
            .take(3)
            .map(|(r, sc)| shape_row("episode", &r, sc, primary_opt))
            .collect();

        // --- assembly -------------------------------------------------------------------------
        let broad = matches!(qi, "list" | "broad");
        let raw_q = if has_tl { 5 } else { opts.raw_quota };
        let fact_q = opts.fact_quota + if !ents.is_empty() { 10 } else { 0 } + if has_tl { 10 } else { 0 };
        turns.truncate(raw_q);
        facts.truncate(fact_q);
        episodes.truncate(3);
        let mut ranked: Vec<Value> = vec![];
        if broad {
            ranked.extend(facts);
            ranked.extend(turns);
        } else {
            ranked.extend(turns);
            ranked.extend(facts);
        }
        ranked.extend(episodes);
        ranked.extend(stale_turns);
        if let Some(ref af) = opts.author_filter {
            ranked.retain(|r| s_of(r, "author") == af.as_str());
        }
        if let Some(ref tf) = opts.type_filter {
            ranked.retain(|r| s_of(r, "type") == tf.as_str());
        }
        // A pinned constraint's own twin (fact/turn with identical text) must not render twice.
        let pinned_text: HashSet<&str> = pins.iter().map(|p| s_of(p, "content")).collect();
        ranked.retain(|r| !pinned_text.contains(s_of(r, "content")));
        if let Some(root) = &opts.repo_root {
            for r in ranked.iter_mut() {
                code_anchors::annotate_row_with_code_drift(r, Path::new(root));
            }
        }
        let pin_rows: Vec<Value> = pins
            .iter()
            .map(|p| {
                let mut o = Map::new();
                o.insert("id".into(), p["id"].clone());
                o.insert("kind".into(), p["kind"].clone());
                o.insert("content".into(), p["content"].clone());
                o.insert("type".into(), json!("constraint"));
                o.insert("pinned".into(), json!(true));
                if !s_of(p, "author").is_empty() {
                    o.insert("author".into(), p["author"].clone());
                }
                let p_ns = s_of(p, "namespace");
                if !p_ns.is_empty() && (wide || p_ns != primary.as_str()) {
                    o.insert("namespace".into(), p["namespace"].clone());
                }
                Value::Object(o)
            })
            .collect();

        let degraded = !reasons.is_empty() || past_deadline || warming;
        let mut explain = json!({
            "ts": Utc::now().to_rfc3339(), "query": query, "namespace": primary, "namespaces": namespaces,
            "intent": qi, "temporal": {"temporal": intent.temporal, "current": intent.current, "historical": historical},
            "error_query": err_q, "arms": arm_counts,
            "returned": ranked.iter().map(|r| json!({"kind": r["kind"], "id": r["id"], "score": r["score"]})).collect::<Vec<_>>(),
            "suppressed_constraints": suppressed, "latency_ms": t0.elapsed().as_millis() as u64,
        });
        if !reasons.is_empty() {
            explain["degraded_reasons"] = json!(reasons);
        }
        self.push_explain(explain);
        Ok(RecallOut { pins: pin_rows, ranked, reasons, degraded })
    }

    fn recall_json(&self, namespaces: &[String], query: &str, opts: &RecallOpts, out: RecallOut, t0: Instant, wide: bool) -> Value {
        let mut results: Vec<Value> = out.pins.clone();
        results.extend(out.ranked.iter().cloned());
        let context = render_context(&results, opts.max_chars, opts.viewer.as_deref(), Some(query));
        let mut v = json!({
            "query": query,
            "namespace": namespaces.first(),
            "context": context,
            "memories": results,
            "results": results,
            "count": results.len(),
            "pinned_constraints": out.pins,
            "latency_ms": t0.elapsed().as_millis() as u64,
        });
        if wide {
            v["namespaces"] = json!(namespaces);
            v["namespaces_searched"] = json!(namespaces);
        } else {
            if !opts.grounded_in.is_empty() || !opts.links_skipped.is_empty() {
                v["grounded_in"] = json!(opts.grounded_in);
                v["links_skipped"] = json!(opts.links_skipped);
            }
            if !opts.ancestors.is_empty() || !opts.inherited_skipped.is_empty() {
                v["inherited_in"] = json!(opts.ancestors);
                v["inheritance_skipped"] = json!(opts.inherited_skipped);
            }
        }
        if out.degraded {
            v["degraded"] = json!(true);
        }
        if !out.reasons.is_empty() {
            v["degraded_reasons"] = json!(out.reasons);
        }
        v
    }

    /// `/recall`: pinned constraints + RRF over semantic facts, raw turns, entity graph, temporal
    /// and episodic arms.
    pub async fn recall(&self, namespace: &str, query: &str, opts: &RecallOpts) -> Result<Value, String> {
        let t0 = Instant::now();
        let ns = vec![namespace.to_string()];
        let out = self.recall_core(&ns, query, opts, false).await?;
        Ok(self.recall_json(&ns, query, opts, out, t0, false))
    }

    /// Recall across several namespaces; each row carries its source `namespace`.
    pub async fn recall_wide(&self, namespaces: &[String], query: &str, opts: &RecallOpts) -> Result<Value, String> {
        let t0 = Instant::now();
        let out = self.recall_core(namespaces, query, opts, true).await?;
        Ok(self.recall_json(namespaces, query, opts, out, t0, true))
    }

    /// `/memory/search`: ranked memories only (no pinned constraints, no rendered context).
    pub async fn memory_search(&self, namespace: &str, query: &str, opts: &RecallOpts) -> Result<Value, String> {
        let t0 = Instant::now();
        let ns = vec![namespace.to_string()];
        let out = self.recall_core(&ns, query, opts, false).await?;
        let mut v = json!({
            "query": query,
            "namespace": namespace,
            "count": out.ranked.len(),
            "memories": out.ranked,
            "results": out.ranked,
            "latency_ms": t0.elapsed().as_millis() as u64,
        });
        if !opts.grounded_in.is_empty() || !opts.links_skipped.is_empty() {
            v["grounded_in"] = json!(opts.grounded_in);
            v["links_skipped"] = json!(opts.links_skipped);
        }
        if out.degraded {
            v["degraded"] = json!(true);
        }
        if !out.reasons.is_empty() {
            v["degraded_reasons"] = json!(out.reasons);
        }
        Ok(v)
    }

    /// `/memory/context`: the paste-ready context block for a query.
    pub async fn memory_context(&self, namespace: &str, query: &str, opts: &RecallOpts) -> Result<Value, String> {
        let r = self.recall(namespace, query, opts).await?;
        Ok(json!({"query": query, "namespace": namespace, "context": r["context"], "count": r["count"],
                  "pinned_constraints": r["pinned_constraints"], "latency_ms": r["latency_ms"]}))
    }
}

// ---------------------------------------------------------------------------------------------
// Staleness & contradiction governance
// ---------------------------------------------------------------------------------------------

impl BrainStore {
    /// Check an external claim (a local note, an agent's belief) against memnos.
    ///
    /// `verdict`: `supported` (a current fact's value appears in the claim), `stale` (the claim
    /// states a value memnos has since superseded), `contradicted` (the claim conflicts with the
    /// current value and matches no history), or `unknown` (nothing relevant on record). The
    /// legacy keys (`claim`, `matches`, `conflicts`, `stale`) are kept: `stale` is true whenever a
    /// current fact's value is absent from the claim. `confidence` is a heuristic: higher when the
    /// subject/predicate matched exactly, lower when only embedding similarity found the fact.
    pub async fn reconcile_claim(
        &self,
        namespace: &str,
        statement: &str,
        subject: Option<&str>,
        predicate: Option<&str>,
    ) -> Result<Value, String> {
        let claim_l = statement.to_lowercase();
        let s = &self.schema;
        let subj = subject.map(str::trim).filter(|x| !x.is_empty());
        let pred = predicate.map(str::trim).filter(|x| !x.is_empty());
        let qlit = self.embed_query_lit(statement).await?;
        let cast = self.ensure_vt_cast(2);
        let cols = "id, statement, subject_entity, predicate, object, valid_from, valid_to";
        let mut live: Vec<Value> = vec![];
        let mut hist: Vec<Value> = vec![];
        let mut exact = false;
        if let Some(sub) = subj {
            let sql = |state: &str, order: &str| {
                format!(
                    "SELECT {cols} FROM {s}.semantic WHERE namespace=$1 AND expired_at IS NULL AND {state} \
                     AND lower(subject_entity)=lower($2) AND ($3::text IS NULL OR lower(predicate)=lower($3)) \
                     ORDER BY {order} DESC NULLS LAST LIMIT 20"
                )
            };
            live = self.q(&sql("valid_to IS NULL", "valid_from"), &[&namespace, &sub, &pred]).await?;
            hist = self.q(&sql("valid_to IS NOT NULL", "valid_to"), &[&namespace, &sub, &pred]).await?;
            exact = !live.is_empty() || !hist.is_empty();
        }
        // Vector arm: paraphrases, and the case where no subject was supplied. When a subject IS
        // supplied, never cross-match rows belonging to a different named subject_entity.
        let vsql = |state: &str| {
            format!(
                "SELECT {cols}, (embedding <=> {cast}) AS dist FROM {s}.semantic WHERE namespace=$1 AND expired_at IS NULL \
                 AND {state} AND embedding IS NOT NULL \
                 AND ($3::text IS NULL OR subject_entity IS NULL OR lower(subject_entity)=lower($3)) \
                 ORDER BY embedding <=> {cast} LIMIT 8"
            )
        };
        let mut vec_live = self.q(&vsql("valid_to IS NULL"), &[&namespace, &qlit, &subj]).await?;
        let mut vec_hist = self.q(&vsql("valid_to IS NOT NULL"), &[&namespace, &qlit, &subj]).await?;
        let near = |r: &Value| r.get("dist").and_then(|d| d.as_f64()).map_or(false, |d| d < 0.45);
        vec_live.retain(near);
        vec_hist.retain(near);

        let in_claim = |obj: &str| !obj.trim().is_empty() && claim_l.contains(&obj.trim().to_lowercase());
        let mut seen = HashSet::new();
        let mut matches: Vec<Value> = vec![];
        let (mut supported, mut conflicts): (bool, Vec<Value>) = (false, vec![]);
        for (rows, status, via_vec) in [(&live, "live", false), (&vec_live, "live", true), (&hist, "superseded", false), (&vec_hist, "superseded", true)] {
            for r in rows {
                if !seen.insert(i_of(r, "id")) {
                    continue;
                }
                let obj = s_of(r, "object");
                let differs = norm_content(s_of(r, "statement")) != norm_content(statement);
                let conflict = status == "live" && !obj.trim().is_empty() && !in_claim(obj) && (!via_vec || differs);
                if status == "live" && (in_claim(obj) || (obj.is_empty() && !differs)) {
                    supported = true;
                }
                let m = json!({
                    "id": r["id"], "statement": r["statement"], "subject": r["subject_entity"], "predicate": r["predicate"],
                    "object": r["object"], "valid_from": r["valid_from"], "valid_to": r["valid_to"],
                    "status": status, "conflict": conflict,
                });
                if conflict {
                    conflicts.push(m.clone());
                }
                matches.push(m);
            }
        }
        let superseded_hit = matches.iter().find(|m| s_of(m, "status") == "superseded" && in_claim(s_of(m, "object")));
        let any_live = matches.iter().any(|m| s_of(m, "status") == "live");
        let verdict = if supported {
            "supported"
        } else if !conflicts.is_empty() {
            if superseded_hit.is_some() { "stale" } else { "contradicted" }
        } else if !any_live && superseded_hit.is_some() {
            "stale"
        } else {
            "unknown"
        };
        let confidence = match (verdict, exact) {
            ("unknown", _) => 0.0,
            ("supported", true) => 0.9,
            ("supported", false) => 0.7,
            ("stale", _) => 0.85,
            (_, true) => 0.8,
            (_, false) => 0.6,
        };
        let current = if verdict == "supported" {
            matches.iter().find(|m| s_of(m, "status") == "live" && (in_claim(s_of(m, "object")) || s_of(m, "object").is_empty()))
        } else {
            conflicts.first().or_else(|| matches.iter().find(|m| s_of(m, "status") == "live"))
        };
        let val_of = |m: &Value| {
            let o = s_of(m, "object");
            if o.is_empty() { s_of(m, "statement").to_string() } else { o.to_string() }
        };
        let sup_val = superseded_hit
            .or_else(|| matches.iter().find(|m| s_of(m, "status") == "superseded"))
            .filter(|_| matches!(verdict, "stale" | "contradicted"))
            .map(val_of);
        let cur_date = current.and_then(|m| utc_date_of(m, "valid_from"));
        Ok(json!({
            "claim": statement, "verdict": verdict, "stale": !conflicts.is_empty(),
            "current_value": current.map(val_of), "current_date": cur_date, "superseded_value": sup_val,
            "confidence": confidence, "matches": matches, "conflicts": conflicts,
        }))
    }

    /// Live facts where the same subject plus a SINGLE-VALUED predicate carries more than one
    /// distinct object. Additive relations (visited, likes, ...) are not contradictions.
    pub async fn contradiction_summary(&self, namespace: &str, limit: usize) -> Result<Value, String> {
        let rows = self
            .q(
                &format!(
                    "SELECT subject_entity AS subject, predicate, array_agg(DISTINCT object) AS objects, array_agg(id ORDER BY id) AS ids \
                     FROM {}.semantic WHERE namespace=$1 AND expired_at IS NULL AND valid_to IS NULL \
                     AND subject_entity IS NOT NULL AND predicate IS NOT NULL AND object IS NOT NULL \
                     GROUP BY subject_entity, predicate HAVING count(DISTINCT object) > 1 \
                     ORDER BY count(DISTINCT object) DESC, subject_entity, predicate",
                    self.schema
                ),
                &[&namespace],
            )
            .await?;
        let groups: Vec<Value> = rows
            .into_iter()
            .filter(|g| {
                let pred = s_of(g, "predicate");
                g["objects"].as_array().map_or(false, |o| o.iter().any(|x| supersedable(pred, x.as_str())))
            })
            .collect();
        let contested: usize = groups.iter().map(|g| g["ids"].as_array().map_or(0, |a| a.len())).sum();
        Ok(json!({"total_groups": groups.len(), "contested_facts": contested,
                  "groups": groups.iter().take(limit).cloned().collect::<Vec<_>>()}))
    }

    /// `/contradictions`: sample groups (capped) plus the uncapped namespace-wide totals.
    pub async fn check_contradictions(&self, namespace: &str) -> Result<Value, String> {
        let cs = self.contradiction_summary(namespace, 50).await?;
        Ok(json!({"namespace": namespace, "contradictions": cs["groups"], "total_groups": cs["total_groups"],
                  "contested_facts": cs["contested_facts"]}))
    }

    /// 0-100 knowledge-health score from contradictions (share of live facts) and orphan
    /// entities. Each signal degrades to `null` on its own failure, and a score is only reported
    /// when every signal it depends on succeeded, so a failed read never looks like a clean bill.
    pub async fn knowledge_health(&self, namespace: &str) -> Result<Value, String> {
        let s = &self.schema;
        let mut reasons: Vec<Value> = vec![];
        let mut one = |name: &'static str, r: Result<Vec<Value>, String>| -> Option<i64> {
            match r {
                Ok(rows) => rows.first().map(|x| i_of(x, "n")),
                Err(e) => {
                    reasons.push(json!({"arm": name, "error": e}));
                    None
                }
            }
        };
        let ns = namespace;
        let facts_current = one("facts_current", self.q(&format!("SELECT count(*)::bigint n FROM {s}.semantic WHERE namespace=$1 AND valid_to IS NULL AND expired_at IS NULL"), &[&ns]).await);
        let facts_super = one("facts_superseded", self.q(&format!("SELECT count(*)::bigint n FROM {s}.semantic WHERE namespace=$1 AND valid_to IS NOT NULL"), &[&ns]).await);
        let facts_expired = one("facts_expired", self.q(&format!("SELECT count(*)::bigint n FROM {s}.semantic WHERE namespace=$1 AND expired_at IS NOT NULL"), &[&ns]).await);
        let ent_total = one("entities", self.q(&format!("SELECT count(*)::bigint n FROM {s}.entities WHERE namespace=$1"), &[&ns]).await);
        // Uncorrelated UNION arms keep this an index-only scan; a correlated OR over both edge
        // columns is O(entities x edges).
        let orphans = one("orphan_entities", self.q(&format!(
            "SELECT count(*)::bigint n FROM {s}.entities e WHERE e.namespace=$1 AND e.id NOT IN (\
             SELECT src_entity FROM {s}.edges WHERE namespace=$1 UNION SELECT dst_entity FROM {s}.edges WHERE namespace=$1)"), &[&ns]).await);
        let contra = match self.contradiction_summary(namespace, 0).await {
            Ok(v) => Some((v["total_groups"].as_i64().unwrap_or(0), v["contested_facts"].as_i64().unwrap_or(0))),
            Err(e) => {
                reasons.push(json!({"arm": "contradictions", "error": e}));
                None
            }
        };
        let ent_embedded = self.q(&format!("SELECT count(*)::bigint n FROM {s}.entities WHERE namespace=$1 AND embedding IS NOT NULL"), &[&ns]).await.ok().and_then(|r| r.first().map(|x| i_of(x, "n")));
        let literal_names = self.q(&format!("SELECT name FROM {s}.entities WHERE namespace=$1"), &[&ns]).await.ok()
            .map(|r| r.iter().filter(|x| !is_valid_entity_name(s_of(x, "name"), true)).count() as i64);
        let degree1 = self.q(&format!(
            "SELECT count(*)::bigint n FROM (SELECT nid FROM (SELECT src_entity AS nid FROM {s}.edges WHERE namespace=$1 \
             UNION ALL SELECT dst_entity FROM {s}.edges WHERE namespace=$1) d GROUP BY nid HAVING count(*)=1) q"), &[&ns]).await.ok()
            .and_then(|r| r.first().map(|x| i_of(x, "n")));

        let share = |n: Option<i64>, d: Option<i64>| -> Option<f64> {
            match (n, d) {
                (Some(n), Some(d)) => Some(if d > 0 { ((n as f64 / d as f64) * 1e6).round() / 1e6 } else { 0.0 }),
                _ => None,
            }
        };
        let contra_ratio = match (contra, facts_current) {
            (Some((_, contested)), Some(fc)) => Some(if fc > 0 { contested as f64 / fc as f64 } else { 0.0 }),
            _ => None,
        };
        let score = match (contra_ratio, orphans, ent_total) {
            (Some(cr), Some(o), Some(et)) => {
                let orphan_ratio = if et > 0 { o as f64 / et as f64 } else { 0.0 };
                Some((100 - contradiction_penalty(cr) - (orphan_ratio * 30.0).min(30.0) as i64).max(0))
            }
            _ => None,
        };
        let mut out = json!({
            "score": score, "facts_current": facts_current, "facts_superseded": facts_super, "facts_expired": facts_expired,
            "entities": ent_total, "orphan_entities": orphans,
            "contradiction_groups": contra.map(|c| c.0), "contested_facts": contra.map(|c| c.1),
            "contradiction_ratio": contra_ratio.map(|r| (r * 1e6).round() / 1e6),
            "degree_1_share": share(degree1, ent_total), "literal_name_share": share(literal_names, ent_total),
            "embedding_coverage": share(ent_embedded, ent_total),
        });
        if !reasons.is_empty() {
            out["degraded"] = json!(true);
            out["degraded_reasons"] = json!(reasons);
        }
        Ok(out)
    }

    /// Record whether a recall was helpful. Logged to the control-plane feedback table (best
    /// effort; the table belongs to the control module) and applied as a bounded salience nudge
    /// (+/-0.05, clamped to 0..1) on the cited facts, which feeds rank-time reinforcement.
    pub async fn record_feedback(
        &self,
        namespace: &str,
        query: &str,
        helpful: bool,
        turn_ids: &[i64],
        fact_ids: &[i64],
        note: Option<&str>,
    ) -> Result<Value, String> {
        let c = self.conn().await?;
        let note_full = match (note, turn_ids.is_empty()) {
            (Some(n), true) => Some(n.to_string()),
            (n, false) => Some(format!("{} [turns:{:?}]", n.unwrap_or(""), turn_ids).trim().to_string()),
            (None, true) => None,
        };
        let q: String = query.chars().take(1000).collect();
        let logged = c
            .execute(
                "INSERT INTO memnos_control.feedback(namespace, query, helpful, note) VALUES($1,$2,$3,$4)",
                &[&namespace, &q, &helpful, &note_full],
            )
            .await
            .is_ok();
        let delta: f32 = if helpful { 0.05 } else { -0.05 };
        let ids: Vec<i64> = fact_ids.to_vec();
        let adjusted = if ids.is_empty() {
            0
        } else {
            c.execute(
                format!(
                    "UPDATE {}.semantic SET salience = LEAST(1.0, GREATEST(0.0, salience + $1)) WHERE namespace=$2 AND id = ANY($3)",
                    self.schema
                )
                .as_str(),
                &[&delta, &namespace, &ids],
            )
            .await
            .map_err(de)?
        };
        Ok(json!({"ok": true, "helpful": helpful, "logged": logged, "facts_adjusted": adjusted}))
    }
}

// ---------------------------------------------------------------------------------------------
// Entity graph, provenance, dossiers
// ---------------------------------------------------------------------------------------------

fn community_name_ok(name: &str) -> bool {
    name.chars().filter(|c| c.is_alphanumeric()).count() >= 2
}

impl BrainStore {
    /// Entity + its `depth`-hop neighbourhood + the live facts that mention it.
    pub async fn get_entity(&self, namespace: &str, name: &str, depth: i32, fact_limit: i64) -> Result<Option<Value>, String> {
        let s = &self.schema;
        let ent = self.q(&format!("SELECT id, name FROM {s}.entities WHERE namespace=$1 AND lower(name)=lower($2)"), &[&namespace, &name]).await?;
        let Some(e) = ent.first() else { return Ok(None) };
        let eid = i_of(e, "id");
        let depth = depth.clamp(1, 3);
        let related = self
            .q(
                &format!(
                    "WITH RECURSIVE reach(id, hop) AS (SELECT $1::bigint, 0 UNION \
                       SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1 \
                       FROM reach r JOIN {s}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id) WHERE r.hop < $2) \
                     SELECT e.name, max(g.weight)::float8 AS weight FROM reach r \
                     JOIN {s}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id) \
                     JOIN {s}.entities e ON e.id = CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END \
                     WHERE e.id <> $1 AND e.namespace=$3 GROUP BY e.name ORDER BY weight DESC LIMIT 50"
                ),
                &[&eid, &depth, &namespace],
            )
            .await?;
        let facts = self
            .q(
                &format!(
                    "SELECT DISTINCT s.id, s.statement AS content, s.valid_from, s.valid_to FROM {s}.mentions m \
                     JOIN {s}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic' \
                     WHERE m.entity_id=$1 AND s.namespace=$2 AND s.expired_at IS NULL \
                     ORDER BY s.valid_from DESC NULLS LAST LIMIT $3"
                ),
                &[&eid, &namespace, &fact_limit.clamp(1, 200)],
            )
            .await?;
        Ok(Some(json!({"entity": {"id": eid, "name": e["name"]}, "related": related, "facts": facts})))
    }

    /// Direct neighbours of an entity over `edges`, weight-ranked.
    pub async fn get_related(&self, namespace: &str, name: &str, limit: i64) -> Result<Value, String> {
        let s = &self.schema;
        let rows = self
            .q(
                &format!(
                    "SELECT e2.name, g.weight::float8 AS weight FROM {s}.entities e1 \
                     JOIN {s}.edges g ON (g.src_entity=e1.id OR g.dst_entity=e1.id) \
                     JOIN {s}.entities e2 ON e2.id = CASE WHEN g.src_entity=e1.id THEN g.dst_entity ELSE g.src_entity END \
                     WHERE e1.namespace=$1 AND lower(e1.name)=lower($2) AND e2.id<>e1.id ORDER BY g.weight DESC LIMIT $3"
                ),
                &[&namespace, &name, &limit.clamp(1, 500)],
            )
            .await?;
        Ok(json!({"name": name, "related": rows}))
    }

    /// N-hop breadth-first expansion (recursive CTE, no graph DB) from seed entities: the reachable
    /// entities with their hop distance, and the live facts they are mentioned in.
    pub async fn graph_query(&self, namespace: &str, entities: &[String], hops: i32, limit: i64) -> Result<Value, String> {
        let names: Vec<String> = entities.iter().map(|e| e.trim().to_lowercase()).filter(|e| !e.is_empty()).take(10).collect();
        if names.is_empty() {
            return Err("entities[] or name required".into());
        }
        let s = &self.schema;
        let hops = hops.clamp(1, 3);
        let reach = format!(
            "WITH RECURSIVE seeds AS (SELECT id FROM {s}.entities WHERE namespace=$1 AND lower(name) = ANY($2)), \
             reach(id, hop) AS (SELECT id, 0 FROM seeds UNION \
               SELECT CASE WHEN g.src_entity=r.id THEN g.dst_entity ELSE g.src_entity END, r.hop+1 \
               FROM reach r JOIN {s}.edges g ON (g.src_entity=r.id OR g.dst_entity=r.id) WHERE r.hop < $3)"
        );
        let nodes = self
            .q(&format!("{reach} SELECT e.name, min(r.hop) AS hop FROM reach r JOIN {s}.entities e ON e.id=r.id GROUP BY e.name ORDER BY min(r.hop), e.name LIMIT 200"), &[&namespace, &names, &hops])
            .await?;
        let facts = self
            .q(
                &format!(
                    "{reach} SELECT DISTINCT s.id, s.statement AS content, s.valid_from FROM {s}.mentions m \
                     JOIN {s}.semantic s ON s.id=m.memory_id AND m.memory_kind='semantic' \
                     WHERE m.entity_id IN (SELECT id FROM reach) AND s.namespace=$1 AND s.expired_at IS NULL AND s.valid_to IS NULL LIMIT $4"
                ),
                &[&namespace, &names, &hops, &limit.clamp(1, 200)],
            )
            .await?;
        Ok(json!({"entities": nodes, "facts": facts, "hops": hops}))
    }

    async fn community_neighbours(&self, c: &Client, ns: &str, ids: &[i64], seen: &HashSet<i64>, limit: i64) -> Result<Vec<Value>, String> {
        let s = &self.schema;
        let (ids_v, seen_v): (Vec<i64>, Vec<i64>) = (ids.to_vec(), seen.iter().copied().collect());
        jq(
            c,
            &format!(
                "SELECT e.id, e.name, max(g.weight)::float8 AS weight, max(g.id) AS recency FROM {s}.edges g \
                 JOIN {s}.entities e ON e.id = CASE WHEN g.src_entity = ANY($2) THEN g.dst_entity ELSE g.src_entity END \
                 WHERE g.namespace=$1 AND (g.src_entity = ANY($2) OR g.dst_entity = ANY($2)) \
                 AND e.id <> ALL($3) AND e.namespace=$1 GROUP BY e.id, e.name ORDER BY weight DESC, recency DESC LIMIT $4"
            ),
            &[&ns, &ids_v, &seen_v, &limit],
        )
        .await
    }

    /// Bounded, weight-ranked NEIGHBOURHOOD of an entity (not community detection): at most 2
    /// hops, hubs (degree > 100) are kept as members but never expanded through, junk names are
    /// dropped, and every statement is LIMITed.
    pub async fn community_search(&self, namespace: &str, name: &str, max_nodes: usize) -> Result<Option<Value>, String> {
        const HUB_DEGREE: i64 = 100;
        const MAX_FRONTIER: usize = 50;
        let max_nodes = max_nodes.clamp(1, 200);
        let c = self.conn().await?;
        let s = &self.schema;
        let seed = jq(&c, &format!("SELECT id, name FROM {s}.entities WHERE namespace=$1 AND lower(name)=lower($2)"), &[&namespace, &name]).await?;
        let Some(seed) = seed.first() else { return Ok(None) };
        let sid = i_of(seed, "id");
        let mut seen: HashSet<i64> = HashSet::from([sid]);
        let mut members: Vec<(i64, Value)> = vec![]; // (recency, member)
        let mut hop1: Vec<Value> = vec![];
        for r in self.community_neighbours(&c, namespace, &[sid], &seen, (max_nodes * 2 + 10) as i64).await? {
            seen.insert(i_of(&r, "id"));
            if community_name_ok(s_of(&r, "name")) {
                members.push((i_of(&r, "recency"), json!({"name": r["name"], "hop": 1, "weight": f_of(&r, "weight")})));
                hop1.push(r);
            }
        }
        members.truncate(max_nodes);
        let room = max_nodes - members.len();
        if room > 0 && !hop1.is_empty() {
            let cand: Vec<i64> = hop1.iter().take(MAX_FRONTIER * 2).map(|r| i_of(r, "id")).collect();
            let deg = jq(
                &c,
                &format!(
                    "SELECT nid AS id, count(*)::bigint AS deg FROM (SELECT src_entity AS nid FROM {s}.edges WHERE namespace=$1 AND src_entity = ANY($2) \
                     UNION ALL SELECT dst_entity FROM {s}.edges WHERE namespace=$1 AND dst_entity = ANY($2)) d GROUP BY nid"
                ),
                &[&namespace, &cand],
            )
            .await?;
            let deg: HashMap<i64, i64> = deg.iter().map(|r| (i_of(r, "id"), i_of(r, "deg"))).collect();
            let frontier: Vec<i64> = cand.into_iter().filter(|i| deg.get(i).copied().unwrap_or(0) <= HUB_DEGREE).take(MAX_FRONTIER).collect();
            if !frontier.is_empty() {
                for r in self.community_neighbours(&c, namespace, &frontier, &seen, (room * 2 + 10) as i64).await? {
                    if members.len() >= max_nodes {
                        break;
                    }
                    seen.insert(i_of(&r, "id"));
                    if community_name_ok(s_of(&r, "name")) {
                        members.push((i_of(&r, "recency"), json!({"name": r["name"], "hop": 2, "weight": f_of(&r, "weight")})));
                    }
                }
            }
        }
        members.sort_by(|a, b| {
            let (ha, hb) = (i_of(&a.1, "hop"), i_of(&b.1, "hop"));
            ha.cmp(&hb).then(f_of(&b.1, "weight").partial_cmp(&f_of(&a.1, "weight")).unwrap()).then(b.0.cmp(&a.0))
        });
        let members: Vec<Value> = members.into_iter().map(|m| m.1).collect();
        Ok(Some(json!({
            "entity": seed["name"], "community": members.iter().map(|m| m["name"].clone()).collect::<Vec<_>>(),
            "members": members, "size": members.len() + 1, "max_hops": 2, "limit": max_nodes,
        })))
    }

    /// UPSERT the stored dossier for an entity; returns the row id.
    pub async fn store_entity_dossier(&self, entity_id: i64, namespace: &str, text: &str, model: Option<&str>) -> Result<i64, String> {
        let c = self.conn().await?;
        let row = c
            .query_one(
                format!(
                    "INSERT INTO {s}.entity_dossiers(entity_id, namespace, dossier_text, model_used, generated_at) VALUES($1,$2,$3,$4,now()) \
                     ON CONFLICT (entity_id, namespace) DO UPDATE SET dossier_text=EXCLUDED.dossier_text, model_used=EXCLUDED.model_used, generated_at=now() \
                     RETURNING id",
                    s = self.schema
                )
                .as_str(),
                &[&entity_id, &namespace, &text, &model],
            )
            .await
            .map_err(de)?;
        Ok(row.get(0))
    }

    pub async fn get_entity_dossier(&self, namespace: &str, entity_name: &str) -> Result<Option<Value>, String> {
        let s = &self.schema;
        let rows = self
            .q(
                &format!(
                    "SELECT d.id, e.name, d.dossier_text, d.generated_at, d.model_used FROM {s}.entity_dossiers d \
                     JOIN {s}.entities e ON e.id = d.entity_id WHERE d.namespace=$1 AND lower(e.name)=lower($2) LIMIT 1"
                ),
                &[&namespace, &entity_name],
            )
            .await?;
        Ok(rows.into_iter().next())
    }

    /// Evidence chain for a fact: the fact, its supersession links, the verbatim turns it came
    /// from, and the episodes linked through `provenance`.
    pub async fn get_provenance(&self, namespace: &str, id: i64) -> Result<Option<Value>, String> {
        let s = &self.schema;
        let fact = self
            .q(
                &format!(
                    "SELECT id, kind, statement, subject_entity, predicate, object, valid_from, valid_to, created_at, expired_at, \
                     superseded_by, restatements, source_speaker, author_principal AS author, source_turn_ids \
                     FROM {s}.semantic WHERE id=$1 AND namespace=$2"
                ),
                &[&id, &namespace],
            )
            .await?;
        let Some(f) = fact.into_iter().next() else { return Ok(None) };
        let src: Vec<i64> = f["source_turn_ids"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
        let sources = if src.is_empty() {
            vec![]
        } else {
            self.q(&format!("SELECT id, speaker, text AS content, observed_at FROM {s}.raw_turns WHERE id = ANY($1) AND namespace=$2 ORDER BY id"), &[&src, &namespace]).await?
        };
        let episodes = self
            .q(&format!("SELECT e.id, COALESCE(e.summary, e.text) AS summary FROM {s}.provenance p JOIN {s}.episodic e ON e.id=p.episodic_id WHERE p.semantic_id=$1 AND e.namespace=$2 ORDER BY e.id"), &[&id, &namespace])
            .await?;
        Ok(Some(json!({
            "fact": {"id": f["id"], "kind": f["kind"], "statement": f["statement"], "subject": f["subject_entity"],
                     "predicate": f["predicate"], "object": f["object"], "valid_from": f["valid_from"], "valid_to": f["valid_to"],
                     "created_at": f["created_at"], "expired_at": f["expired_at"], "superseded_by": f["superseded_by"],
                     "restatements": f["restatements"], "source_speaker": f["source_speaker"], "author": f["author"]},
            "source_turn_ids": src, "sources": sources, "episodes": episodes,
        })))
    }
}

// ---------------------------------------------------------------------------------------------
// Episodes, consolidation, namespace copy
// ---------------------------------------------------------------------------------------------

/// First two sentences, at most 400 chars (extractive, no LLM).
fn extractive_summary(text: &str) -> String {
    let mut sents: Vec<&str> = vec![];
    let (mut start, bytes) = (0usize, text.as_bytes());
    for i in 0..bytes.len() {
        if matches!(bytes[i], b'.' | b'!' | b'?') && bytes.get(i + 1).map_or(false, |b| b.is_ascii_whitespace()) {
            sents.push(&text[start..=i]);
            start = i + 1;
            while start < bytes.len() && bytes[start].is_ascii_whitespace() {
                start += 1;
            }
        }
    }
    if start < text.len() {
        sents.push(&text[start..]);
    }
    let joined = sents.iter().take(2).copied().collect::<Vec<_>>().join(" ");
    let src = if joined.is_empty() { text } else { joined.as_str() };
    src.chars().take(400).collect()
}

impl BrainStore {
    async fn touch_episodes(&self, ids: &[i64]) -> Result<(), String> {
        if ids.is_empty() {
            return Ok(());
        }
        let v = ids.to_vec();
        let c = self.conn().await?;
        c.execute(
            format!("UPDATE {}.episodic SET last_access=now(), access_count=access_count+1 WHERE id = ANY($1)", self.schema).as_str(),
            &[&v],
        )
        .await
        .map_err(de)?;
        Ok(())
    }

    /// Segment raw turns not yet in an episode: boundary on a session change or a time gap above
    /// `gap_minutes`, extractive summary, two-level provenance (fact -> episode -> turn).
    /// Incremental and idempotent. An episode inherits a `memory_type` only when all its turns
    /// share one non-null type.
    pub async fn segment_episodes(&self, namespace: &str, gap_minutes: i64, max_episodes: usize) -> Result<Value, String> {
        let s = &self.schema;
        let turns = self
            .q(
                &format!(
                    "SELECT id, speaker, text, observed_at, session_id, memory_type FROM {s}.raw_turns WHERE namespace=$1 \
                     AND id NOT IN (SELECT unnest(source_turn_ids) FROM {s}.episodic WHERE namespace=$1 AND source_turn_ids IS NOT NULL) \
                     AND coalesce(speaker,'') NOT LIKE 'memnos:%' ORDER BY observed_at, id"
                ),
                &[&namespace],
            )
            .await?;
        if turns.is_empty() {
            return Ok(json!({"episodes": 0}));
        }
        let mut groups: Vec<Vec<&Value>> = vec![];
        let mut cur: Vec<&Value> = vec![];
        for t in &turns {
            if let Some(prev) = cur.last() {
                let gap = match (parse_ts(t, "observed_at"), parse_ts(prev, "observed_at")) {
                    (Some(a), Some(b)) => (a - b).num_seconds() as f64 / 60.0,
                    _ => 0.0,
                };
                if t.get("session_id") != prev.get("session_id") || gap > gap_minutes as f64 {
                    groups.push(std::mem::take(&mut cur));
                }
            }
            cur.push(t);
        }
        if !cur.is_empty() {
            groups.push(cur);
        }
        // Summaries and embeddings first (network in OpenAI mode), no connection held.
        struct Prep { ids: Vec<i64>, session: Option<String>, body: String, summary: String, vec: String, mtype: Option<String>, t0: Option<DateTime<Utc>>, t1: Option<DateTime<Utc>> }
        let mut prepared = vec![];
        for g in groups.iter().take(max_episodes.max(1)) {
            let body = g.iter().map(|r| format!("{}: {}", if s_of(r, "speaker").is_empty() { "user" } else { s_of(r, "speaker") }, s_of(r, "text"))).collect::<Vec<_>>().join("\n");
            let joined = g.iter().map(|r| s_of(r, "text")).collect::<Vec<_>>().join(" ");
            let summary = extractive_summary(&joined);
            let vec = self.embed_lit(&summary).await?;
            let types: HashSet<&str> = g.iter().map(|r| s_of(r, "memory_type")).collect();
            let mtype = if types.len() == 1 { types.iter().next().filter(|t| !t.is_empty()).map(|t| t.to_string()) } else { None };
            prepared.push(Prep {
                ids: g.iter().map(|r| i_of(r, "id")).collect(),
                session: g[0].get("session_id").and_then(|x| x.as_str()).map(String::from),
                body, summary, vec, mtype,
                t0: parse_ts(g[0], "observed_at"), t1: parse_ts(g[g.len() - 1], "observed_at"),
            });
        }
        let c = self.conn().await?;
        let ins = format!(
            "INSERT INTO {s}.episodic(namespace,session_id,text,summary,t_start,t_end,observed_at,salience,source_turn_ids,embedding,memory_type) \
             VALUES($1,$2,$3,$4,$5,$6,COALESCE($6,now()),$7,$8,{},$10) RETURNING id",
            self.ensure_vt_cast(9)
        );
        let link = format!("INSERT INTO {s}.provenance(semantic_id, episodic_id) SELECT id, $2 FROM {s}.semantic WHERE namespace=$1 AND source_turn_ids && $3::bigint[] ON CONFLICT DO NOTHING");
        let mut n = 0;
        for p in &prepared {
            let sal = (0.3 + 0.1 * p.ids.len() as f32).min(1.0);
            let row = c.query_one(ins.as_str(), &[&namespace, &p.session, &p.body, &p.summary, &p.t0, &p.t1, &sal, &p.ids, &p.vec, &p.mtype]).await.map_err(de)?;
            let eid: i64 = row.get(0);
            c.execute(link.as_str(), &[&namespace, &eid, &p.ids]).await.map_err(de)?;
            n += 1;
        }
        Ok(json!({"episodes": n}))
    }

    /// Hybrid (vector + FTS, RRF) search over episodes; touches the hits so decay sees the access.
    pub async fn recall_episodes(&self, namespace: &str, query: &str, k: i64) -> Result<Value, String> {
        let qlit = self.embed_query_lit(query).await?;
        let cols = "e.id, e.summary, e.text, e.t_start, e.t_end, e.observed_at, e.salience, e.memory_type";
        let (v, f) = self.ann_fts("episodic", "e", cols, "", &[namespace.to_string()], &qlit, &fts_clamp(query), k.clamp(1, 50)).await;
        let rows: Vec<Value> = fuse_rows(vec![v?, f?], "episode")
            .into_iter()
            .map(|(mut r, sc)| {
                r["score"] = json!(sc);
                r
            })
            .collect();
        let ids: Vec<i64> = rows.iter().map(|r| i_of(r, "id")).collect();
        self.touch_episodes(&ids).await?;
        Ok(json!({"episodes": rows}))
    }

    pub async fn get_episode(&self, namespace: &str, id: i64) -> Result<Option<Value>, String> {
        let s = &self.schema;
        let ep = self
            .q(&format!("SELECT id, session_id, text, summary, t_start, t_end, observed_at, salience, access_count, consolidated, source_turn_ids, memory_type FROM {s}.episodic WHERE id=$1 AND namespace=$2"), &[&id, &namespace])
            .await?;
        let Some(mut e) = ep.into_iter().next() else { return Ok(None) };
        let src: Vec<i64> = e["source_turn_ids"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
        e["source_turn_ids"] = json!(src);
        let turns = if src.is_empty() {
            vec![]
        } else {
            self.q(
                &format!("SELECT id, speaker, text AS content, observed_at FROM {s}.raw_turns WHERE id = ANY($1) AND namespace=$2 ORDER BY observed_at, id"),
                &[&src, &namespace],
            )
            .await?
        };
        let facts = self
            .q(
                &format!("SELECT f.id, f.statement FROM {s}.provenance p JOIN {s}.semantic f ON f.id=p.semantic_id WHERE p.episodic_id=$1 AND f.namespace=$2 ORDER BY f.id"),
                &[&id, &namespace],
            )
            .await?;
        self.touch_episodes(&[id]).await?;
        e["access_count"] = json!(i_of(&e, "access_count") + 1);
        Ok(Some(json!({
            "episode": e,
            "turns": turns,
            "facts": facts,
        })))
    }

    /// Recompute episodic salience as half-life recency plus an access boost; facts are untouched.
    pub async fn decay_episodes(&self, namespace: &str, half_life_days: f64) -> Result<Value, String> {
        let hl = half_life_days.max(0.1);
        let rows = self
            .q(
                &format!(
                    "UPDATE {s}.episodic SET salience = LEAST(1.0, exp(-ln(2.0) * (EXTRACT(EPOCH FROM (now() - COALESCE(last_access, observed_at))) / 86400.0) / $1::float8) \
                     + 0.05 * LEAST(access_count, 10)) WHERE namespace=$2 RETURNING id",
                    s = self.schema
                ),
                &[&hl, &namespace],
            )
            .await?;
        Ok(json!({"updated": rows.len()}))
    }

    /// Delete entity nodes whose names are literals or clause fragments (and their mentions, edges
    /// and dossiers), then embed up to 200 valid entities that lack an embedding. `dry_run` only
    /// counts.
    pub async fn cleanup_entity_graph(&self, namespace: &str, dry_run: bool) -> Result<Value, String> {
        let s = &self.schema;
        let ents = self.q(&format!("SELECT id, name, embedding IS NOT NULL AS has_emb FROM {s}.entities WHERE namespace=$1"), &[&namespace]).await?;
        let bad: Vec<i64> = ents.iter().filter(|e| !is_valid_entity_name(s_of(e, "name"), true)).map(|e| i_of(e, "id")).collect();
        let sample: Vec<&str> = ents.iter().filter(|e| bad.contains(&i_of(e, "id"))).take(10).map(|e| s_of(e, "name")).collect();
        let (mut removed, mut embedded) = (0u64, 0u64);
        if !dry_run {
            if !bad.is_empty() {
                let c = self.conn().await?;
                c.execute(format!("DELETE FROM {s}.mentions WHERE entity_id = ANY($1)").as_str(), &[&bad]).await.map_err(de)?;
                c.execute(format!("DELETE FROM {s}.edges WHERE namespace=$1 AND (src_entity = ANY($2) OR dst_entity = ANY($2))").as_str(), &[&namespace, &bad]).await.map_err(de)?;
                c.execute(format!("DELETE FROM {s}.entity_dossiers WHERE namespace=$1 AND entity_id = ANY($2)").as_str(), &[&namespace, &bad]).await.map_err(de)?;
                removed = c.execute(format!("DELETE FROM {s}.entities WHERE namespace=$1 AND id = ANY($2)").as_str(), &[&namespace, &bad]).await.map_err(de)?;
            }
            let missing: Vec<&Value> = ents.iter().filter(|e| !e["has_emb"].as_bool().unwrap_or(true) && !bad.contains(&i_of(e, "id"))).take(200).collect();
            for e in missing {
                let lit = self.embed_lit(s_of(e, "name")).await?;
                let id = i_of(e, "id");
                let c = self.conn().await?;
                c.execute(format!("UPDATE {s}.entities SET embedding={} WHERE id=$2", self.ensure_vt_cast(1)).as_str(), &[&lit, &id]).await.map_err(de)?;
                embedded += 1;
            }
        }
        Ok(json!({"namespace": namespace, "dry_run": dry_run, "entities": ents.len(), "invalid_entities": bad.len(),
                  "sample": sample, "removed": removed, "embedded": embedded}))
    }

    /// Run the entity-graph cleanup over every namespace that has entities.
    pub async fn cleanup_all_entity_graphs(&self, dry_run: bool) -> Result<Value, String> {
        let nss = self.q(&format!("SELECT DISTINCT namespace FROM {}.entities ORDER BY 1", self.schema), &[]).await?;
        let (mut out, mut removed) = (vec![], 0u64);
        for n in nss {
            let r = self.cleanup_entity_graph(s_of(&n, "namespace"), dry_run).await?;
            removed += r["removed"].as_u64().unwrap_or(0);
            out.push(r);
        }
        Ok(json!({"dry_run": dry_run, "namespaces": out, "removed": removed}))
    }

    /// Deterministic sleep cycle (no LLM): (1) merge near-duplicate live facts among the 500 newest
    /// (same subject and object, distance < 0.03): the oldest survives with the duplicate's
    /// provenance, the newer is expired, never deleted; (2) drop junk entity-graph nodes;
    /// (3) write an extractive dossier for entities mentioned by >= 3 live facts (top 25).
    pub async fn consolidate(&self, namespace: &str) -> Result<Value, String> {
        let s = &self.schema;
        let dupes = self
            .q(
                &format!(
                    "WITH recent AS (SELECT id, subject_entity, object, embedding, source_turn_ids FROM {s}.semantic WHERE namespace=$1 AND kind='fact' \
                       AND valid_to IS NULL AND expired_at IS NULL AND embedding IS NOT NULL ORDER BY id DESC LIMIT 500) \
                     SELECT a.id AS keep, b.id AS dup, b.source_turn_ids FROM recent a JOIN recent b ON a.id < b.id \
                     AND lower(coalesce(a.subject_entity,''))=lower(coalesce(b.subject_entity,'')) \
                     AND lower(coalesce(a.object,''))=lower(coalesce(b.object,'')) AND (a.embedding <=> b.embedding) < 0.03"
                ),
                &[&namespace],
            )
            .await?;
        let mut merged = 0;
        let mut gone: HashSet<i64> = HashSet::new();
        let c = self.conn().await?;
        for d in &dupes {
            let (keep, dup) = (i_of(d, "keep"), i_of(d, "dup"));
            if gone.contains(&keep) || !gone.insert(dup) {
                continue;
            }
            let turns: Vec<i64> = d["source_turn_ids"].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
            self.bump_restatement(&c, keep, &turns).await?;
            c.execute(format!("UPDATE {s}.semantic SET expired_at=now(), valid_to=COALESCE(valid_to, now()), superseded_by=$1 WHERE id=$2").as_str(), &[&keep, &dup]).await.map_err(de)?;
            merged += 1;
        }
        drop(c);
        let cleanup = self.cleanup_entity_graph(namespace, false).await?;
        let cands = self
            .q(
                &format!(
                    "SELECT e.id, e.name FROM {s}.entities e JOIN {s}.mentions m ON m.entity_id=e.id AND m.memory_kind='semantic' \
                     JOIN {s}.semantic f ON f.id=m.memory_id AND f.valid_to IS NULL AND f.expired_at IS NULL \
                     WHERE e.namespace=$1 GROUP BY e.id, e.name HAVING count(*) >= 3 ORDER BY count(*) DESC LIMIT 25"
                ),
                &[&namespace],
            )
            .await?;
        let mut dossiers = 0;
        for e in &cands {
            let eid = i_of(e, "id");
            let facts = self
                .q(&format!("SELECT f.statement FROM {s}.mentions m JOIN {s}.semantic f ON f.id=m.memory_id AND m.memory_kind='semantic' WHERE m.entity_id=$1 AND f.valid_to IS NULL AND f.expired_at IS NULL ORDER BY f.valid_from DESC NULLS LAST, f.id DESC LIMIT 6"), &[&eid])
                .await?;
            let text = format!("{}: {}", s_of(e, "name"), facts.iter().map(|f| s_of(f, "statement")).collect::<Vec<_>>().join(" "));
            self.store_entity_dossier(eid, namespace, &text, Some("extractive")).await?;
            dossiers += 1;
        }
        Ok(json!({"namespace": namespace, "duplicates_merged": merged,
                  "entities_removed": cleanup["removed"], "entities_embedded": cleanup["embedded"], "dossiers": dossiers}))
    }

    /// Copy memories from `src_ns` into `dst_ns` (same tenant schema): raw turns and live facts
    /// (embeddings carried over, so nothing is re-embedded), restricted to `memory_types` when
    /// non-empty. Rows already present in the destination (same text and timestamp) are skipped,
    /// so a repeated copy does not duplicate. Retired constraints and expired facts are not copied.
    /// The destination entity graph is rebuilt from the copied facts' subject/predicate/object.
    pub async fn copy_memories_from(&self, src_ns: &str, dst_ns: &str, memory_types: &[String]) -> Result<Value, String> {
        if src_ns.trim().is_empty() || dst_ns.trim().is_empty() || src_ns == dst_ns {
            return Err("src and dst must be two different non-empty namespaces".into());
        }
        let s = &self.schema;
        let types: Vec<String> = memory_types.to_vec();
        let c = self.conn().await?;
        let rt = jq(
            &c,
            &format!(
                "INSERT INTO {s}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding,author_principal,memory_type,constraint_subject,code_anchors) \
                 SELECT $2,session_id,speaker,text,observed_at,embedding,author_principal,memory_type,constraint_subject,code_anchors FROM {s}.raw_turns r \
                 WHERE r.namespace=$1 AND (cardinality($3::text[])=0 OR r.memory_type = ANY($3)) AND r.constraint_retired_at IS NULL \
                 AND NOT EXISTS (SELECT 1 FROM {s}.raw_turns d WHERE d.namespace=$2 AND d.text=r.text AND d.observed_at=r.observed_at) RETURNING id"
            ),
            &[&src_ns, &dst_ns, &types],
        )
        .await?;
        // Copied facts lose source_turn_ids: the turn ids differ in the destination.
        let facts = jq(
            &c,
            &format!(
                "INSERT INTO {s}.semantic(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,observed_at,confidence,salience,embedding,\
                 source_speaker,author_principal,memory_type,constraint_subject,constraint_since,constraint_until,code_anchors) \
                 SELECT $2,kind,statement,subject_entity,predicate,object,valid_from,valid_to,observed_at,confidence,salience,embedding,\
                 source_speaker,author_principal,memory_type,constraint_subject,constraint_since,constraint_until,code_anchors FROM {s}.semantic f \
                 WHERE f.namespace=$1 AND f.expired_at IS NULL AND f.constraint_retired_at IS NULL AND (cardinality($3::text[])=0 OR f.memory_type = ANY($3)) \
                 AND NOT EXISTS (SELECT 1 FROM {s}.semantic d WHERE d.namespace=$2 AND d.statement=f.statement AND d.observed_at=f.observed_at) \
                 RETURNING id, subject_entity, object"
            ),
            &[&src_ns, &dst_ns, &types],
        )
        .await?;
        if !rt.is_empty() || !facts.is_empty() {
            self.register_namespace(&c, dst_ns).await;
        }
        for f in &facts {
            let subj = canonicalize_entity_name(s_of(f, "subject_entity"), false);
            if !is_valid_entity_name(&subj, false) {
                continue;
            }
            let fid = i_of(f, "id");
            let se = self.upsert_entity(&c, dst_ns, &subj, None).await?;
            self.add_mention(&c, se, fid, "semantic").await?;
            let obj = canonicalize_entity_name(&s_of(f, "object").chars().take(100).collect::<String>(), true);
            if is_valid_entity_name(&obj, true) {
                let oe = self.upsert_entity(&c, dst_ns, &obj, None).await?;
                self.add_mention(&c, oe, fid, "semantic").await?;
                self.bump_edge(&c, dst_ns, se, oe, 1.0).await?;
            }
        }
        Ok(json!({"src": src_ns, "dst": dst_ns, "raw_turns": rt.len(), "facts": facts.len(), "memory_types": memory_types}))
    }

    /// `/namespace/copy`: copy or move memories (`raw_turns`, `semantic` facts, and `episodic`
    /// summaries) from `src_ns` to `dst_ns`, optionally filtered by substring `like_filter`.
    pub async fn namespace_copy(
        &self,
        src_ns: &str,
        dst_ns: &str,
        mode: &str,
        like_filter: Option<&str>,
        copied_by: Option<i64>,
    ) -> Result<Value, String> {
        if src_ns.trim().is_empty() || dst_ns.trim().is_empty() || src_ns == dst_ns {
            return Err("src and dst must be two different non-empty namespaces".into());
        }
        let mode = mode.trim();
        if mode != "copy" && mode != "move" {
            return Err(format!("invalid mode {:?} (expected 'copy' or 'move')", mode));
        }
        let pat: Option<String> = like_filter
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| format!("%{}%", s.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_")));
        let s = &self.schema;
        let c = self.conn().await?;
        let rt = jq(
            &c,
            &format!(
                "INSERT INTO {s}.raw_turns(namespace,session_id,speaker,text,observed_at,embedding,author_principal,memory_type,constraint_subject,code_anchors) \
                 SELECT $2,session_id,speaker,text,observed_at,embedding,author_principal,memory_type,constraint_subject,code_anchors FROM {s}.raw_turns r \
                 WHERE r.namespace=$1 AND ($3::text IS NULL OR r.text ILIKE $3 ESCAPE '\\') AND r.constraint_retired_at IS NULL \
                 AND NOT EXISTS (SELECT 1 FROM {s}.raw_turns d WHERE d.namespace=$2 AND d.text=r.text AND d.observed_at=r.observed_at) RETURNING id"
            ),
            &[&src_ns, &dst_ns, &pat],
        )
        .await?;
        let facts = jq(
            &c,
            &format!(
                "INSERT INTO {s}.semantic(namespace,kind,statement,subject_entity,predicate,object,valid_from,valid_to,observed_at,confidence,salience,embedding,\
                 source_speaker,author_principal,memory_type,constraint_subject,constraint_since,constraint_until,code_anchors) \
                 SELECT $2,kind,statement,subject_entity,predicate,object,valid_from,valid_to,observed_at,confidence,salience,embedding,\
                 source_speaker,author_principal,memory_type,constraint_subject,constraint_since,constraint_until,code_anchors FROM {s}.semantic f \
                 WHERE f.namespace=$1 AND f.expired_at IS NULL AND f.constraint_retired_at IS NULL AND ($3::text IS NULL OR f.statement ILIKE $3 ESCAPE '\\') \
                 AND NOT EXISTS (SELECT 1 FROM {s}.semantic d WHERE d.namespace=$2 AND d.statement=f.statement AND d.observed_at=f.observed_at) \
                 RETURNING id, subject_entity, object"
            ),
            &[&src_ns, &dst_ns, &pat],
        )
        .await?;
        let eps = jq(
            &c,
            &format!(
                "INSERT INTO {s}.episodic(namespace,session_id,text,summary,t_start,t_end,observed_at,salience,embedding,author_principal,memory_type,constraint_subject) \
                 SELECT $2,session_id,text,summary,t_start,t_end,observed_at,salience,embedding,author_principal,memory_type,constraint_subject FROM {s}.episodic e \
                 WHERE e.namespace=$1 AND e.constraint_retired_at IS NULL AND ($3::text IS NULL OR e.text ILIKE $3 ESCAPE '\\') \
                 AND NOT EXISTS (SELECT 1 FROM {s}.episodic d WHERE d.namespace=$2 AND d.text=e.text AND d.observed_at=e.observed_at) \
                 RETURNING id"
            ),
            &[&src_ns, &dst_ns, &pat],
        )
        .await?;
        if !rt.is_empty() || !facts.is_empty() || !eps.is_empty() {
            self.register_namespace(&c, dst_ns).await;
        }
        for f in &facts {
            let subj = canonicalize_entity_name(s_of(f, "subject_entity"), false);
            if !is_valid_entity_name(&subj, false) {
                continue;
            }
            let fid = i_of(f, "id");
            let se = self.upsert_entity(&c, dst_ns, &subj, None).await?;
            self.add_mention(&c, se, fid, "semantic").await?;
            let obj = canonicalize_entity_name(&s_of(f, "object").chars().take(100).collect::<String>(), true);
            if is_valid_entity_name(&obj, true) {
                let oe = self.upsert_entity(&c, dst_ns, &obj, None).await?;
                self.add_mention(&c, oe, fid, "semantic").await?;
                self.bump_edge(&c, dst_ns, se, oe, 1.0).await?;
            }
        }
        if mode == "move" {
            let _ = c
                .execute(
                    format!("DELETE FROM {s}.raw_turns WHERE namespace=$1 AND ($2::text IS NULL OR text ILIKE $2 ESCAPE '\\')").as_str(),
                    &[&src_ns, &pat],
                )
                .await;
            let _ = c
                .execute(
                    format!("UPDATE {s}.semantic SET expired_at=now(), valid_to=COALESCE(valid_to, now()) WHERE namespace=$1 AND expired_at IS NULL AND ($2::text IS NULL OR statement ILIKE $2 ESCAPE '\\')").as_str(),
                    &[&src_ns, &pat],
                )
                .await;
            let _ = c
                .execute(
                    format!("DELETE FROM {s}.episodic WHERE namespace=$1 AND ($2::text IS NULL OR text ILIKE $2 ESCAPE '\\')").as_str(),
                    &[&src_ns, &pat],
                )
                .await;
        }
        let n_rt = rt.len() as i32;
        let n_f = facts.len() as i32;
        let n_e = eps.len() as i32;
        let _ = c
            .execute(
                "INSERT INTO memnos_control.namespace_copy_provenance(src_ns,dst_ns,mode,copied_by,raw_turns,facts,episodes) VALUES($1,$2,$3,$4,$5,$6,$7)",
                &[&src_ns, &dst_ns, &mode, &copied_by, &n_rt, &n_f, &n_e],
            )
            .await;
        Ok(json!({
            "mode": mode,
            "src": src_ns,
            "dst": dst_ns,
            "raw_turns": rt.len(),
            "facts": facts.len(),
            "episodes": eps.len(),
        }))
    }
}

// ---------------------------------------------------------------------------------------------
// Corpus governance (architecture constraints)
// ---------------------------------------------------------------------------------------------

static CONSTRAINT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|SHALL|MUST|REQUIRED|SHOULD|PROHIBITED|FORBIDDEN)\b").unwrap()
});
static NEGATIVE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\b(SHALL NOT|MUST NOT|SHOULD NOT|MAY NOT|PROHIBITED|FORBIDDEN)\b").unwrap());
static WORD4_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[A-Za-z]{4,}").unwrap());
const DIFF_STOPWORDS: &[&str] = &[
    "shall", "must", "should", "required", "prohibited", "forbidden", "this", "that", "these", "those", "with",
    "from", "into", "have", "has", "had", "will", "would", "could", "when", "then", "than", "were", "been",
    "being", "there", "their", "which", "what", "where", "while", "about", "after", "before", "between",
    "during", "under", "over", "also", "only", "each", "every", "some", "more", "most", "such", "same",
    "other", "just", "your", "them", "here", "upon", "onto", "across", "does", "doing", "done", "using", "used",
];
const DIFF_MIN_OVERLAP: usize = 2;
const DIFF_WORD_CAP: usize = 300;

fn words_of(text: &str, strip_stop: bool) -> HashSet<String> {
    WORD4_RE
        .find_iter(text)
        .map(|m| m.as_str().to_lowercase())
        .filter(|w| !strip_stop || !DIFF_STOPWORDS.contains(&w.as_str()))
        .collect()
}

fn ordered_unique_words(text: &str, cap: usize) -> Vec<String> {
    let mut seen = HashSet::new();
    WORD4_RE
        .find_iter(text)
        .map(|m| m.as_str().to_lowercase())
        .filter(|w| seen.insert(w.clone()))
        .take(cap)
        .collect()
}

/// Fair-share the `k` output slots across namespace levels (own namespace first, then ancestors
/// nearest-first) before score-only backfill, so a leaf with >= k own matches cannot starve an
/// ancestor's single relevant rule.
pub fn rank_by_specificity(rows: Vec<Value>, nss: &[String], k: usize) -> Vec<Value> {
    if k == 0 || rows.is_empty() {
        return vec![];
    }
    let idx = |r: &Value| nss.iter().position(|n| n == s_of(r, "namespace")).unwrap_or(nss.len());
    let mut levels: Vec<usize> = rows.iter().map(idx).collect::<HashSet<_>>().into_iter().collect();
    levels.sort();
    let share = (k / levels.len()).max(1);
    let (mut out, mut leftover): (Vec<Value>, Vec<Value>) = (vec![], vec![]);
    for l in &levels {
        let group: Vec<&Value> = rows.iter().filter(|r| idx(r) == *l).collect();
        out.extend(group.iter().take(share).map(|r| (*r).clone()));
        leftover.extend(group.iter().skip(share).map(|r| (*r).clone()));
    }
    if out.len() < k {
        leftover.sort_by(|a, b| idx(a).cmp(&idx(b)).then(f_of(b, "score").partial_cmp(&f_of(a, "score")).unwrap()));
        out.extend(leftover.into_iter().take(k - (out.len())));
    }
    out.sort_by(|a, b| idx(a).cmp(&idx(b)).then(f_of(b, "score").partial_cmp(&f_of(a, "score")).unwrap()));
    out.truncate(k);
    out
}

/// Classify each candidate constraint against per-hunk word sets: `violated` / `satisfied` /
/// `uncovered`, with a would-be violation of a non-active (expired or deviated) constraint routed
/// to `deviated`. Hunk-local on purpose: one hunk's removed-side vocabulary can never cancel a
/// different hunk's real violation. Returns `(violated, satisfied, uncovered, deviated)`.
pub fn classify_diff(candidates: &[Value], hunks: &[(String, String)]) -> (Vec<Value>, Vec<Value>, Vec<Value>, Vec<Value>) {
    let hunk_words: Vec<(HashSet<String>, HashSet<String>)> = hunks.iter().map(|(a, r)| (words_of(a, false), words_of(r, false))).collect();
    let added_all: HashSet<String> = hunk_words.iter().flat_map(|h| h.0.iter().cloned()).collect();
    let removed_all: HashSet<String> = hunk_words.iter().flat_map(|h| h.1.iter().cloned()).collect();
    let (mut violated, mut satisfied, mut uncovered, mut deviated) = (vec![], vec![], vec![], vec![]);
    for row in candidates {
        let content = s_of(row, "content");
        let cw = words_of(content, true);
        let prohibition = NEGATIVE_RE.is_match(&content.to_uppercase());
        let (mut vh, mut sh): (Option<(usize, usize, Vec<String>)>, Option<(usize, usize, Vec<String>)>) = (None, None);
        for (ha, hr) in &hunk_words {
            let (add, rem) = (cw.intersection(ha).count(), cw.intersection(hr).count());
            if add.max(rem) < DIFF_MIN_OVERLAP {
                continue;
            }
            let added_dominant = add >= rem;
            let violation = if prohibition { added_dominant } else { !added_dominant };
            let mut matched: Vec<String> = cw.iter().filter(|w| ha.contains(*w) || hr.contains(*w)).cloned().collect();
            matched.sort();
            if violation {
                vh.get_or_insert((add, rem, matched));
            } else {
                sh.get_or_insert((add, rem, matched));
            }
        }
        let (add, rem, matched) = match vh.clone().or(sh.clone()) {
            Some(d) => d,
            None => {
                let mut m: Vec<String> = cw.iter().filter(|w| added_all.contains(*w) || removed_all.contains(*w)).cloned().collect();
                m.sort();
                (cw.intersection(&added_all).count(), cw.intersection(&removed_all).count(), m)
            }
        };
        let entry = json!({
            "id": row["id"], "content": content, "source": row["source"], "namespace": row["namespace"], "score": row["score"],
            "matched_terms": matched, "added_hits": add, "removed_hits": rem,
            "status": row["status"], "since": row["since"], "until": row["until"],
        });
        if vh.is_some() && s_of(row, "status") != "active" {
            deviated.push(entry);
        } else if vh.is_some() {
            violated.push(entry);
        } else if sh.is_some() {
            satisfied.push(entry);
        } else {
            uncovered.push(entry);
        }
    }
    (violated, satisfied, uncovered, deviated)
}

impl BrainStore {
    /// Parse RFC-2119 normative sentences out of a document and store each as a `constraint` fact
    /// tagged with `source`. Re-ingesting a source replaces its previous constraints. `since` and
    /// `until` (dotted-numeric versions) bound the release window the constraints apply to.
    /// Returns `{source, constraints, ids}`.
    pub async fn corpus_ingest(
        &self,
        namespace: &str,
        source: &str,
        text: &str,
        author: Option<&str>,
        since: Option<&str>,
        until: Option<&str>,
    ) -> Result<Value, String> {
        let norm = |v: Option<&str>| v.map(str::trim).filter(|x| !x.is_empty()).map(String::from);
        let (since, until) = (norm(since), norm(until));
        let ks = since.as_deref().map(parse_semver_key).transpose()?;
        let ku = until.as_deref().map(parse_semver_key).transpose()?;
        if let (Some(a), Some(b)) = (ks, ku) {
            if a >= b {
                return Err(format!("since ({:?}) must be earlier than until ({:?})", since, until));
            }
        }
        if source.trim().is_empty() || text.trim().is_empty() {
            return Err("name and text required".into());
        }
        let mut cands: Vec<String> = vec![];
        for raw in clean_text(text).split('\n') {
            let line = raw.trim().trim_start_matches(|c: char| "#-*>| ".contains(c)).trim();
            if line.is_empty() {
                continue;
            }
            // split into sentences after . ! ? followed by whitespace
            let (mut start, b) = (0usize, line.as_bytes());
            let mut sents: Vec<&str> = vec![];
            for i in 0..b.len() {
                if matches!(b[i], b'.' | b'!' | b'?') && b.get(i + 1).map_or(false, |x| x.is_ascii_whitespace()) {
                    sents.push(&line[start..=i]);
                    start = i + 1;
                }
            }
            sents.push(&line[start..]);
            for sent in sents {
                let sent = sent.trim();
                if sent.len() >= 8 && CONSTRAINT_RE.is_match(&sent.to_uppercase()) {
                    cands.push(sent.chars().take(1000).collect());
                }
            }
        }
        let s = &self.schema;
        let c = self.conn().await?;
        c.execute(format!("DELETE FROM {s}.semantic WHERE namespace=$1 AND kind='constraint' AND subject_entity=$2").as_str(), &[&namespace, &source]).await.map_err(de)?;
        let ins = format!(
            "INSERT INTO {s}.semantic(namespace,kind,statement,subject_entity,predicate,object,author_principal,constraint_since,constraint_until) \
             VALUES($1,'constraint',$2,$3,'constraint_of',$3,$4,$5,$6) RETURNING id"
        );
        let mut ids: Vec<i64> = vec![];
        for sent in &cands {
            let row = c.query_one(ins.as_str(), &[&namespace, sent, &source, &author, &since, &until]).await.map_err(de)?;
            ids.push(row.get(0));
        }
        self.register_namespace(&c, namespace).await;
        Ok(json!({"source": source, "constraints": ids.len(), "ids": ids}))
    }

    /// FTS candidates over `kind='constraint'` facts across `nss`, with version window and
    /// approved-deviation status, ranked specificity-first. The deviation table belongs to the
    /// control module; if it is missing the query is retried without it.
    async fn corpus_candidates(&self, nss: &[String], q: &str, name: Option<&str>, k: usize, version: Option<&str>) -> Result<Vec<Value>, String> {
        let version_key = version.map(str::trim).filter(|v| !v.is_empty()).map(parse_semver_key).transpose()?;
        if nss.is_empty() || q.is_empty() {
            return Ok(vec![]);
        }
        let s = &self.schema;
        let nss_v = nss.to_vec();
        let cap = k.max(50) as i64;
        let build = |with_dev: bool| {
            let (dev_cols, dev_join) = if with_dev {
                ("d.until AS deviation_until, (d.id IS NOT NULL) AS has_deviation,",
                 "LEFT JOIN LATERAL (SELECT id, until FROM memnos_control.corpus_deviations WHERE namespace = s.namespace AND constraint_id = s.id ORDER BY created_at DESC LIMIT 1) d ON true")
            } else {
                ("NULL::text AS deviation_until, false AS has_deviation,", "")
            };
            format!(
                "WITH scored AS (SELECT s.id, s.statement AS content, s.subject_entity AS source, s.namespace, \
                   s.constraint_since AS since, s.constraint_until AS until, {dev_cols} \
                   ts_rank(s.fts, websearch_to_tsquery('english',$1)) AS score FROM {s}.semantic s {dev_join} \
                   WHERE s.namespace = ANY($2) AND s.kind='constraint' AND s.expired_at IS NULL \
                   AND ($4::text IS NULL OR s.subject_entity=$4) AND s.fts @@ websearch_to_tsquery('english',$1)), \
                 ranked AS (SELECT *, row_number() OVER (PARTITION BY namespace ORDER BY score DESC) AS rn FROM scored) \
                 SELECT id, content, source, namespace, since, until, deviation_until, has_deviation, score \
                 FROM ranked WHERE rn <= $3 ORDER BY score DESC"
            )
        };
        let rows = match self.q(&build(true), &[&q, &nss_v, &cap, &name]).await {
            Ok(r) => r,
            Err(e) if e.contains("corpus_deviations") || e.starts_with("42P01") => self.q(&build(false), &[&q, &nss_v, &cap, &name]).await?,
            Err(e) => return Err(e),
        };
        let mut out = vec![];
        for mut row in rows {
            let has_dev = row["has_deviation"].as_bool().unwrap_or(false);
            let dev_until = s_of(&row, "deviation_until").to_string();
            let status = match version_key {
                None => if has_dev { "approved_deviation" } else { "active" },
                Some(vk) => {
                    if let Ok(since_k) = parse_semver_key(s_of(&row, "since")) {
                        if vk < since_k {
                            continue; // not yet introduced at this version
                        }
                    }
                    let expired = parse_semver_key(s_of(&row, "until")).map_or(false, |u| vk >= u);
                    let dev_active = has_dev && (dev_until.is_empty() || parse_semver_key(&dev_until).map_or(true, |u| vk < u));
                    if dev_active { "approved_deviation" } else if expired { "expired" } else { "active" }
                }
            };
            if let Some(o) = row.as_object_mut() {
                o.remove("has_deviation");
                o.remove("deviation_until");
                o.insert("status".into(), json!(status));
            }
            out.push(row);
        }
        Ok(rank_by_specificity(out, nss, k))
    }

    pub async fn constraint_exists(&self, namespace: &str, id: i64) -> Result<bool, String> {
        let s = &self.schema;
        let rows = self
            .q(
                &format!("SELECT id FROM {s}.semantic WHERE id=$1 AND namespace=$2 AND kind='constraint' AND expired_at IS NULL LIMIT 1"),
                &[&id, &namespace],
            )
            .await?;
        Ok(!rows.is_empty())
    }

    /// Constraints most relevant to a code snippet. `nss` is the ordered namespace search list,
    /// most specific first; each row carries its source `namespace`, version `status`
    /// (`active | expired | approved_deviation`) and its `since`/`until` window. A constraint not
    /// yet introduced at `version` is dropped.
    pub async fn corpus_check(&self, nss: &[String], snippet: &str, k: usize, version: Option<&str>) -> Result<Vec<Value>, String> {
        if let Some(v) = version.map(str::trim).filter(|v| !v.is_empty()) {
            parse_semver_key(v)?;
        }
        let words = ordered_unique_words(snippet, 40);
        if words.is_empty() {
            return Ok(vec![]);
        }
        self.corpus_candidates(nss, &words.join(" or "), None, k, version).await
    }

    /// Verdict for a unified diff: each relevant constraint is `violated`, `satisfied`,
    /// `uncovered` or `deviated`, plus `score = satisfied / (satisfied + violated)` (1.0 when
    /// nothing was evaluated; `evaluated` disambiguates a vacuous 1.0).
    pub async fn corpus_check_diff(&self, nss: &[String], diff: &str, name: Option<&str>, k: usize, version: Option<&str>) -> Result<Value, String> {
        let hunks = split_diff_hunks(diff);
        let all_text = hunks.iter().flat_map(|(a, r)| [a.as_str(), r.as_str()]).collect::<Vec<_>>().join("\n");
        let words = ordered_unique_words(&all_text, DIFF_WORD_CAP);
        let empty = json!({"violated": [], "satisfied": [], "uncovered": [], "deviated": [], "score": 1.0, "evaluated": 0});
        if words.is_empty() {
            // still validate the version string so a typo is a 400, not a silent pass
            if let Some(v) = version.filter(|v| !v.trim().is_empty()) {
                parse_semver_key(v)?;
            }
            return Ok(empty);
        }
        let candidates = self.corpus_candidates(nss, &words.join(" or "), name, k, version).await?;
        let (violated, satisfied, uncovered, deviated) = classify_diff(&candidates, &hunks);
        let evaluated = violated.len() + satisfied.len();
        let score = if evaluated > 0 { (satisfied.len() as f64 / evaluated as f64 * 1e4).round() / 1e4 } else { 1.0 };
        Ok(json!({"violated": violated, "satisfied": satisfied, "uncovered": uncovered, "deviated": deviated,
                  "score": score, "evaluated": evaluated}))
    }
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn pin(id: i64, ns: &str, subj: &str, content: &str) -> Value {
        json!({"id": id, "kind": "turn", "namespace": ns, "constraint_subject": subj, "content": content, "ts": "2026-01-01"})
    }

    #[test]
    fn rrf_rewards_agreement_and_is_deterministic() {
        let l1 = vec!["a".to_string(), "b".into(), "c".into()];
        let l2 = vec!["c".to_string(), "d".into()];
        let out = rrf_fuse(&[l1.clone(), l2.clone()], 60.0);
        // c is rank 3 in l1 and rank 1 in l2 -> 1/63 + 1/61 beats a's 1/61
        assert_eq!(out[0].0, "c");
        assert!((out[0].1 - (1.0 / 63.0 + 1.0 / 61.0)).abs() < 1e-12);
        assert_eq!(out.len(), 4);
        assert_eq!(out, rrf_fuse(&[l1, l2], 60.0));
        // equal scores keep first-seen order
        let tie = rrf_fuse(&[vec!["x".to_string()], vec!["y".to_string()]], 60.0);
        assert_eq!((tie[0].0.as_str(), tie[1].0.as_str()), ("x", "y"));
    }

    #[test]
    fn constraint_precedence_ancestor_wins_unless_overridden() {
        let rows = vec![pin(1, "org:acme", "deploy", "ORG: never deploy on Friday"), pin(2, "org:acme:web", "deploy", "WEB: deploy any day")];
        let (w, l) = resolve_constraint_precedence(rows.clone(), &HashSet::new());
        assert_eq!(w.len(), 1);
        assert_eq!(w[0]["namespace"], "org:acme");
        assert_eq!(l.len(), 1);
        assert_eq!((l[0]["namespace"].as_str(), l[0]["winner_namespace"].as_str()), (Some("org:acme:web"), Some("org:acme")));
        // explicit (child, parent) override reverses the default
        let ov: HashSet<(String, String)> = HashSet::from([("org:acme:web".to_string(), "org:acme".to_string())]);
        let (w, _) = resolve_constraint_precedence(rows, &ov);
        assert_eq!(w.len(), 1);
        assert_eq!(w[0]["namespace"], "org:acme:web");
    }

    #[test]
    fn constraint_precedence_leaves_siblings_untagged_and_cycles_alone() {
        // siblings: no ancestor relation, both survive; untagged always survive
        let rows = vec![pin(1, "org:a", "s", "x"), pin(2, "org:b", "s", "y"), pin(3, "org:a", "", "z")];
        let (w, l) = resolve_constraint_precedence(rows, &HashSet::new());
        assert_eq!((w.len(), l.len()), (3, 0));
        // 3-level chain with only the root/leaf pair reversed forms a cycle: fail open
        let rows = vec![pin(1, "a", "s", "r"), pin(2, "a:b", "s", "m"), pin(3, "a:b:c", "s", "l")];
        let ov = HashSet::from([("a:b:c".to_string(), "a".to_string())]);
        let (w, l) = resolve_constraint_precedence(rows, &ov);
        assert_eq!(w.len() + l.len(), 3);
        assert!(!w.is_empty());
        assert!(is_ancestor_ns("org:acme", "org:acme:web") && !is_ancestor_ns("org:ac", "org:acme"));
    }

    #[test]
    fn diff_hunks_split_on_headers_and_hunk_markers() {
        let diff = "--- a/x.rs\n+++ b/x.rs\n@@ -1,2 +1,2 @@\n-let a = 1;\n+let a = 2;\n@@ -9,1 +9,2 @@\n+retry_forever();\n--- do not skip validation\n context\n";
        let h = split_diff_hunks(diff);
        // implicit lead, file-header hunk, @@ hunk 1, @@ hunk 2
        assert_eq!(h.len(), 4);
        assert_eq!((h[2].0.as_str(), h[2].1.as_str()), ("let a = 2;", "let a = 1;"));
        assert!(h[3].0.contains("retry_forever();"));
        // a lone '--- foo' line (no '+++' follows) is removed CONTENT, not a header
        assert!(h[3].1.contains("-- do not skip validation"));
        assert_eq!(split_diff_hunks("").len(), 1);
    }

    #[test]
    fn diff_classification_is_hunk_local_and_routes_deviations() {
        let cand = |id: i64, text: &str, status: &str| json!({"id": id, "content": text, "source": "doc", "namespace": "n", "score": 1.0, "status": status, "since": null, "until": null});
        let hunks = split_diff_hunks("@@ -1 +1 @@\n+call database directly from controller handlers\n@@ -5 +5 @@\n-unrelated deleted comment about database controller handlers\n");
        let prohib = cand(1, "Controllers MUST NOT call database directly from controller handlers.", "active");
        let (v, s, u, d) = classify_diff(&[prohib.clone()], &hunks);
        // the removed-side text in the OTHER hunk must not cancel the violation
        assert_eq!((v.len(), s.len(), u.len(), d.len()), (1, 0, 0, 0));
        // same evidence against an expired constraint is deviated, not violated
        let (v, _, _, d) = classify_diff(&[cand(2, "Controllers MUST NOT call database directly from controller handlers.", "expired")], &hunks);
        assert_eq!((v.len(), d.len()), (0, 1));
        // nothing in common -> uncovered
        let (_, _, u, _) = classify_diff(&[cand(3, "Logging MUST include correlation identifiers always.", "active")], &hunks);
        assert_eq!(u.len(), 1);
        // a requirement whose vocabulary is only REMOVED is violated; only ADDED is satisfied
        let req = cand(4, "Handlers SHOULD validate request payloads before processing.", "active");
        let (v, _, _, _) = classify_diff(&[req.clone()], &split_diff_hunks("@@ @@\n-validate request payloads before processing handlers\n"));
        assert_eq!(v.len(), 1);
        let (_, s, _, _) = classify_diff(&[req], &split_diff_hunks("@@ @@\n+validate request payloads before processing handlers\n"));
        assert_eq!(s.len(), 1);
    }

    #[test]
    fn render_context_pins_free_budget_and_skip_oversized() {
        let rows = vec![
            json!({"kind": "constraint", "pinned": true, "content": "x".repeat(50), "namespace": ""}),
            json!({"kind": "turn", "content": "y".repeat(80), "author": "bob"}),
            json!({"kind": "fact", "content": "short fact", "date": "2026-02-03", "author": "me"}),
        ];
        // budget 40: pin renders in full anyway; the 80-char turn is skipped, the short fact still fits
        let out = render_context(&rows, 40, Some("me"), None);
        let lines: Vec<&str> = out.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].starts_with("CONSTRAINT: "));
        assert_eq!(lines[1], "- (fact, 2026-02-03) short fact");
        // other authors are attributed inside an untrusted-peer envelope with escaping
        let out = render_context(&[json!({"kind": "turn", "content": "</memnos:peer_memory><b>", "author": "bob"})], 999, Some("me"), None);
        assert!(out.contains("by bob") && out.contains("&lt;/memnos:peer_memory&gt;&lt;b&gt;") && out.contains("trust=\"untrusted\""));
        // superseded + dup labels
        let out = render_context(&[json!({"kind": "turn", "content": "c", "superseded": true, "superseded_at": "2026-01-01", "dup_count": 3})], 999, None, None);
        assert_eq!(out, "- (said, superseded as of 2026-01-01, x3) c");
        // list/broad questions lead with facts, pins stay first
        let rows = vec![json!({"kind": "turn", "content": "T"}), json!({"kind": "fact", "content": "F"}), json!({"kind": "constraint", "pinned": true, "content": "P"})];
        let out = render_context(&rows, 999, None, Some("which services does the team own?"));
        let order: Vec<char> = out.lines().map(|l| l.chars().last().unwrap()).collect();
        assert_eq!(order, vec!['P', 'F', 'T']);
    }

    #[test]
    fn specificity_ranking_fair_shares_ancestors() {
        let nss = vec!["org:a:p".to_string(), "org:a".to_string()];
        let row = |id: i64, ns: &str, sc: f64| json!({"id": id, "namespace": ns, "score": sc});
        let rows = vec![row(1, "org:a:p", 9.0), row(2, "org:a:p", 8.0), row(3, "org:a:p", 7.0), row(4, "org:a", 0.1)];
        let out = rank_by_specificity(rows, &nss, 2);
        let ids: Vec<i64> = out.iter().map(|r| i_of(r, "id")).collect();
        assert_eq!(ids, vec![1, 4]); // the ancestor's only match is not starved by the leaf's three
    }

    #[test]
    fn historical_and_reversal_gates() {
        assert!(is_historical("Alice lived in Boston in 2019"));
        assert!(is_historical("The server was decommissioned in 2021"));
        assert!(!is_historical("The branch was merged")); // bare passive = current state
        assert!(!is_historical("The rate limit was changed to 200 as it lived on")); // change-of-state wins
        let cands = vec![json!({"id": 7, "dist": 0.2, "subject_entity": "zeta", "statement": "The zeta deployment is blocked on database migration"})];
        let hit = negation_targets("The zeta deployment is no longer blocked on database migration", Some("zeta"), &cands, 0.4);
        assert_eq!(hit, vec![7]);
        // subject mismatch or too-distant candidates are never closed
        assert!(negation_targets("The zeta deployment is no longer blocked on database migration", Some("omega"), &cands, 0.4).is_empty());
        assert!(negation_targets("The zeta deployment is no longer blocked on database migration", Some("zeta"), &cands, 0.1).is_empty());
    }

    #[test]
    fn entity_names_literals_are_rejected() {
        for ok in ["AcmeApp", "payments-sync", "Acme, Inc.", "In Memory Cache"] {
            assert!(is_valid_entity_name(ok, false), "{ok}");
        }
        for bad in ["42", "$1,200", "true", "2026-01-02", "in the pool", "waiting on review", "a; b", "", ":8080"] {
            assert!(!is_valid_entity_name(bad, true), "{bad}");
        }
        assert_eq!(canonicalize_entity_name("The User", false), "user");
        assert_eq!(canonicalize_entity_name("AcmeApp", true), "AcmeApp");
    }

    #[test]
    fn constraint_subject_is_deterministic_and_text_sensitive() {
        let a = derive_constraint_subject("Never deploy without a rollback plan");
        assert_eq!(a, derive_constraint_subject("  never   DEPLOY without a rollback plan "));
        assert_ne!(a, derive_constraint_subject("Never deploy on Fridays"));
        assert!(a.starts_with("auto:never-deploy-without-a-rollback-") && a.len() < 80);
        assert_eq!(effective_constraint_subject(Some("fact"), Some("x"), "t"), None);
        assert_eq!(effective_constraint_subject(Some("constraint"), Some(" Deploy-Policy "), "t").as_deref(), Some("deploy-policy"));
    }

    #[test]
    fn small_pure_helpers() {
        assert!(check_ident("tenant_memnos").is_ok() && check_ident("x; DROP").is_err() && check_ident("1a").is_err());
        assert_eq!(parse_semver_key("2.1").unwrap(), parse_semver_key("2.1.0").unwrap());
        assert!(parse_semver_key("10.0.0").unwrap() > parse_semver_key("9.0.0").unwrap());
        assert!(parse_semver_key("2.x").is_err() && parse_semver_key("").is_err() && parse_semver_key("1.2.3.4.5.6.7").is_err());
        assert_eq!(fts_clamp("fix: a-----b (c) OR \"d\"\0"), "fix a b c OR d");
        assert_eq!(fts_clamp(&"w ".repeat(500)).split(' ').count(), 64);
        assert_eq!((query_intent("what exactly did she say"), query_intent("how many hosts do we run"), query_intent("where are we with the rollout")), ("verbatim", "list", "broad"));
        assert!(is_error_query("traceback in the worker") && !is_error_query("hello"));
        assert_eq!(extractive_summary("One. Two! Three? Four."), "One. Two!");
        assert_eq!(format_fix_memory(" s ", "c", "f", Some("make test")), "Symptom: s\nCause: c\nFix: f\nVerify: make test");
        assert_eq!(contradiction_penalty(0.0), 0);
        assert_eq!(contradiction_penalty(1.0), 40);
        // fix memories are boosted only on error-shaped queries; git-bulk rows are damped
        let fix = json!({"content": "c", "memory_type": "fix"});
        assert!(adjust_score("turn", &fix, 1.0, true, false) > adjust_score("turn", &fix, 1.0, false, false));
        assert!(adjust_score("turn", &json!({"content": "c", "speaker": "git"}), 1.0, false, false) < 1.0);
    }


    /// Built here (not via `db::build_pool`): deadpool needs `.runtime(Tokio1)` once a wait timeout
    /// is set, which `db::build_pool` does not do.
    fn test_pool(dsn: &str) -> Pool {
        let cfg = <tokio_postgres::Config as std::str::FromStr>::from_str(dsn).unwrap();
        let mgr = deadpool_postgres::Manager::from_config(
            cfg,
            tokio_postgres::NoTls,
            deadpool_postgres::ManagerConfig { recycling_method: deadpool_postgres::RecyclingMethod::Fast },
        );
        Pool::builder(mgr)
            .max_size(8)
            .runtime(deadpool_postgres::Runtime::Tokio1)
            .wait_timeout(Some(std::time::Duration::from_secs(10)))
            .build()
            .unwrap()
    }

    fn quiet_env() {
        std::env::remove_var("OPENAI_API_KEY");
        std::env::remove_var("MEMNOS_EXTRACT_BASE_URL");
        std::env::remove_var("MEMNOS_DEDUPE_THRESHOLD");
    }

    // Compile-time guard: every public future must be Send so an axum handler can await it.
    #[allow(dead_code)]
    fn futures_are_send(s: &BrainStore) {
        fn send<T: Send>(_: T) {}
        let o = RecallOpts::default();
        send(s.remember("n", "t", None, None, None, None, None, None));
        send(s.recall("n", "q", &o));
        send(s.recall_wide(&[], "q", &o));
        send(s.memory_search("n", "q", &o));
        send(s.reconcile_claim("n", "c", None, None));
        send(s.knowledge_health("n"));
        send(s.consolidate("n"));
        send(s.segment_episodes("n", 30, 10));
        send(s.copy_memories_from("a", "b", &[]));
        send(s.corpus_check_diff(&[], "d", None, 10, None));
        send(s.community_search("n", "e", 5));
    }

    /// Runtime SQL check. Needs a THROWAWAY Postgres with pgvector, never a live memnos database:
    ///   MEMNOS_TEST_DSN=postgres://user:pw@127.0.0.1:5432/db cargo test --lib store -- --ignored
    /// It works in a scratch schema that it drops at the end.
    #[tokio::test]
    #[ignore]
    async fn runtime_sql_end_to_end() {
        let Ok(dsn) = std::env::var("MEMNOS_TEST_DSN") else {
            eprintln!("MEMNOS_TEST_DSN not set; skipping");
            return;
        };
        quiet_env();
        let pool = test_pool(&dsn);
        let schema = format!("t_store_{}", std::process::id());
        let st = BrainStore::new(pool.clone(), Some(&schema), Arc::new(Embedder::from_env(None, Some(384))), Arc::new(Extractor::from_env(None))).unwrap();
        st.init_schema().await.expect("init");
        st.init_schema().await.expect("init is idempotent");
        st.warm_indexes().await.expect("warm");
        let ns = "proj:payments-sync";
        let o = RecallOpts::default();

        // constraints: same subject -> older retired, exactly one stays pinned
        let r1 = st.remember(ns, "Deploys MUST NOT happen on Fridays", None, Some("constraint"), Some("deploy-policy"), Some("alice"), None, None).await.unwrap();
        let r2 = st.remember(ns, "Deploys MUST NOT happen on Fridays or weekends", None, Some("constraint"), Some("Deploy-Policy"), Some("alice"), None, None).await.unwrap();
        assert_eq!(r1["facts_written"], 0);
        assert_eq!(r2["constraints_retired"].as_array().map(|a| a.len()), Some(1), "{r2}");
        assert_eq!(st.list_pinned_constraints(ns).await.unwrap().len(), 1);
        // an untagged constraint gets a derived subject and is retirable by id
        let r3 = st.remember(ns, "Always run the linter", None, Some("constraint"), None, None, None, None).await.unwrap();
        assert!(r3["constraint_subject"].as_str().unwrap().starts_with("auto:"));
        assert!(st.retire_constraint_by_id(ns, &format!("turn:{}", r3["turn_id"])).await.unwrap());

        // secrets are redacted before storage
        let rr = st.remember(ns, "token is mnk_AbCdEfGhIjKlMnOpQrStUvWxYz1234 ok", None, None, None, None, None, None).await.unwrap();
        assert_eq!(rr["redacted"], true);

        // bi-temporal supersession through the real write path
        let tid = st.remember(ns, "AcmeApp rollout notes", Some("user"), None, None, None, None, None).await.unwrap()["turn_id"].as_i64().unwrap();
        let mut c = st.conn().await.unwrap();
        let now = Utc::now();
        let mk = |obj: &str| PreparedFact { f: ExtractedFact { subject: "AcmeApp".into(), predicate: "deployed_version".into(), object: obj.into(), statement: format!("AcmeApp deployed_version is {}", obj) }, vec: vec![], ents: vec![] };
        let (mut p1, mut p2) = (mk("1.0.0"), mk("2.0.0"));
        p1.vec = st.embed(&p1.f.statement).await.unwrap();
        p2.vec = st.embed(&p2.f.statement).await.unwrap();
        p1.ents = vec![("AcmeApp".into(), false, st.embed("AcmeApp").await.unwrap()), ("deploy target".into(), true, st.embed("deploy target").await.unwrap())];
        let (id1, _) = st.write_fact(&mut c, ns, &p1, now, tid, None, Some("user"), None, &None).await.unwrap();
        let (id2, n) = st.write_fact(&mut c, ns, &p2, now + chrono::Duration::seconds(5), tid, None, Some("user"), None, &None).await.unwrap();
        assert_eq!(n, 1, "second value must supersede the first");
        // restating the live value reinforces instead of inserting
        let (dup, _) = st.write_fact(&mut c, ns, &p2, now + chrono::Duration::seconds(9), tid, None, Some("user"), None, &None).await.unwrap();
        assert!(dup.is_none());
        drop(c);
        let prov = st.get_provenance(ns, id1.unwrap()).await.unwrap().unwrap();
        assert_eq!(prov["fact"]["superseded_by"].as_i64(), id2);
        assert!(prov["fact"]["valid_to"].is_string());
        assert_eq!(prov["sources"].as_array().unwrap().len(), 1);
        assert!(st.get_entity(ns, "acmeapp", 1, 10).await.unwrap().is_some());

        // reconcile verdicts
        let sup = st.reconcile_claim(ns, "AcmeApp deployed_version is 2.0.0", Some("AcmeApp"), Some("deployed_version")).await.unwrap();
        assert_eq!(sup["verdict"], "supported", "{sup}");
        assert_eq!(sup["stale"], false);
        let stale = st.reconcile_claim(ns, "AcmeApp deployed_version is 1.0.0", Some("AcmeApp"), Some("deployed_version")).await.unwrap();
        assert_eq!(stale["verdict"], "stale", "{stale}");
        assert_eq!(stale["stale"], true);
        assert_eq!(stale["current_value"], "2.0.0");
        assert_eq!(stale["superseded_value"], "1.0.0");
        assert!(stale["conflicts"][0]["valid_from"].is_string());
        let contra = st.reconcile_claim(ns, "AcmeApp deployed_version is 9.9.9", Some("AcmeApp"), Some("deployed_version")).await.unwrap();
        assert_eq!(contra["verdict"], "contradicted", "{contra}");
        let unk = st.reconcile_claim(ns, "Zebra uses Azure", Some("Zebra"), Some("uses")).await.unwrap();
        assert_eq!((unk["verdict"].as_str(), unk["stale"].as_bool()), (Some("unknown"), Some(false)));
        assert!(unk["matches"].as_array().unwrap().is_empty());

        // recall: pins first, current value ranked, explain recorded
        let rec = st.recall(ns, "what version of AcmeApp is deployed", &o).await.unwrap();
        assert!(rec.get("degraded").is_none(), "{rec}");
        assert_eq!(rec["pinned_constraints"].as_array().unwrap().len(), 1);
        let ctx = rec["context"].as_str().unwrap();
        assert!(ctx.starts_with("CONSTRAINT:") && ctx.contains("2.0.0"), "{ctx}");
        assert!(!ctx.contains("deployed_version is 1.0.0"), "superseded fact must not be in current recall: {ctx}");
        assert_eq!(st.recall_explain(5).len(), 1);
        let hist = st.recall(ns, "what was the previous AcmeApp version before", &o).await.unwrap();
        assert!(hist["context"].as_str().unwrap().contains("1.0.0"), "historical intent keeps superseded facts");
        assert!(st.memory_search(ns, "AcmeApp", &o).await.unwrap()["count"].as_u64().unwrap() > 0);
        let wide = st.recall_wide(&[ns.to_string(), "other:ns".into()], "AcmeApp", &o).await.unwrap();
        assert!(wide["results"].as_array().unwrap().iter().any(|r| r["namespace"] == ns));
        // an fix memory
        let fx = st.record_fix(ns, "pool exhausted error", "leak in handler", "close the connection", Some("cargo test"), None).await.unwrap();
        assert_eq!(fx["memory_type"], "fix");
        assert_eq!(st.list_by_type(ns, "fix", 10).await.unwrap()["count"], 1);

        // governance
        assert!(st.check_contradictions(ns).await.unwrap()["total_groups"].is_number());
        let h = st.knowledge_health(ns).await.unwrap();
        assert!(h["score"].is_number() && h.get("degraded").is_none(), "{h}");
        assert_eq!(st.record_feedback(ns, "q", true, &[tid], &[id2.unwrap()], None).await.unwrap()["facts_adjusted"], 1);

        // episodes + consolidation
        assert!(st.segment_episodes(ns, 30, 50).await.unwrap()["episodes"].as_u64().unwrap() >= 1);
        assert_eq!(st.segment_episodes(ns, 30, 50).await.unwrap()["episodes"], 0, "segmentation is idempotent");
        let eps = st.recall_episodes(ns, "AcmeApp rollout", 5).await.unwrap();
        let eid = eps["episodes"][0]["id"].as_i64().unwrap();
        assert!(st.get_episode(ns, eid).await.unwrap().is_some());
        assert_eq!(st.decay_episodes(ns, 30.0).await.unwrap()["updated"].as_u64().map(|n| n >= 1), Some(true));
        let cons = st.consolidate(ns).await.unwrap();
        assert!(cons["duplicates_merged"].is_number());
        assert!(st.cleanup_all_entity_graphs(true).await.unwrap()["dry_run"] == true);

        // graph
        assert!(st.graph_query(ns, &["AcmeApp".into()], 2, 10).await.unwrap()["entities"].as_array().unwrap().len() >= 1);
        assert!(st.get_related(ns, "AcmeApp", 10).await.unwrap()["related"].is_array());
        assert!(st.community_search(ns, "AcmeApp", 10).await.unwrap().is_some());
        assert!(st.get_entity_dossier(ns, "AcmeApp").await.unwrap().is_none());

        // corpus (works without memnos_control: deviations are optional)
        let ing = st.corpus_ingest("org:acme", "arch.md", "# Rules\nControllers MUST NOT call database directly from controller handlers.\nLogging SHOULD include correlation identifiers.", Some("alice"), Some("1.0"), None).await.unwrap();
        assert_eq!(ing["constraints"], 2);
        let chk = st.corpus_check(&["org:acme:web".into(), "org:acme".into()], "controller handlers call database directly", 5, Some("1.2.0")).await.unwrap();
        assert!(!chk.is_empty() && chk[0]["status"] == "active", "{chk:?}");
        assert!(st.corpus_check(&["org:acme".into()], "controller handlers call database", 5, Some("0.5")).await.unwrap().is_empty(), "not yet introduced");
        let dv = st.corpus_check_diff(&["org:acme".into()], "@@ -1 +1 @@\n+call database directly from controller handlers\n", None, 10, None).await.unwrap();
        assert_eq!(dv["violated"].as_array().unwrap().len(), 1, "{dv}");
        assert_eq!(dv["score"], 0.0);

        // memory_delete expires, never hard-deletes
        let del = st.memory_delete(ns, id2.unwrap()).await.unwrap();
        assert_eq!(del["deleted"], true);
        assert_eq!(st.memory_delete(ns, 999_999).await.unwrap()["deleted"], false);
        assert!(st.get_provenance(ns, id2.unwrap()).await.unwrap().unwrap()["fact"]["expired_at"].is_string());

        // copy
        let cp = st.copy_memories_from(ns, "proj:copy-target", &[]).await.unwrap();
        assert!(cp["raw_turns"].as_u64().unwrap() > 0);
        let cp2 = st.copy_memories_from(ns, "proj:copy-target", &[]).await.unwrap();
        assert_eq!((cp2["raw_turns"].as_u64(), cp2["facts"].as_u64()), (Some(0), Some(0)), "repeat copy must not duplicate");
        assert!(st.copy_memories_from(ns, ns, &[]).await.is_err());

        // ingest_file
        let f = st.ingest_file(ns, "notes.md", Some("First paragraph.\n\nSecond paragraph about payments-sync."), None).await.unwrap();
        assert_eq!(f["chunks"], 1);
        assert!(st.counts().await.unwrap()["raw_turns"].as_i64().unwrap() > 5);

        let c = st.conn().await.unwrap();
        c.batch_execute(&format!("DROP SCHEMA {} CASCADE", schema)).await.unwrap();
    }

    async fn pf(st: &BrainStore, subj: &str, pred: &str, obj: &str, stmt: &str) -> PreparedFact {
        PreparedFact {
            f: ExtractedFact { subject: subj.into(), predicate: pred.into(), object: obj.into(), statement: stmt.into() },
            vec: st.embed(stmt).await.unwrap(),
            ents: vec![],
        }
    }

    /// A database built by the PYTHON engine's `create_brain_schema` (both a 1536-d `vector` and a
    /// 384-d `halfvec` one) must boot when configured with a matching-dimension embedder, refuse
    /// mismatched embedders loudly, and take writes, recall and reconcile unchanged.
    #[tokio::test]
    #[ignore]
    async fn runtime_existing_python_schema() {
        let Ok(dsn) = std::env::var("MEMNOS_TEST_DSN") else {
            return;
        };
        quiet_env();
        let pool = test_pool(&dsn);
        let c = pool.get().await.unwrap();
        c.batch_execute(include_str!("../../core/schema.sql")).await.unwrap();
        for (dim, vtype, ops) in [(1536i32, "vector", "vector_cosine_ops"), (384, "halfvec", "halfvec_cosine_ops")] {
            let tenant = format!("compat{}_{}", dim, std::process::id());
            c.execute("SELECT create_brain_schema($1, $2, $3, $4)", &[&tenant, &dim, &vtype, &ops]).await.unwrap();
            let schema = format!("tenant_{}", tenant);
            // 1) A 384-d embedder against a 1536-d schema MUST fail loudly (never silently hash!)
            if dim == 1536 {
                let bad_dim = BrainStore::new(
                    pool.clone(),
                    Some(&schema),
                    Arc::new(Embedder::from_env(None, Some(384))),
                    Arc::new(Extractor::from_env(None)),
                )
                .unwrap();
                let err = bad_dim
                    .init_schema()
                    .await
                    .expect_err("1536-d schema booted with 384-d embedder must fail loudly");
                assert!(err.contains("dimension mismatch"), "{err}");
            }
            // 2) Matching-dimension embedder boots cleanly, writes, recalls, and reconciles
            let matching_emb = if dim == 384 {
                Arc::new(Embedder::from_env(None, Some(384)))
            } else {
                Arc::new(Embedder::synthetic_for_test(dim as usize, "text-embedding-3-small"))
            };
            let st = BrainStore::new(pool.clone(), Some(&schema), matching_emb, Arc::new(Extractor::from_env(None))).unwrap();
            st.init_schema().await.expect("init over a python-built schema");
            assert_eq!((st.vt(), st.col_dim()), (vtype.to_string(), dim as usize));
            st.warm_indexes().await.unwrap();
            let ns = "proj:agy-webapp";
            let r = st.remember(ns, "Merged PR #123 and tagged v1.2.0 for agy-webapp", Some("user"), None, None, Some("bob"), None, None).await.unwrap();
            assert!(r["facts_written"].as_u64().unwrap() >= 1, "{r}");
            assert!(r.get("facts_error").is_none(), "{r}");
            let tid = r["turn_id"].as_i64().unwrap();
            let mut c2 = st.conn().await.unwrap();
            let now = Utc::now();
            let (a, b) = (pf(&st, "agy-webapp", "deployed_version", "1.0.0", "agy-webapp deployed_version is 1.0.0").await, pf(&st, "agy-webapp", "deployed_version", "2.0.0", "agy-webapp deployed_version is 2.0.0").await);
            st.write_fact(&mut c2, ns, &a, now, tid, None, None, None, &None).await.unwrap();
            let (_, n) = st.write_fact(&mut c2, ns, &b, now + chrono::Duration::seconds(3), tid, None, None, None, &None).await.unwrap();
            assert_eq!(n, 1);
            drop(c2);
            let rec = st.recall(ns, "agy-webapp deployed version", &RecallOpts::default()).await.unwrap();
            assert!(rec.get("degraded").is_none(), "{rec}");
            assert!(rec["context"].as_str().unwrap().contains("2.0.0"));
            let v = st.reconcile_claim(ns, "agy-webapp deployed_version is 1.0.0", Some("agy-webapp"), Some("deployed_version")).await.unwrap();
            assert_eq!(v["verdict"], "stale");
            assert!(st.knowledge_health(ns).await.unwrap()["score"].is_number());
            // 3) Same-dimension model family mismatch (`BAAI/bge-small-en-v1.5` vs incompatible 384-d model) MUST also fail loudly!
            if dim == 384 {
                let wrong_family = BrainStore::new(
                    pool.clone(),
                    Some(&schema),
                    Arc::new(Embedder::synthetic_for_test(384, "incompatible-384-model")),
                    Arc::new(Extractor::from_env(None)),
                )
                .unwrap();
                let merr = wrong_family
                    .init_schema()
                    .await
                    .expect_err("populated bge-small schema opened with incompatible 384-d model must fail loudly");
                assert!(merr.contains("model mismatch"), "{merr}");
            }
            c.batch_execute(&format!("DROP SCHEMA {} CASCADE", schema)).await.unwrap();
        }
    }

    /// Dedupe must never swallow a distinct fact or a reversal as a "restatement".
    #[tokio::test]
    #[ignore]
    async fn runtime_dedupe_boundaries() {
        let Ok(dsn) = std::env::var("MEMNOS_TEST_DSN") else {
            return;
        };
        quiet_env();
        let pool = test_pool(&dsn);
        let schema = format!("t_dedupe_{}", std::process::id());
        let st = BrainStore::new(pool.clone(), Some(&schema), Arc::new(Embedder::from_env(None, Some(384))), Arc::new(Extractor::from_env(None))).unwrap();
        st.init_schema().await.unwrap();
        let ns = "proj:auth-redesign";
        let tid = st.remember(ns, "seed turn", None, None, None, None, None, None).await.unwrap()["turn_id"].as_i64().unwrap();
        let mut c = st.conn().await.unwrap();
        let now = Utc::now();
        let long = |id: &str| format!("The payments-sync worker retries failed webhook deliveries with exponential backoff and posts the delivery results to {} every five minutes.", id);
        let (a, b) = (pf(&st, "payments-sync", "", "", &long("build-agent-17")).await, pf(&st, "payments-sync", "", "", &long("build-agent-18")).await);
        let dist = jq(&c, &format!("SELECT ($1::text::{v} <=> $2::text::{v})::float8 AS d", v = st.vt()), &[&vlit(&a.vec), &vlit(&b.vec)]).await.unwrap();
        eprintln!("distance between two long statements differing in one identifier: {}", dist[0]["d"]);
        let (ida, _) = st.write_fact(&mut c, ns, &a, now, tid, None, None, None, &None).await.unwrap();
        let (idb, _) = st.write_fact(&mut c, ns, &b, now + chrono::Duration::seconds(1), tid, None, None, None, &None).await.unwrap();
        assert!(ida.is_some() && idb.is_some(), "two statements differing in an identifier must both be stored");
        let blocked = pf(&st, "zeta", "", "", "The zeta deployment is blocked on the database migration.").await;
        let freed = pf(&st, "zeta", "", "", "The zeta deployment is no longer blocked on the database migration.").await;
        let (idc, _) = st.write_fact(&mut c, ns, &blocked, now, tid, None, None, None, &None).await.unwrap();
        let (idd, n) = st.write_fact(&mut c, ns, &freed, now + chrono::Duration::seconds(2), tid, None, None, None, &None).await.unwrap();
        assert!(idc.is_some() && idd.is_some(), "a reversal must not be eaten as a restatement");
        assert_eq!(n, 1, "the reversal must close out the blocked fact");
        drop(c);
        let conn = st.conn().await.unwrap();
        conn.batch_execute(&format!("DROP SCHEMA {} CASCADE", schema)).await.unwrap();
    }
}
