//! Native Rust Model Context Protocol (MCP) Server (`mcp.rs`).
//! Supports BOTH `stdio` (`memnos mcp`) and Streamable-HTTP (`POST /mcp` on `memnos serve`),
//! exposing all 55 MEMNOS MCP tools with <4 MB RSS, zero-touch terminal auto-registration,
//! 25s OS PID liveness heartbeats, offline queue fallback, and binary self-re-exec on upgrade.

use crate::code_anchors::{annotate_row_with_code_drift, extract_code_anchors};
use crate::config::{config_dir, load_config, server_url, session_token, Config, VERSION};
use crate::nsresolve::{
    clear_active_session, detect_terminal_session, is_local_pid_alive, resolve_with_source,
    save_active_session,
};
use crate::offline_queue;
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

#[derive(Clone)]
pub struct McpContext {
    pub url: String,
    pub token: String,
    pub default_ns: String,
    pub active_session: Arc<Mutex<String>>,
}

impl McpContext {
    pub fn from_config(cfg: &Config, ns_override: Option<String>) -> Self {
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let default_ns = ns_override
            .filter(|s| !s.trim().is_empty() && s != "auto")
            .unwrap_or_else(|| resolve_with_source(&cwd, None).0);
        let info = detect_terminal_session(&cwd);
        Self {
            url: server_url(cfg),
            token: session_token(cfg),
            default_ns,
            active_session: Arc::new(Mutex::new(info.name)),
        }
    }

    fn resolve_ns(&self, arg_ns: Option<&str>) -> String {
        if let Some(ns) = arg_ns {
            let t = ns.trim();
            if !t.is_empty() && t != "auto" {
                return t.to_string();
            }
        }
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let (resolved, src) = resolve_with_source(&cwd, None);
        if src != "default" {
            return resolved;
        }
        self.default_ns.clone()
    }

    fn resolve_sess(&self, explicit: Option<&str>) -> String {
        if let Some(s) = explicit {
            let t = s.trim().trim_start_matches('@');
            if !t.is_empty() {
                return t.to_string();
            }
        }
        if let Ok(g) = self.active_session.lock() {
            if !g.is_empty() {
                return g.clone();
            }
        }
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let from_file = crate::nsresolve::resolve_active_session_with_data(&cwd, None);
        if !from_file.is_empty() {
            return from_file;
        }
        detect_terminal_session(&cwd).name
    }

    fn post(&self, path: &str, body: &Value, timeout_secs: u64) -> Result<Value, String> {
        let endpoint = format!("{}{}", self.url.trim_end_matches('/'), path);
        let mut req = ureq::post(&endpoint)
            .timeout(Duration::from_secs(timeout_secs))
            .set("Content-Type", "application/json");
        if !self.token.is_empty() {
            req = req.set("Authorization", &format!("Bearer {}", self.token));
        }
        let mut payload = body.clone();
        if let Some(obj) = payload.as_object_mut() {
            if !obj.contains_key("actor_session") {
                let sess = self.resolve_sess(None);
                if !sess.is_empty() {
                    obj.insert("actor_session".to_string(), json!(sess));
                }
            }
        }
        match req.send_json(payload) {
            Ok(resp) => resp.into_json::<Value>().map_err(|e| e.to_string()),
            Err(ureq::Error::Status(code, resp)) => {
                let raw = resp.into_string().unwrap_or_default();
                if let Ok(j) = serde_json::from_str::<Value>(&raw) {
                    if let Some(err) = j.get("error").and_then(|v| v.as_str()) {
                        return Err(format!("HTTP {}: {}", code, err));
                    }
                }
                Err(format!("HTTP {}: {}", code, raw))
            }
            Err(e) => Err(format!("transport error: {}", e)),
        }
    }
}

fn tool_schema(name: &str, description: &str, props: Value, required: &[&str]) -> Value {
    json!({
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
        }
    })
}

pub fn all_tool_definitions() -> Vec<Value> {
    vec![
        tool_schema(
            "recall",
            "Recall relevant memories and pinned constraints for this namespace before answering.",
            json!({
                "query": {"type": "string", "description": "Focused search query"},
                "namespace": {"type": "string", "description": "Optional namespace override"},
                "memory_type": {"type": "string", "description": "Optional filter: decision|incident|constraint|skill|fact|handoff|fix"},
                "top_k": {"type": "integer", "description": "Max results (default 12)"}
            }),
            &["query"],
        ),
        tool_schema(
            "recall_wide",
            "Recall relevant memories across EVERY namespace your token is authorized to read.",
            json!({
                "query": {"type": "string", "description": "Search query"},
                "memory_type": {"type": "string", "description": "Optional type filter"}
            }),
            &["query"],
        ),
        tool_schema(
            "remember",
            "Save a durable fact, decision, incident, skill, handoff, or pinned constraint to long-term memory.",
            json!({
                "text": {"type": "string", "description": "Self-contained statement to remember"},
                "namespace": {"type": "string", "description": "Optional target namespace"},
                "memory_type": {"type": "string", "description": "decision|incident|constraint|skill|fact|handoff|fix"},
                "constraint_subject": {"type": "string", "description": "Grouping key when memory_type='constraint'"},
                "extract": {"type": "boolean", "description": "Run structured fact extraction (default true)"}
            }),
            &["text"],
        ),
        tool_schema(
            "record_fix",
            "Record a structured Symptom / Cause / Fix / Verify debugging memory with automatic git blob anchors.",
            json!({
                "symptom": {"type": "string"},
                "cause": {"type": "string"},
                "fix": {"type": "string"},
                "verify_cmd": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["symptom", "cause", "fix"],
        ),
        tool_schema(
            "consolidate",
            "Run entity resolution, duplicate fact deduplication, and episode consolidation for a namespace.",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "memory_search",
            "Structured search returning JSON memory rows with scores and provenance.",
            json!({
                "query": {"type": "string"},
                "namespace": {"type": "string"},
                "scope": {"type": "string"},
                "top_k": {"type": "integer"}
            }),
            &["query"],
        ),
        tool_schema(
            "memory_write",
            "Alias of remember accepting text and optional metadata.",
            json!({
                "text": {"type": "string"},
                "namespace": {"type": "string"},
                "memory_type": {"type": "string"}
            }),
            &["text"],
        ),
        tool_schema(
            "memory_delete",
            "Expire/retire a semantic fact or pinned constraint by id.",
            json!({
                "id": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["id"],
        ),
        tool_schema(
            "get_entity",
            "Fetch structured facts and mentions known about a named entity.",
            json!({
                "name": {"type": "string"},
                "depth": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["name"],
        ),
        tool_schema(
            "get_related",
            "Find entities co-mentioned or linked to a given entity in the knowledge graph.",
            json!({"name": {"type": "string"}, "namespace": {"type": "string"}}),
            &["name"],
        ),
        tool_schema(
            "graph_query",
            "Multi-hop entity-relationship traversal across the semantic graph.",
            json!({
                "entities": {"type": "array", "items": {"type": "string"}},
                "hops": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["entities"],
        ),
        tool_schema(
            "community_search",
            "Search entity clusters/communities matching a named entity.",
            json!({
                "name": {"type": "string"},
                "limit": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["name"],
        ),
        tool_schema(
            "check_contradictions",
            "List active contradictory claims in a namespace.",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "reconcile_claim",
            "Verify whether a local note/claim is stale against newer facts in memnos.",
            json!({
                "statement": {"type": "string"},
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["statement"],
        ),
        tool_schema(
            "knowledge_health",
            "Inspect memory health metrics (stale claims, orphan entities, fact density).",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "namespace_subscribe",
            "Register a subscription (with optional webhook URL) to receive namespace memory events.",
            json!({"webhook": {"type": "string"}, "namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "namespace_feed",
            "Poll recent events/turns in a namespace feed for a subscription.",
            json!({
                "subscription_id": {"type": "integer"},
                "limit": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["subscription_id"],
        ),
        tool_schema(
            "corpus_ingest",
            "Ingest an architectural/standards document into enforceable corpus constraints.",
            json!({
                "name": {"type": "string"},
                "text": {"type": "string"},
                "kind": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["name", "text"],
        ),
        tool_schema(
            "corpus_check",
            "Check code/design snippet against ingested corpus constraints (and ancestor standards).",
            json!({
                "snippet": {"type": "string"},
                "version": {"type": "string"},
                "inherit": {"type": "boolean"},
                "namespace": {"type": "string"}
            }),
            &["snippet"],
        ),
        tool_schema(
            "corpus_deviation",
            "Record an approved deviation from a corpus constraint.",
            json!({
                "constraint_id": {"type": "integer"},
                "rationale": {"type": "string"},
                "approved_by": {"type": "string"},
                "until": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["constraint_id", "rationale", "approved_by"],
        ),
        tool_schema(
            "corpus_check_diff",
            "Evaluate a git diff against corpus constraints.",
            json!({
                "diff": {"type": "string"},
                "name": {"type": "string"},
                "inherit": {"type": "boolean"},
                "version": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["diff"],
        ),
        tool_schema(
            "corpus_list",
            "List ingested corpus constraint sources in a namespace.",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "get_entity_dossier",
            "Retrieve the synthesized dossier for an entity.",
            json!({"entity": {"type": "string"}, "namespace": {"type": "string"}}),
            &["entity"],
        ),
        tool_schema(
            "ingest_file",
            "Ingest a document or text file into memory.",
            json!({
                "filename": {"type": "string"},
                "text": {"type": "string"},
                "extract": {"type": "boolean"},
                "namespace": {"type": "string"}
            }),
            &["filename", "text"],
        ),
        tool_schema(
            "segment_episodes",
            "Segment recent raw turns into episodic events.",
            json!({"gap_minutes": {"type": "integer"}, "namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "recall_episodes",
            "Search episodic event memories.",
            json!({"query": {"type": "string"}, "namespace": {"type": "string"}}),
            &["query"],
        ),
        tool_schema(
            "get_episode",
            "Fetch a single episodic event and its source raw turns.",
            json!({"id": {"type": "integer"}, "namespace": {"type": "string"}}),
            &["id"],
        ),
        tool_schema(
            "decay_episodes",
            "Apply salience decay to aged episodic memories.",
            json!({"half_life_days": {"type": "integer"}, "namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "copy_memories_from",
            "Copy or move matching memories from a source namespace into a target namespace.",
            json!({
                "src": {"type": "string"},
                "mode": {"type": "string"},
                "like": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["src"],
        ),
        tool_schema(
            "get_provenance",
            "Inspect source turn and observation timestamps for a semantic fact.",
            json!({"id": {"type": "integer"}, "namespace": {"type": "string"}}),
            &["id"],
        ),
        tool_schema(
            "get_context",
            "Assemble token-budgeted context block for a prompt.",
            json!({"query": {"type": "string"}, "budget_chars": {"type": "integer"}, "namespace": {"type": "string"}}),
            &["query"],
        ),
        tool_schema(
            "lease_acquire",
            "Atomically acquire a distributed coordination lease on a work item key.",
            json!({
                "key": {"type": "string"},
                "holder_id": {"type": "string"},
                "ttl_seconds": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["key", "holder_id"],
        ),
        tool_schema(
            "lease_heartbeat",
            "Extend an active coordination lease.",
            json!({
                "key": {"type": "string"},
                "holder_id": {"type": "string"},
                "ttl_seconds": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &["key", "holder_id"],
        ),
        tool_schema(
            "lease_release",
            "Release a held coordination lease.",
            json!({"key": {"type": "string"}, "holder_id": {"type": "string"}, "namespace": {"type": "string"}}),
            &["key", "holder_id"],
        ),
        tool_schema(
            "lease_who_holds",
            "Check which agent currently holds a lease key.",
            json!({"key": {"type": "string"}, "namespace": {"type": "string"}}),
            &["key"],
        ),
        tool_schema(
            "lease_list",
            "List active coordination leases in this namespace.",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "session_register",
            "Register this agent session under a human-readable name (auto-subscribes @name and #general).",
            json!({
                "name": {"type": "string"},
                "role": {"type": "string"},
                "harness": {"type": "string"},
                "model": {"type": "string"},
                "current_task": {"type": "string"},
                "channels": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
                "channel_ads": {"anyOf": [{"type": "object", "additionalProperties": {"type": "string"}}, {"type": "null"}]},
                "ttl_seconds": {"type": "integer"},
                "webhook": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["name"],
        ),
        tool_schema(
            "session_heartbeat",
            "Refresh session liveness and publish current_task updates.",
            json!({
                "name": {"type": "string"},
                "status": {"type": "string"},
                "current_task": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "session_list",
            "List active peer sessions (same-laptop vs remote, PID liveness, tasks, unread counts).",
            json!({"include_expired": {"type": "boolean"}, "namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "session_unregister",
            "Mark a session offline and release uncompleted claimed tasks.",
            json!({"name": {"type": "string"}, "namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "channel_publish",
            "Publish a message or claimable task to a channel (#topic, @session, status:<session>).",
            json!({
                "channel": {"type": "string"},
                "content": {"type": "string"},
                "intent": {"type": "string"},
                "sender_session": {"type": "string"},
                "remember": {"type": "boolean"},
                "reply_to": {"type": "integer"},
                "claimable": {"type": "boolean"},
                "namespace": {"type": "string"}
            }),
            &["channel", "content"],
        ),
        tool_schema(
            "channel_registry",
            "Join or create a global cross-namespace topic channel (e.g. '#arch-review' or 'payments sync').",
            json!({
                "topic": {"type": "string"},
                "task": {"type": "string"},
                "description": {"type": "string"},
                "from_beginning": {"type": "boolean"},
                "session_name": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["topic"],
        ),
        tool_schema(
            "channel_ack",
            "Complete ('done') or release ('release') a competing-consumer channel task claimed by this session.",
            json!({
                "message_id": {"type": "integer"},
                "action": {"type": "string"},
                "resolution": {"type": "string"},
                "session_name": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["message_id"],
        ),
        tool_schema(
            "channel_subscribe",
            "Subscribe this session to one or more channels.",
            json!({
                "channels": {"type": "array", "items": {"type": "string"}},
                "session_name": {"type": "string"},
                "from_beginning": {"type": "boolean"},
                "namespace": {"type": "string"}
            }),
            &["channels"],
        ),
        tool_schema(
            "channel_unsubscribe",
            "Unsubscribe from channels.",
            json!({
                "channels": {"type": "array", "items": {"type": "string"}},
                "session_name": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["channels"],
        ),
        tool_schema(
            "channel_poll",
            "Poll unread channel messages and competing-consumer tasks.",
            json!({
                "session_name": {"type": "string"},
                "channel": {"type": "string"},
                "peek": {"type": "boolean"},
                "limit": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "channel_wait",
            "Block via LISTEN/NOTIFY until an unread message or task arrives (up to 120s).",
            json!({
                "channel": {"type": "string"},
                "session_name": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "channel_list",
            "List channels and active peer sessions in this namespace.",
            json!({"namespace": {"type": "string"}}),
            &[],
        ),
        tool_schema(
            "inbox_send",
            "Queue a prioritized direct message in another session's inbox.",
            json!({
                "to_session": {"type": "string"},
                "content": {"type": "string"},
                "subject": {"type": "string"},
                "intent": {"type": "string"},
                "priority": {"type": "string"},
                "thread_key": {"type": "string"},
                "replaces_id": {"type": "integer"},
                "replace_pending": {"type": "boolean"},
                "sender_session": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["to_session", "content"],
        ),
        tool_schema(
            "inbox_recall",
            "Retract/unsend an inbox item or channel message you sent before the recipient acts on it.",
            json!({
                "message_id": {"type": "integer"},
                "channel_message_id": {"type": "integer"},
                "thread_key": {"type": "string"},
                "reason": {"type": "string"},
                "sender_session": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "inbox_retract",
            "Alias for inbox_recall: retract an unacted message you sent.",
            json!({
                "message_id": {"type": "integer"},
                "channel_message_id": {"type": "integer"},
                "thread_key": {"type": "string"},
                "reason": {"type": "string"},
                "sender_session": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "inbox_list",
            "Read items in this session's inbox (urgent first).",
            json!({
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "mark_read": {"type": "boolean"},
                "include_recalled": {"type": "boolean"},
                "limit": {"type": "integer"},
                "session_name": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "inbox_act",
            "Mark an inbox item read, in_progress, or acted/done (optionally sending a thread reply).",
            json!({
                "message_id": {"type": "integer"},
                "action": {"type": "string"},
                "resolution": {"type": "string"},
                "reply": {"type": "string"},
                "session_name": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["message_id"],
        ),
        tool_schema(
            "channel_cleanup",
            "Prune idle subscriptions, expired sessions, and old messages.",
            json!({
                "idle_days": {"type": "integer"},
                "message_retention_days": {"type": "integer"},
                "dry_run": {"type": "boolean"},
                "namespace": {"type": "string"}
            }),
            &[],
        ),
        tool_schema(
            "channel_advertise",
            "Advertise a channel's purpose and expected intents in the catalog.",
            json!({
                "channel": {"type": "string"},
                "description": {"type": "string"},
                "what_you_get": {"type": "string"},
                "intents": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
                "owner_session": {"type": "string"},
                "namespace": {"type": "string"}
            }),
            &["channel", "description"],
        ),
    ]
}

pub fn dispatch_tool_call(ctx: &McpContext, name: &str, args: &Value) -> Result<String, String> {
    let ns = ctx.resolve_ns(args.get("namespace").and_then(|v| v.as_str()));
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));

    match name {
        "recall" | "recall_wide" => {
            let query = args.get("query").and_then(|v| v.as_str()).unwrap_or("");
            let mut body = json!({"namespace": ns, "query": query});
            if name == "recall_wide" {
                body["scope"] = json!("all");
            }
            if let Some(mt) = args.get("memory_type").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                body["type"] = json!(mt);
            }
            if let Some(k) = args.get("top_k").and_then(|v| v.as_u64()) {
                body["top_k"] = json!(k);
            }
            match ctx.post("/recall", &body, 15) {
                Ok(mut out) => {
                    if let Some(arr) = out.get_mut("memories").and_then(|v| v.as_array_mut()) {
                        for row in arr.iter_mut() {
                            annotate_row_with_code_drift(row, &cwd);
                        }
                    }
                    let text = out
                        .get("context")
                        .and_then(|v| v.as_str())
                        .unwrap_or("(no relevant memories)")
                        .to_string();
                    offline_queue::save_snapshot(
                        &config_dir(),
                        &ns,
                        &text,
                        out.get("memories").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
                    );
                    Ok(if text.trim().is_empty() {
                        "(no relevant memories)".to_string()
                    } else {
                        text
                    })
                }
                Err(e) => {
                    if e.starts_with("transport error") {
                        if let Some(snap) = offline_queue::load_snapshot(&config_dir(), &ns) {
                            return Ok(format!(
                                "[STALE offline snapshot from {}]\n{}",
                                snap.saved_at, snap.context
                            ));
                        }
                    }
                    Ok(format!("(memnos {}: {})", name, e))
                }
            }
        }
        "remember" | "memory_write" => {
            let text = args
                .get("text")
                .or_else(|| args.get("content"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let anchors = extract_code_anchors(text, &cwd, 8);
            let mut body = json!({
                "namespace": ns,
                "text": text,
                "extract": args.get("extract").and_then(|v| v.as_bool()).unwrap_or(true),
            });
            if !anchors.is_empty() {
                body["code_anchors"] = serde_json::to_value(anchors).unwrap_or(json!([]));
            }
            if let Some(mt) = args.get("memory_type").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                body["type"] = json!(mt);
            }
            if let Some(cs) = args.get("constraint_subject").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                body["constraint_subject"] = json!(cs);
            }
            match ctx.post("/remember", &body, 30) {
                Ok(out) => {
                    let _ = offline_queue::drain(&config_dir(), &ctx.url, &ctx.token, 5);
                    let tid = out.get("turn_id").and_then(|v| v.as_i64()).unwrap_or(0);
                    Ok(format!("remembered in '{}' (turn {})", ns, tid))
                }
                Err(e) => {
                    if e.starts_with("transport error") {
                        offline_queue::enqueue(&config_dir(), &ns, text, "assistant", &ctx.token);
                        return Ok(format!(
                            "queued offline for '{}' (server unreachable; will replay automatically)",
                            ns
                        ));
                    }
                    Ok(format!("(memnos {}: {})", name, e))
                }
            }
        }
        "record_fix" => {
            let sym = args.get("symptom").and_then(|v| v.as_str()).unwrap_or("");
            let cause = args.get("cause").and_then(|v| v.as_str()).unwrap_or("");
            let fix = args.get("fix").and_then(|v| v.as_str()).unwrap_or("");
            let verify = args
                .get("verify_cmd")
                .or_else(|| args.get("verify"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let mut text = format!("SYMPTOM: {}\nCAUSE: {}\nFIX: {}", sym, cause, fix);
            if !verify.is_empty() {
                text.push_str(&format!("\nVERIFY: {}", verify));
            }
            let anchors = extract_code_anchors(&text, &cwd, 8);
            let mut body = json!({"namespace": ns, "text": text, "type": "fix"});
            if !anchors.is_empty() {
                body["code_anchors"] = serde_json::to_value(anchors).unwrap_or(json!([]));
            }
            match ctx.post("/remember", &body, 30) {
                Ok(out) => Ok(format!(
                    "recorded fix in '{}' (turn {})",
                    ns,
                    out.get("turn_id").and_then(|v| v.as_i64()).unwrap_or(0)
                )),
                Err(e) => Ok(format!("(memnos record_fix: {})", e)),
            }
        }
        "session_register" => {
            let info = detect_terminal_session(&cwd);
            let req_name = args
                .get("name")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or(&info.name);
            let mut body = json!({
                "namespace": ns,
                "name": req_name,
                "role": args.get("role").and_then(|v| v.as_str()).unwrap_or(""),
                "harness": args.get("harness").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(&info.harness),
                "model": args.get("model").and_then(|v| v.as_str()).unwrap_or(""),
                "current_task": args.get("current_task").and_then(|v| v.as_str()).unwrap_or(""),
                "channels": args.get("channels").filter(|v| !v.is_null()).cloned().unwrap_or(json!([])),
                "channel_ads": args.get("channel_ads").filter(|v| !v.is_null()).cloned().unwrap_or(json!({})),
                "ttl_seconds": args.get("ttl_seconds").and_then(|v| v.as_i64()).unwrap_or(1800),
                "host": info.host,
                "cwd": info.cwd,
                "machine_id": info.machine_id,
                "pid": info.pid,
                "tty": info.tty,
                "metadata": info.session_metadata(),
            });
            if let Some(wh) = args.get("webhook").and_then(|v| v.as_str()).filter(|s| !s.trim().is_empty()) {
                body["webhook"] = json!(wh);
            }
            match ctx.post("/session/register", &body, 10) {
                Ok(out) => {
                    if let Some(n) = out.get("name").and_then(|v| v.as_str()) {
                        if let Ok(mut g) = ctx.active_session.lock() {
                            *g = n.to_string();
                        }
                        save_active_session(n, &cwd, &ns);
                    }
                    Ok(serde_json::to_string(&out).unwrap_or_default())
                }
                Err(e) => Ok(format!("(memnos session_register: {})", e)),
            }
        }
        "session_heartbeat" => {
            let sname = ctx.resolve_sess(args.get("name").and_then(|v| v.as_str()));
            let mut body = json!({
                "namespace": ns,
                "name": sname,
                "status": args.get("status").and_then(|v| v.as_str()).unwrap_or("active"),
            });
            if let Some(ct) = args.get("current_task").and_then(|v| v.as_str()) {
                body["current_task"] = json!(ct);
            }
            ctx.post("/session/heartbeat", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos session_heartbeat: {})", e)))
        }
        "session_list" => {
            let body = json!({
                "namespace": ns,
                "include_expired": args.get("include_expired").and_then(|v| v.as_bool()).unwrap_or(false)
            });
            ctx.post("/session/list", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos session_list: {})", e)))
        }
        "session_unregister" => {
            let sname = ctx.resolve_sess(args.get("name").and_then(|v| v.as_str()));
            match ctx.post("/session/unregister", &json!({"namespace": ns, "name": sname}), 10) {
                Ok(out) => {
                    clear_active_session(&sname, &cwd);
                    Ok(serde_json::to_string(&out).unwrap_or_default())
                }
                Err(e) => Ok(format!("(memnos session_unregister: {})", e)),
            }
        }
        "channel_publish" => {
            let sname = ctx.resolve_sess(args.get("sender_session").and_then(|v| v.as_str()));
            let mut body = json!({
                "namespace": ns,
                "channel": args.get("channel").and_then(|v| v.as_str()).unwrap_or("#general"),
                "content": args.get("content").and_then(|v| v.as_str()).unwrap_or(""),
                "intent": args.get("intent").and_then(|v| v.as_str()).unwrap_or("message"),
                "sender_session": sname,
                "remember": args.get("remember").and_then(|v| v.as_bool()).unwrap_or(false),
                "claimable": args.get("claimable").and_then(|v| v.as_bool()).unwrap_or(false),
            });
            if let Some(rt) = args.get("reply_to").and_then(|v| v.as_i64()).filter(|&x| x > 0) {
                body["metadata"] = json!({"reply_to": rt});
            }
            ctx.post("/channel/publish", &body, 15)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_publish: {})", e)))
        }
        "channel_registry" => {
            let info = detect_terminal_session(&cwd);
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "topic": args.get("topic").and_then(|v| v.as_str()).unwrap_or(""),
                "task": args.get("task").and_then(|v| v.as_str()).unwrap_or(""),
                "description": args.get("description").and_then(|v| v.as_str()).unwrap_or(""),
                "from_beginning": args.get("from_beginning").and_then(|v| v.as_bool()).unwrap_or(false),
                "session_name": sname,
                "harness": info.harness,
                "host": info.host,
                "cwd": info.cwd,
                "machine_id": info.machine_id,
                "pid": info.pid,
                "tty": info.tty,
                "metadata": info.session_metadata(),
            });
            ctx.post("/channel/registry", &body, 15)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_registry: {})", e)))
        }
        "channel_ack" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "session_name": sname,
                "message_id": args.get("message_id").and_then(|v| v.as_i64()).unwrap_or(0),
                "action": args.get("action").and_then(|v| v.as_str()).unwrap_or("done"),
                "resolution": args.get("resolution").and_then(|v| v.as_str()).unwrap_or(""),
            });
            ctx.post("/channel/ack", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_ack: {})", e)))
        }
        "channel_subscribe" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "session_name": sname,
                "channels": args.get("channels").cloned().unwrap_or(json!([])),
                "from_beginning": args.get("from_beginning").and_then(|v| v.as_bool()).unwrap_or(false),
            });
            ctx.post("/channel/subscribe", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_subscribe: {})", e)))
        }
        "channel_unsubscribe" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "session_name": sname,
                "channels": args.get("channels").cloned().unwrap_or(json!([])),
            });
            ctx.post("/channel/unsubscribe", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_unsubscribe: {})", e)))
        }
        "channel_poll" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "session_name": sname,
                "channel": args.get("channel").and_then(|v| v.as_str()).unwrap_or(""),
                "peek": args.get("peek").and_then(|v| v.as_bool()).unwrap_or(false),
                "limit": args.get("limit").and_then(|v| v.as_i64()).unwrap_or(50),
            });
            ctx.post("/channel/poll", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_poll: {})", e)))
        }
        "channel_wait" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let t = args
                .get("timeout_seconds")
                .and_then(|v| v.as_u64())
                .unwrap_or(60)
                .clamp(1, 60);
            let body = json!({
                "namespace": ns,
                "session_name": sname,
                "channel": args.get("channel").and_then(|v| v.as_str()).unwrap_or(""),
                "timeout_seconds": t,
            });
            ctx.post("/channel/wait", &body, t + 15)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_wait: {})", e)))
        }
        "channel_list" => ctx
            .post("/channel/list", &json!({"namespace": ns}), 10)
            .map(|o| serde_json::to_string(&o).unwrap_or_default())
            .or_else(|e| Ok(format!("(memnos channel_list: {})", e))),
        "channel_cleanup" => {
            let body = json!({
                "namespace": ns,
                "idle_days": args.get("idle_days").and_then(|v| v.as_i64()).unwrap_or(7),
                "message_retention_days": args.get("message_retention_days").and_then(|v| v.as_i64()).unwrap_or(30),
                "dry_run": args.get("dry_run").and_then(|v| v.as_bool()).unwrap_or(false),
            });
            ctx.post("/channel/cleanup", &body, 15)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_cleanup: {})", e)))
        }
        "channel_advertise" => {
            let sname = ctx.resolve_sess(args.get("owner_session").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "channel": args.get("channel").and_then(|v| v.as_str()).unwrap_or(""),
                "description": args.get("description").and_then(|v| v.as_str()).unwrap_or(""),
                "what_you_get": args.get("what_you_get").and_then(|v| v.as_str()).unwrap_or(""),
                "intents": args.get("intents").filter(|v| !v.is_null()).cloned().unwrap_or(json!([])),
                "owner_session": sname,
            });
            ctx.post("/channel/advertise", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos channel_advertise: {})", e)))
        }
        "inbox_send" => {
            let sname = ctx.resolve_sess(args.get("sender_session").and_then(|v| v.as_str()));
            let mut body = json!({
                "namespace": ns,
                "to_session": args.get("to_session").and_then(|v| v.as_str()).unwrap_or(""),
                "content": args.get("content").and_then(|v| v.as_str()).unwrap_or(""),
                "subject": args.get("subject").and_then(|v| v.as_str()).unwrap_or(""),
                "intent": args.get("intent").and_then(|v| v.as_str()).unwrap_or("task"),
                "priority": args.get("priority").and_then(|v| v.as_str()).unwrap_or("normal"),
                "thread_key": args.get("thread_key").and_then(|v| v.as_str()).unwrap_or(""),
                "replace_pending": args.get("replace_pending").and_then(|v| v.as_bool()).unwrap_or(false),
                "sender_session": sname,
            });
            if let Some(rid) = args.get("replaces_id").and_then(|v| v.as_i64()).filter(|&x| x > 0) {
                body["replaces_id"] = json!(rid);
            }
            ctx.post("/inbox/send", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos inbox_send: {})", e)))
        }
        "inbox_recall" | "inbox_retract" => {
            let sname = ctx.resolve_sess(args.get("sender_session").and_then(|v| v.as_str()));
            let mut body = json!({
                "namespace": ns,
                "sender_session": sname,
                "thread_key": args.get("thread_key").and_then(|v| v.as_str()).unwrap_or(""),
                "reason": args.get("reason").and_then(|v| v.as_str()).unwrap_or(""),
            });
            if let Some(mid) = args.get("message_id").and_then(|v| v.as_i64()).filter(|&x| x > 0) {
                body["message_id"] = json!(mid);
            }
            if let Some(cmid) = args.get("channel_message_id").and_then(|v| v.as_i64()).filter(|&x| x > 0) {
                body["channel_message_id"] = json!(cmid);
            }
            ctx.post("/inbox/recall", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos {}: {})", name, e)))
        }
        "inbox_list" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "to_session": sname,
                "status": args.get("status").and_then(|v| v.as_str()).unwrap_or("active"),
                "priority": args.get("priority").and_then(|v| v.as_str()).unwrap_or(""),
                "mark_read": args.get("mark_read").and_then(|v| v.as_bool()).unwrap_or(false),
                "include_recalled": args.get("include_recalled").and_then(|v| v.as_bool()).unwrap_or(false),
                "limit": args.get("limit").and_then(|v| v.as_i64()).unwrap_or(50),
            });
            ctx.post("/inbox/list", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos inbox_list: {})", e)))
        }
        "inbox_act" => {
            let sname = ctx.resolve_sess(args.get("session_name").and_then(|v| v.as_str()));
            let body = json!({
                "namespace": ns,
                "to_session": sname,
                "message_id": args.get("message_id").and_then(|v| v.as_i64()).unwrap_or(0),
                "action": args.get("action").and_then(|v| v.as_str()).unwrap_or("acted"),
                "resolution": args.get("resolution").and_then(|v| v.as_str()).unwrap_or(""),
                "reply": args.get("reply").and_then(|v| v.as_str()).unwrap_or(""),
            });
            ctx.post("/inbox/act", &body, 10)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos inbox_act: {})", e)))
        }
        // Remaining governance, graph, episode, lease, and corpus tools map cleanly 1:1 to REST endpoints
        _ => {
            let route = match name {
                "consolidate" => "/consolidate",
                "memory_search" => "/memory/search",
                "memory_delete" => "/memory/delete",
                "get_entity" => "/entity",
                "get_related" => "/related",
                "graph_query" => "/graph",
                "community_search" => "/community",
                "check_contradictions" => "/contradictions",
                "reconcile_claim" => "/reconcile",
                "knowledge_health" => "/knowledge/health",
                "namespace_subscribe" => "/subscribe",
                "namespace_feed" => "/feed",
                "corpus_ingest" => "/corpus/ingest",
                "corpus_check" => "/corpus/check",
                "corpus_deviation" => "/corpus/deviation",
                "corpus_check_diff" => "/corpus/check_diff",
                "corpus_list" => "/corpus/list",
                "get_entity_dossier" => "/entity/dossier",
                "ingest_file" => "/ingest/file",
                "segment_episodes" => "/episode/segment",
                "recall_episodes" => "/episode/recall",
                "get_episode" => "/episode",
                "decay_episodes" => "/episode/decay",
                "copy_memories_from" => "/namespace/copy",
                "get_provenance" => "/provenance",
                "get_context" => "/memory/context",
                "lease_acquire" => "/lease/acquire",
                "lease_heartbeat" => "/lease/heartbeat",
                "lease_release" => "/lease/release",
                "lease_who_holds" => "/lease/who_holds",
                "lease_list" => "/lease/list",
                _ => return Err(format!("unknown MCP tool {:?}", name)),
            };
            let mut merged = args.clone();
            if let Some(obj) = merged.as_object_mut() {
                match name {
                    "get_entity" | "get_related" => {
                        if !obj.contains_key("name") {
                            if let Some(ent) = obj.get("entity").cloned() {
                                obj.insert("name".to_string(), ent);
                            }
                        }
                    }
                    "community_search" => {
                        if !obj.contains_key("name") {
                            if let Some(q) = obj.get("query").cloned() {
                                obj.insert("name".to_string(), q);
                            }
                        }
                        if let Some(lim) = obj.get("limit").cloned() {
                            obj.entry("max_nodes".to_string()).or_insert(lim);
                        }
                    }
                    "copy_memories_from" => {
                        if !obj.contains_key("src") {
                            if let Some(s) = obj.get("source_namespace").cloned() {
                                obj.insert("src".to_string(), s);
                            }
                        }
                        if let Some(dst) = obj.get("target_namespace").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                            obj.insert("namespace".to_string(), json!(dst));
                        }
                    }
                    "get_context" => {
                        if let Some(bc) = obj.get("budget_chars").cloned() {
                            obj.entry("max_chars".to_string()).or_insert(bc);
                        }
                    }
                    _ => {}
                }
                obj.entry("namespace".to_string()).or_insert_with(|| json!(ns));
            }
            ctx.post(route, &merged, 30)
                .map(|o| serde_json::to_string(&o).unwrap_or_default())
                .or_else(|e| Ok(format!("(memnos {}: {})", name, e)))
        }
    }
}

pub fn handle_jsonrpc_message(ctx: &McpContext, req: &Value) -> Option<Value> {
    let method = req.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let id = req.get("id").cloned();

    // Notifications have no `id` and expect no response
    if id.is_none() || method.starts_with("notifications/") {
        return None;
    }
    let id_val = id.unwrap_or(Value::Null);

    match method {
        "initialize" => Some(json!({
            "jsonrpc": "2.0",
            "id": id_val,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": false},
                    "resources": {"listChanged": false},
                    "prompts": {"listChanged": false}
                },
                "serverInfo": {
                    "name": "memnos",
                    "version": VERSION
                }
            }
        })),
        "ping" => Some(json!({"jsonrpc": "2.0", "id": id_val, "result": {}})),
        "tools/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": id_val,
            "result": {"tools": all_tool_definitions()}
        })),
        "resources/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": id_val,
            "result": {"resources": []}
        })),
        "prompts/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": id_val,
            "result": {"prompts": []}
        })),
        "tools/call" => {
            let params = req.get("params").cloned().unwrap_or(json!({}));
            let tname = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let targs = params.get("arguments").cloned().unwrap_or(json!({}));
            match dispatch_tool_call(ctx, tname, &targs) {
                Ok(text) => Some(json!({
                    "jsonrpc": "2.0",
                    "id": id_val,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": false
                    }
                })),
                Err(err) => Some(json!({
                    "jsonrpc": "2.0",
                    "id": id_val,
                    "result": {
                        "content": [{"type": "text", "text": err}],
                        "isError": true
                    }
                })),
            }
        }
        _ => Some(json!({
            "jsonrpc": "2.0",
            "id": id_val,
            "error": {"code": -32601, "message": format!("Method not found: {}", method)}
        })),
    }
}

fn spawn_session_lifecycle_watcher(ctx: McpContext) {
    if env::var("MEMNOS_MCP_AUTO_REGISTER").unwrap_or_else(|_| "1".to_string()) == "0" {
        return;
    }
    thread::spawn(move || {
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let info = detect_terminal_session(&cwd);
        let ns = ctx.resolve_ns(None);
        let mut registered_name = info.name.clone();
        if let Ok(resp) = ctx.post(
            "/session/register",
            &json!({
                "namespace": ns,
                "name": registered_name,
                "harness": info.harness,
                "host": info.host,
                "cwd": info.cwd,
                "machine_id": info.machine_id,
                "pid": info.pid,
                "tty": info.tty,
                "ttl_seconds": 120,
                "metadata": {
                    "auto_named": info.auto_named,
                    "display_label": info.display_label,
                    "suggested_topic": info.suggested_topic,
                }
            }),
            4,
        ) {
            if let Some(n) = resp.get("name").and_then(|v| v.as_str()) {
                registered_name = n.to_string();
                if let Ok(mut g) = ctx.active_session.lock() {
                    *g = registered_name.clone();
                }
            }
        }
        loop {
            thread::sleep(Duration::from_secs(25));
            if !is_local_pid_alive(info.pid) {
                if info.auto_named {
                    let _ = ctx.post(
                        "/session/unregister",
                        &json!({"namespace": ns, "name": registered_name}),
                        2,
                    );
                }
                return;
            }
            let cur_name = ctx
                .active_session
                .lock()
                .ok()
                .map(|g| g.clone())
                .unwrap_or_else(|| registered_name.clone());
            let _ = ctx.post(
                "/session/heartbeat",
                &json!({
                    "namespace": ctx.resolve_ns(None),
                    "name": cur_name,
                    "machine_id": info.machine_id,
                    "pid": info.pid,
                    "tty": info.tty,
                    "ttl_seconds": 120,
                    "publish_status": false
                }),
                4,
            );
        }
    });
}

fn spawn_binary_reexec_watcher() {
    if matches!(
        env::var("MEMNOS_ADAPTER_REEXEC")
            .unwrap_or_else(|_| "1".to_string())
            .to_ascii_lowercase()
            .as_str(),
        "0" | "false" | "no" | "off"
    ) {
        return;
    }
    #[cfg(unix)]
    thread::spawn(move || {
        let Ok(exe) = env::current_exe() else { return };
        let sig = |p: &PathBuf| -> Option<(u64, std::time::SystemTime)> {
            let m = fs::metadata(p).ok()?;
            Some((m.len(), m.modified().ok()?))
        };
        let Some(start_sig) = sig(&exe) else { return };
        let mut prev_sig = start_sig;
        loop {
            thread::sleep(Duration::from_secs(20));
            let Some(cur_sig) = sig(&exe) else { continue };
            if cur_sig == start_sig || cur_sig != prev_sig {
                prev_sig = cur_sig;
                continue;
            }
            use std::os::unix::process::CommandExt;
            let args: Vec<String> = env::args().skip(1).collect();
            let _ = std::process::Command::new(&exe).args(&args).exec();
            return;
        }
    });
}

pub fn run_stdio(ns_override: Option<String>) -> i32 {
    let cfg = load_config();
    let ctx = McpContext::from_config(&cfg, ns_override);
    spawn_session_lifecycle_watcher(ctx.clone());
    spawn_binary_reexec_watcher();

    let stdin = io::stdin();
    let mut stdout = io::stdout();
    for line in stdin.lock().lines() {
        let Ok(raw) = line else { break };
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(req) = serde_json::from_str::<Value>(trimmed) else {
            continue;
        };
        if let Some(resp) = handle_jsonrpc_message(&ctx, &req) {
            if let Ok(out_line) = serde_json::to_string(&resp) {
                let _ = writeln!(stdout, "{}", out_line);
                let _ = stdout.flush();
            }
        }
    }
    0
}
