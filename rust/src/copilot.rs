//! `copilot.rs` — Executive AI Briefing & Agent Coordination Chat ("Ask MEMNOS").
//!
//! Lets the workspace owner chat directly with MEMNOS using any installed CLI harness
//! (`Claude Code`, `Antigravity`, `Codex`, `Gemini`, `Cursor`, `Auto` with quota failover,
//! or the built-in instant digest engine) to:
//!   1. Understand everything that happened while away / asleep (facts, decisions, fixes,
//!      incidents, `#channel` threads, task claims, and `@inbox` handoffs).
//!   2. Inspect live multi-agent coordination without opening terminal harness controls.
//!   3. Manage `#channels`, `@handles`, tasks, quotas, and namespaces conversationally.

use crate::config::config_dir;
use crate::control::{Advert, Control, PublishOpts, SendOpts};
use crate::spawner::{
    discover_harnesses, extract_session_and_reply, kill_process_group, normalize_harness_key,
    read_bounded_line, structured_json_quota_error, summarize_stream_line, terminate_process_tree,
    write_private_file, API_KEY_ENV_VARS, SAFE_OS_ENV_ALLOWLIST,
};
use deadpool_postgres::Pool;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{broadcast, oneshot, Notify};

const COPILOT_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS memnos_control.copilot_threads (
    id              BIGSERIAL PRIMARY KEY,
    principal_id    BIGINT NOT NULL REFERENCES memnos_control.principals(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT 'New Conversation',
    namespace_scope TEXT NOT NULL DEFAULT '*',
    harness         TEXT NOT NULL DEFAULT 'auto',
    model           TEXT NOT NULL DEFAULT '',
    cli_session_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS copilot_threads_pid_upd_idx
    ON memnos_control.copilot_threads(principal_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS memnos_control.copilot_messages (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       BIGINT NOT NULL REFERENCES memnos_control.copilot_threads(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    harness         TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    steps           JSONB NOT NULL DEFAULT '[]'::jsonb,
    status          TEXT NOT NULL DEFAULT 'done',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS copilot_messages_thread_id_idx
    ON memnos_control.copilot_messages(thread_id, id ASC);
"#;

#[derive(Clone, Debug, Deserialize)]
pub struct CopilotSendRequest {
    pub thread_id: Option<i64>,
    pub message: String,
    #[serde(default = "default_auto")]
    pub harness: String,
    #[serde(default)]
    pub model: String,
    #[serde(default = "default_star")]
    pub namespace: String,
    #[serde(default = "default_window_hours")]
    pub window_hours: i64,
    #[serde(default)]
    pub wait: bool,
}

fn default_auto() -> String {
    "auto".to_string()
}

fn default_star() -> String {
    "*".to_string()
}

fn default_window_hours() -> i64 {
    8
}

pub struct CopilotEngine {
    schema_ready: AtomicBool,
    kill_senders: Mutex<HashMap<i64, oneshot::Sender<()>>>,
}

impl CopilotEngine {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            schema_ready: AtomicBool::new(false),
            kill_senders: Mutex::new(HashMap::new()),
        })
    }

    pub async fn ensure_schema(&self, pool: &Pool) -> Result<(), String> {
        if self.schema_ready.load(Ordering::Acquire) {
            return Ok(());
        }
        let c = pool.get().await.map_err(|e| e.to_string())?;
        c.batch_execute(COPILOT_DDL)
            .await
            .map_err(|e| format!("copilot schema init failed: {}", e))?;
        self.schema_ready.store(true, Ordering::Release);
        Ok(())
    }

    pub fn available_harnesses_payload() -> Vec<Value> {
        let discovered = discover_harnesses();
        let any_installed = discovered.iter().any(|h| h.installed);
        let mut out = vec![json!({
            "key": "auto",
            "label": "Auto (Best Available)",
            "installed": true,
            "recommended": true,
            "note": if any_installed {
                "Automatically selects an installed subscription harness with failover"
            } else {
                "Uses built-in MEMNOS digest & command engine"
            }
        })];
        for h in discovered {
            out.push(json!({
                "key": h.key,
                "label": h.label,
                "binary_name": h.binary_name,
                "installed": h.installed,
            }));
        }
        out.push(json!({
            "key": "builtin",
            "label": "Instant Digest (Built-in)",
            "installed": true,
            "note": "Zero-latency SQL briefing & direct coordination actions without external LLM calls"
        }));
        out
    }

    pub async fn list_threads(&self, pool: &Pool, pid: i64) -> Result<Value, String> {
        self.ensure_schema(pool).await?;
        let c = pool.get().await.map_err(|e| e.to_string())?;
        let rows = c
            .query(
                "SELECT t.id, t.title, t.namespace_scope, t.harness, t.model, t.created_at, t.updated_at,
                        (SELECT count(*) FROM memnos_control.copilot_messages m WHERE m.thread_id = t.id) AS message_count
                 FROM memnos_control.copilot_threads t
                 WHERE t.principal_id = $1
                 ORDER BY t.updated_at DESC
                 LIMIT 60",
                &[&pid],
            )
            .await
            .map_err(|e| e.to_string())?;

        let running_ids: Vec<i64> = self
            .kill_senders
            .lock()
            .unwrap()
            .keys()
            .copied()
            .collect();

        let threads: Vec<Value> = rows
            .into_iter()
            .map(|r| {
                let id: i64 = r.get("id");
                let created: chrono::DateTime<chrono::Utc> = r.get("created_at");
                let updated: chrono::DateTime<chrono::Utc> = r.get("updated_at");
                json!({
                    "id": id,
                    "title": r.get::<_, String>("title"),
                    "namespace_scope": r.get::<_, String>("namespace_scope"),
                    "harness": r.get::<_, String>("harness"),
                    "model": r.get::<_, String>("model"),
                    "message_count": r.get::<_, i64>("message_count"),
                    "running": running_ids.contains(&id),
                    "created_at": created.to_rfc3339(),
                    "updated_at": updated.to_rfc3339(),
                })
            })
            .collect();

        Ok(json!({
            "threads": threads,
            "harnesses": Self::available_harnesses_payload(),
            "running_thread_ids": running_ids,
        }))
    }

    pub async fn get_thread_messages(
        &self,
        pool: &Pool,
        pid: i64,
        thread_id: i64,
    ) -> Result<Value, String> {
        self.ensure_schema(pool).await?;
        let c = pool.get().await.map_err(|e| e.to_string())?;
        let trow = c
            .query_opt(
                "SELECT id, title, namespace_scope, harness, model, created_at, updated_at
                 FROM memnos_control.copilot_threads
                 WHERE id = $1 AND principal_id = $2",
                &[&thread_id, &pid],
            )
            .await
            .map_err(|e| e.to_string())?
            .ok_or_else(|| "NotFound: conversation thread not found".to_string())?;

        let running = self.kill_senders.lock().unwrap().contains_key(&thread_id);
        let created: chrono::DateTime<chrono::Utc> = trow.get("created_at");
        let updated: chrono::DateTime<chrono::Utc> = trow.get("updated_at");
        let thread_json = json!({
            "id": trow.get::<_, i64>("id"),
            "title": trow.get::<_, String>("title"),
            "namespace_scope": trow.get::<_, String>("namespace_scope"),
            "harness": trow.get::<_, String>("harness"),
            "model": trow.get::<_, String>("model"),
            "running": running,
            "created_at": created.to_rfc3339(),
            "updated_at": updated.to_rfc3339(),
        });

        let mrows = c
            .query(
                "SELECT id, role, harness, model, content, steps, status, created_at
                 FROM memnos_control.copilot_messages
                 WHERE thread_id = $1
                 ORDER BY id ASC
                 LIMIT 300",
                &[&thread_id],
            )
            .await
            .map_err(|e| e.to_string())?;

        let messages: Vec<Value> = mrows
            .into_iter()
            .map(|r| {
                let ts: chrono::DateTime<chrono::Utc> = r.get("created_at");
                json!({
                    "id": r.get::<_, i64>("id"),
                    "role": r.get::<_, String>("role"),
                    "harness": r.get::<_, String>("harness"),
                    "model": r.get::<_, String>("model"),
                    "content": r.get::<_, String>("content"),
                    "steps": r.get::<_, Value>("steps"),
                    "status": r.get::<_, String>("status"),
                    "created_at": ts.to_rfc3339(),
                })
            })
            .collect();

        Ok(json!({
            "thread": thread_json,
            "messages": messages,
        }))
    }

    pub async fn stop_thread(&self, pool: &Pool, pid: i64, thread_id: i64) -> Result<Value, String> {
        self.ensure_schema(pool).await?;
        let c = pool.get().await.map_err(|e| e.to_string())?;
        let owns = c
            .query_opt(
                "SELECT 1 FROM memnos_control.copilot_threads WHERE id = $1 AND principal_id = $2",
                &[&thread_id, &pid],
            )
            .await
            .map_err(|e| e.to_string())?
            .is_some();
        if !owns {
            return Err("NotFound: conversation thread not found".to_string());
        }
        let killed = if let Some(tx) = self.kill_senders.lock().unwrap().remove(&thread_id) {
            let _ = tx.send(());
            true
        } else {
            false
        };
        Ok(json!({ "ok": true, "thread_id": thread_id, "stopped": killed }))
    }

    pub async fn delete_thread(
        &self,
        pool: &Pool,
        pid: i64,
        thread_id: i64,
    ) -> Result<Value, String> {
        self.ensure_schema(pool).await?;
        if let Some(tx) = self.kill_senders.lock().unwrap().remove(&thread_id) {
            let _ = tx.send(());
        }
        let c = pool.get().await.map_err(|e| e.to_string())?;
        let n = c
            .execute(
                "DELETE FROM memnos_control.copilot_threads WHERE id = $1 AND principal_id = $2",
                &[&thread_id, &pid],
            )
            .await
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("NotFound: conversation thread not found".to_string());
        }
        Ok(json!({ "ok": true, "deleted": thread_id }))
    }

    pub async fn build_digest(
        pool: &Pool,
        pid: i64,
        namespace_scope: Option<&str>,
        window_hours: i64,
    ) -> Result<Value, String> {
        let hrs = window_hours.clamp(1, 720);
        let ns_clean = namespace_scope
            .map(str::trim)
            .filter(|s| !s.is_empty() && *s != "*");

        if let Some(target_ns) = ns_clean {
            let allowed = Control::authorize(pool, pid, target_ns, false)
                .await
                .map_err(|e| format!("{:?}", e))?;
            if !allowed {
                return Err(format!(
                    "Forbidden: read grant required on namespace '{}'",
                    target_ns
                ));
            }
        }

        let ov = Control::workspace_overview(pool, pid, ns_clean)
            .await
            .map_err(|e| format!("{:?}", e))?;

        let is_admin = ov.get("is_admin").and_then(|b| b.as_bool()).unwrap_or(false);
        let auth_namespaces: Vec<String> = match ns_clean {
            Some(explicit) => vec![explicit.to_string()],
            None => ov
                .get("namespaces")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|n| n.get("name").and_then(|x| x.as_str()))
                        .filter(|nm| !nm.starts_with("test:"))
                        .map(|s| s.to_string())
                        .collect()
                })
                .unwrap_or_default(),
        };

        let c = pool.get().await.map_err(|e| e.to_string())?;
        let cutoff = chrono::Utc::now() - chrono::Duration::hours(hrs);

        // 1. Recent semantic facts / decisions / fixes / incidents in window
        let mut recent_facts: Vec<Value> = Vec::new();
        let mut total_new_facts: i64 = 0;
        let mut decisions_and_fixes: i64 = 0;

        let tenant_exists: bool = c
            .query_one(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema = 'tenant_memnos' AND table_name = 'semantic')",
                &[],
            )
            .await
            .map(|r| r.get(0))
            .unwrap_or(false);

        if tenant_exists && (!auth_namespaces.is_empty() || (is_admin && ns_clean.is_none())) {
            let (cnt_row, fact_rows) = if is_admin && ns_clean.is_none() {
                let cr = c
                    .query_one(
                        "SELECT count(*),
                                count(*) FILTER (WHERE COALESCE(fact_type, '') IN ('decision', 'fix', 'incident', 'constraint', 'handoff'))
                         FROM tenant_memnos.semantic
                         WHERE valid_to IS NULL AND observed_at >= $1 AND namespace NOT LIKE 'test:%'",
                        &[&cutoff],
                    )
                    .await;
                let fr = c
                    .query(
                        "SELECT id, namespace, COALESCE(fact_type, 'fact') AS fact_type,
                                COALESCE(subject, '') AS subject, statement, observed_at
                         FROM tenant_memnos.semantic
                         WHERE valid_to IS NULL AND observed_at >= $1 AND namespace NOT LIKE 'test:%'
                         ORDER BY observed_at DESC
                         LIMIT 35",
                        &[&cutoff],
                    )
                    .await;
                (cr, fr)
            } else {
                let cr = c
                    .query_one(
                        "SELECT count(*),
                                count(*) FILTER (WHERE COALESCE(fact_type, '') IN ('decision', 'fix', 'incident', 'constraint', 'handoff'))
                         FROM tenant_memnos.semantic
                         WHERE valid_to IS NULL AND observed_at >= $1 AND namespace = ANY($2)",
                        &[&cutoff, &auth_namespaces],
                    )
                    .await;
                let fr = c
                    .query(
                        "SELECT id, namespace, COALESCE(fact_type, 'fact') AS fact_type,
                                COALESCE(subject, '') AS subject, statement, observed_at
                         FROM tenant_memnos.semantic
                         WHERE valid_to IS NULL AND observed_at >= $1 AND namespace = ANY($2)
                         ORDER BY observed_at DESC
                         LIMIT 35",
                        &[&cutoff, &auth_namespaces],
                    )
                    .await;
                (cr, fr)
            };

            if let Ok(r) = cnt_row {
                total_new_facts = r.get(0);
                decisions_and_fixes = r.get(1);
            }
            if let Ok(rows) = fact_rows {
                for r in rows {
                    let ts: chrono::DateTime<chrono::Utc> = r.get("observed_at");
                    recent_facts.push(json!({
                        "id": r.get::<_, i64>("id"),
                        "namespace": r.get::<_, String>("namespace"),
                        "type": r.get::<_, String>("fact_type"),
                        "subject": r.get::<_, String>("subject"),
                        "statement": r.get::<_, String>("statement"),
                        "observed_at": ts.to_rfc3339(),
                    }));
                }
            }
        }

        // 2. Recent #channel messages & task claims in window
        let mut recent_ch_msgs: Vec<Value> = Vec::new();
        let mut open_tasks: i64 = 0;
        let mut claimed_tasks: i64 = 0;
        let mut done_tasks: i64 = 0;

        if !auth_namespaces.is_empty() || (is_admin && ns_clean.is_none()) {
            let ch_rows = if is_admin && ns_clean.is_none() {
                c.query(
                    "SELECT id, namespace, channel, sender_session, intent, content,
                            claimable, claim_status, COALESCE(claimed_by, '') AS claimed_by,
                            COALESCE(resolution, '') AS resolution, created_at
                     FROM memnos_control.channel_messages
                     WHERE recalled_at IS NULL AND created_at >= $1 AND namespace NOT LIKE 'test:%'
                     ORDER BY created_at DESC
                     LIMIT 35",
                    &[&cutoff],
                )
                .await
            } else {
                c.query(
                    "SELECT id, namespace, channel, sender_session, intent, content,
                            claimable, claim_status, COALESCE(claimed_by, '') AS claimed_by,
                            COALESCE(resolution, '') AS resolution, created_at
                     FROM memnos_control.channel_messages
                     WHERE recalled_at IS NULL AND created_at >= $1 AND namespace = ANY($2)
                     ORDER BY created_at DESC
                     LIMIT 35",
                    &[&cutoff, &auth_namespaces],
                )
                .await
            };
            if let Ok(rows) = ch_rows {
                for r in rows {
                    let claimable: bool = r.get("claimable");
                    let cst: String = r.get("claim_status");
                    if claimable {
                        match cst.as_str() {
                            "done" => done_tasks += 1,
                            "claimed" => claimed_tasks += 1,
                            _ => open_tasks += 1,
                        }
                    }
                    let ts: chrono::DateTime<chrono::Utc> = r.get("created_at");
                    let content_s: String = r.get("content");
                    recent_ch_msgs.push(json!({
                        "id": r.get::<_, i64>("id"),
                        "namespace": r.get::<_, String>("namespace"),
                        "channel": r.get::<_, String>("channel"),
                        "sender": r.get::<_, String>("sender_session"),
                        "intent": r.get::<_, String>("intent"),
                        "claimable": claimable,
                        "claim_status": cst,
                        "claimed_by": r.get::<_, String>("claimed_by"),
                        "resolution": r.get::<_, String>("resolution"),
                        "content": content_s.chars().take(320).collect::<String>(),
                        "created_at": ts.to_rfc3339(),
                    }));
                }
            }
        }

        // 3. Recent @inbox DMs / handoffs in window
        let mut recent_inbox: Vec<Value> = Vec::new();
        if !auth_namespaces.is_empty() || (is_admin && ns_clean.is_none()) {
            let inb_rows = if is_admin && ns_clean.is_none() {
                c.query(
                    "SELECT id, namespace, from_session, to_session, intent, status,
                            COALESCE(resolution, '') AS resolution, content, created_at
                     FROM memnos_control.agent_inbox
                     WHERE recalled_at IS NULL AND created_at >= $1 AND namespace NOT LIKE 'test:%'
                     ORDER BY created_at DESC
                     LIMIT 25",
                    &[&cutoff],
                )
                .await
            } else {
                c.query(
                    "SELECT id, namespace, from_session, to_session, intent, status,
                            COALESCE(resolution, '') AS resolution, content, created_at
                     FROM memnos_control.agent_inbox
                     WHERE recalled_at IS NULL AND created_at >= $1 AND namespace = ANY($2)
                     ORDER BY created_at DESC
                     LIMIT 25",
                    &[&cutoff, &auth_namespaces],
                )
                .await
            };
            if let Ok(rows) = inb_rows {
                for r in rows {
                    let ts: chrono::DateTime<chrono::Utc> = r.get("created_at");
                    let content_s: String = r.get("content");
                    recent_inbox.push(json!({
                        "id": r.get::<_, i64>("id"),
                        "namespace": r.get::<_, String>("namespace"),
                        "from": r.get::<_, String>("from_session"),
                        "to": r.get::<_, String>("to_session"),
                        "intent": r.get::<_, String>("intent"),
                        "status": r.get::<_, String>("status"),
                        "resolution": r.get::<_, String>("resolution"),
                        "content": content_s.chars().take(280).collect::<String>(),
                        "created_at": ts.to_rfc3339(),
                    }));
                }
            }
        }

        // 4. Active work / file leases
        let mut active_leases: Vec<Value> = Vec::new();
        if !auth_namespaces.is_empty() || (is_admin && ns_clean.is_none()) {
            let l_rows = if is_admin && ns_clean.is_none() {
                c.query(
                    "SELECT namespace, key, holder_id, expires_at
                     FROM memnos_control.leases
                     WHERE expires_at > now() AND namespace NOT LIKE 'test:%'
                     ORDER BY expires_at ASC
                     LIMIT 25",
                    &[],
                )
                .await
            } else {
                c.query(
                    "SELECT namespace, key, holder_id, expires_at
                     FROM memnos_control.leases
                     WHERE expires_at > now() AND namespace = ANY($1)
                     ORDER BY expires_at ASC
                     LIMIT 25",
                    &[&auth_namespaces],
                )
                .await
            };
            if let Ok(rows) = l_rows {
                for r in rows {
                    let exp: chrono::DateTime<chrono::Utc> = r.get("expires_at");
                    active_leases.push(json!({
                        "namespace": r.get::<_, String>("namespace"),
                        "key": r.get::<_, String>("key"),
                        "holder": r.get::<_, String>("holder_id"),
                        "expires_at": exp.to_rfc3339(),
                    }));
                }
            }
        }

        let sessions = ov
            .get("sessions")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let online_handles: Vec<Value> = sessions
            .iter()
            .filter(|s| s.get("online").and_then(|b| b.as_bool()).unwrap_or(false))
            .take(35)
            .map(|s| {
                json!({
                    "name": s.get("name"),
                    "namespace": s.get("namespace"),
                    "harness": s.get("harness"),
                    "model": s.get("model"),
                    "current_task": s.get("current_task"),
                    "channels": s.get("channels"),
                    "quota_paused": s.get("quota_paused"),
                    "last_heartbeat_at": s.get("last_heartbeat_at"),
                })
            })
            .collect();

        let channels: Vec<Value> = ov
            .get("channels")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .take(35)
                    .map(|ch| {
                        let subs: Vec<String> = ch
                            .get("subscribers")
                            .and_then(|x| x.as_array())
                            .map(|sa| {
                                sa.iter()
                                    .filter_map(|item| {
                                        item.get("session_name")
                                            .or_else(|| item.get("name"))
                                            .and_then(|s| s.as_str())
                                            .or_else(|| item.as_str())
                                            .map(|s| format!("@{}", s))
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        json!({
                            "channel": ch.get("channel"),
                            "namespace": ch.get("namespace"),
                            "message_count": ch.get("message_count"),
                            "description": ch.get("description"),
                            "subscribers": subs,
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();

        Ok(json!({
            "scope": ns_clean.unwrap_or("*"),
            "window_hours": hrs,
            "generated_at": chrono::Utc::now().to_rfc3339(),
            "counts": {
                "new_facts": total_new_facts,
                "decisions_and_fixes": decisions_and_fixes,
                "channel_posts": recent_ch_msgs.len(),
                "open_tasks": open_tasks,
                "claimed_tasks": claimed_tasks,
                "completed_tasks": done_tasks,
                "inbox_messages": recent_inbox.len(),
                "online_handles": online_handles.len(),
                "total_handles": sessions.len(),
                "active_channels": channels.len(),
                "active_leases": active_leases.len(),
            },
            "online_handles": online_handles,
            "channels": channels,
            "active_leases": active_leases,
            "recent_channel_messages": recent_ch_msgs,
            "recent_inbox_messages": recent_inbox,
            "recent_facts": recent_facts,
        }))
    }

    pub async fn send_message(
        self: &Arc<Self>,
        pool: Pool,
        port: u16,
        pid: i64,
        event_tx: broadcast::Sender<Value>,
        channel_notify: Arc<Notify>,
        req: CopilotSendRequest,
    ) -> Result<Value, String> {
        self.ensure_schema(&pool).await?;
        let user_text = req.message.trim().to_string();
        if user_text.is_empty() {
            return Err("message cannot be empty".to_string());
        }
        let ns_scope = {
            let s = req.namespace.trim();
            if s.is_empty() {
                "*".to_string()
            } else {
                s.to_string()
            }
        };
        if ns_scope != "*" {
            let allowed = Control::authorize(&pool, pid, &ns_scope, false)
                .await
                .map_err(|e| format!("{:?}", e))?;
            if !allowed {
                return Err(format!(
                    "Forbidden: read grant required on namespace '{}'",
                    ns_scope
                ));
            }
        }

        let req_harness = {
            let h = req.harness.trim().to_ascii_lowercase();
            if h.is_empty() || h == "auto" {
                "auto".to_string()
            } else if h == "builtin" || h == "digest" {
                "builtin".to_string()
            } else {
                let norm = normalize_harness_key(&h);
                if norm.is_empty() {
                    return Err(format!("unknown harness '{}'", req.harness));
                }
                norm.to_string()
            }
        };

        let c = pool.get().await.map_err(|e| e.to_string())?;
        let (thread_id, title) = if let Some(tid) = req.thread_id.filter(|&id| id > 0) {
            let row = c
                .query_opt(
                    "UPDATE memnos_control.copilot_threads
                     SET harness = $3, model = $4, namespace_scope = $5, updated_at = now()
                     WHERE id = $1 AND principal_id = $2
                     RETURNING id, title",
                    &[&tid, &pid, &req_harness, &req.model.trim(), &ns_scope],
                )
                .await
                .map_err(|e| e.to_string())?
                .ok_or_else(|| "NotFound: conversation thread not found".to_string())?;
            (row.get::<_, i64>(0), row.get::<_, String>(1))
        } else {
            let auto_title = derive_thread_title(&user_text);
            let row = c
                .query_one(
                    "INSERT INTO memnos_control.copilot_threads(principal_id, title, namespace_scope, harness, model)
                     VALUES ($1, $2, $3, $4, $5)
                     RETURNING id, title",
                    &[&pid, &auto_title, &ns_scope, &req_harness, &req.model.trim()],
                )
                .await
                .map_err(|e| e.to_string())?;
            (row.get::<_, i64>(0), row.get::<_, String>(1))
        };

        if self.kill_senders.lock().unwrap().contains_key(&thread_id) {
            return Err("a response is already in progress on this conversation — wait or click Stop first".to_string());
        }

        // Load prior conversation turns for multi-turn context across harnesses
        let prior_rows = c
            .query(
                "SELECT role, content FROM memnos_control.copilot_messages
                 WHERE thread_id = $1 AND status = 'done'
                 ORDER BY id DESC LIMIT 8",
                &[&thread_id],
            )
            .await
            .unwrap_or_default();
        let mut prior_turns: Vec<(String, String)> = prior_rows
            .into_iter()
            .map(|r| (r.get::<_, String>(0), r.get::<_, String>(1)))
            .collect();
        prior_turns.reverse();

        let urow = c
            .query_one(
                "INSERT INTO memnos_control.copilot_messages(thread_id, role, harness, model, content, status)
                 VALUES ($1, 'user', '', '', $2, 'done')
                 RETURNING id, created_at",
                &[&thread_id, &user_text],
            )
            .await
            .map_err(|e| e.to_string())?;
        let user_msg_id: i64 = urow.get(0);
        let user_ts: chrono::DateTime<chrono::Utc> = urow.get(1);

        let arow = c
            .query_one(
                "INSERT INTO memnos_control.copilot_messages(thread_id, role, harness, model, content, status)
                 VALUES ($1, 'assistant', $2, $3, '', 'running')
                 RETURNING id, created_at",
                &[&thread_id, &req_harness, &req.model.trim()],
            )
            .await
            .map_err(|e| e.to_string())?;
        let asst_msg_id: i64 = arow.get(0);
        let asst_ts: chrono::DateTime<chrono::Utc> = arow.get(1);
        drop(c);

        let (kill_tx, kill_rx) = oneshot::channel::<()>();
        self.kill_senders.lock().unwrap().insert(thread_id, kill_tx);

        let (done_tx, done_rx) = oneshot::channel::<Value>();
        let eng = Arc::clone(self);
        let pool_bg = pool.clone();
        let ev_bg = event_tx.clone();
        let cn_bg = Arc::clone(&channel_notify);
        let model_req = req.model.trim().to_string();
        let window_hrs = req.window_hours.clamp(1, 720);
        let ns_for_bg = ns_scope.clone();
        let harness_for_bg = req_harness.clone();
        let prompt_for_bg = user_text.clone();

        tokio::spawn(async move {
            let result_val = execute_copilot_turn(
                eng,
                pool_bg,
                port,
                pid,
                thread_id,
                asst_msg_id,
                harness_for_bg,
                model_req,
                ns_for_bg,
                window_hrs,
                prior_turns,
                prompt_for_bg,
                ev_bg,
                cn_bg,
                kill_rx,
            )
            .await;
            let _ = done_tx.send(result_val);
        });

        if req.wait {
            let final_asst = done_rx.await.unwrap_or_else(|_| {
                json!({
                    "id": asst_msg_id,
                    "role": "assistant",
                    "status": "error",
                    "content": "Copilot turn interrupted.",
                })
            });
            return Ok(json!({
                "ok": true,
                "thread": {
                    "id": thread_id,
                    "title": title,
                    "namespace_scope": ns_scope,
                    "harness": req_harness,
                    "running": false,
                },
                "user_message": {
                    "id": user_msg_id,
                    "role": "user",
                    "content": user_text,
                    "status": "done",
                    "created_at": user_ts.to_rfc3339(),
                },
                "assistant_message": final_asst,
            }));
        }

        Ok(json!({
            "ok": true,
            "thread": {
                "id": thread_id,
                "title": title,
                "namespace_scope": ns_scope,
                "harness": req_harness,
                "running": true,
            },
            "user_message": {
                "id": user_msg_id,
                "role": "user",
                "content": user_text,
                "status": "done",
                "created_at": user_ts.to_rfc3339(),
            },
            "assistant_message": {
                "id": asst_msg_id,
                "role": "assistant",
                "harness": req_harness,
                "content": "",
                "steps": [],
                "status": "running",
                "created_at": asst_ts.to_rfc3339(),
            }
        }))
    }
}

fn derive_thread_title(msg: &str) -> String {
    let first_line = msg.lines().next().unwrap_or(msg).trim();
    if first_line.chars().count() <= 54 {
        first_line.to_string()
    } else {
        let mut s: String = first_line.chars().take(52).collect();
        s.push('…');
        s
    }
}

async fn execute_copilot_turn(
    eng: Arc<CopilotEngine>,
    pool: Pool,
    port: u16,
    pid: i64,
    thread_id: i64,
    asst_msg_id: i64,
    requested_harness: String,
    requested_model: String,
    ns_scope: String,
    window_hours: i64,
    prior_turns: Vec<(String, String)>,
    user_prompt: String,
    event_tx: broadcast::Sender<Value>,
    channel_notify: Arc<Notify>,
    mut kill_rx: oneshot::Receiver<()>,
) -> Value {
    let _ = event_tx.send(json!({
        "type": "activity",
        "op": "copilot/step",
        "target_principal_id": pid,
        "thread_id": thread_id,
        "message_id": asst_msg_id,
        "namespace": &ns_scope,
        "actor": "copilot",
        "detail": format!("Assembling {}h memory & agent coordination digest…", window_hours),
        "ok": true,
        "ts": chrono::Utc::now().to_rfc3339(),
    }));

    let digest = CopilotEngine::build_digest(
        &pool,
        pid,
        if ns_scope == "*" { None } else { Some(ns_scope.as_str()) },
        window_hours,
    )
    .await
    .unwrap_or_else(|e| json!({ "error": e }));

    let discovered = discover_harnesses();
    let candidate_harnesses: Vec<(String, String, PathBuf)> = if requested_harness == "builtin" {
        Vec::new()
    } else if requested_harness == "auto" {
        discovered
            .into_iter()
            .filter_map(|h| h.resolved_path.map(|p| (h.key, h.label, p)))
            .collect()
    } else {
        discovered
            .into_iter()
            .filter(|h| h.key == requested_harness)
            .filter_map(|h| h.resolved_path.map(|p| (h.key, h.label, p)))
            .collect()
    };

    let mut used_harness = "builtin".to_string();
    let mut final_text = String::new();
    let mut collected_steps: Vec<String> = Vec::new();
    let mut status = "done".to_string();

    if candidate_harnesses.is_empty() && requested_harness != "builtin" && requested_harness != "auto" {
        final_text = format!(
            "Harness `{}` is not installed in `PATH` on this machine. Switch the harness selector above to **Auto** or **Instant Digest (Built-in)**.",
            requested_harness
        );
        status = "error".to_string();
    } else if !candidate_harnesses.is_empty() {
        for (idx, (h_key, h_label, exe_path)) in candidate_harnesses.iter().enumerate() {
            used_harness = h_key.clone();
            let step_msg = format!("Consulting {} ({})…", h_label, h_key);
            collected_steps.push(step_msg.clone());
            let _ = event_tx.send(json!({
                "type": "activity",
                "op": "copilot/step",
                "target_principal_id": pid,
                "thread_id": thread_id,
                "message_id": asst_msg_id,
                "harness": h_key,
                "namespace": &ns_scope,
                "actor": "copilot",
                "detail": step_msg,
                "ok": true,
                "ts": chrono::Utc::now().to_rfc3339(),
            }));

            match run_single_harness_turn(
                &pool,
                port,
                pid,
                thread_id,
                asst_msg_id,
                h_key,
                exe_path,
                &requested_model,
                &ns_scope,
                window_hours,
                &digest,
                &prior_turns,
                &user_prompt,
                &event_tx,
                &mut collected_steps,
                &mut kill_rx,
            )
            .await
            {
                HarnessTurnOutcome::Success(reply) => {
                    final_text = reply;
                    status = "done".to_string();
                    break;
                }
                HarnessTurnOutcome::Stopped(partial) => {
                    final_text = if partial.trim().is_empty() {
                        "_(Stopped by user.)_".to_string()
                    } else {
                        format!("{}\n\n_(Stopped by user.)_", partial.trim())
                    };
                    status = "done".to_string();
                    break;
                }
                HarnessTurnOutcome::QuotaExceeded(reason) => {
                    collected_steps.push(format!("⚠ {} hit quota ({}) — failing over", h_label, reason));
                    if requested_harness != "auto" || idx + 1 >= candidate_harnesses.len() {
                        // Fall back cleanly to built-in digest + note so owner still gets their briefing!
                        let (builtin_md, mut b_steps) = run_builtin_turn(
                            &pool,
                            pid,
                            &ns_scope,
                            &digest,
                            &user_prompt,
                            &channel_notify,
                        )
                        .await;
                        collected_steps.append(&mut b_steps);
                        used_harness = "builtin".to_string();
                        final_text = format!(
                            "> **Note:** `{}` reported a subscription quota limit (`{}`), so MEMNOS generated this briefing directly via the built-in engine:\n\n{}",
                            h_label, reason, builtin_md
                        );
                        break;
                    }
                }
                HarnessTurnOutcome::Failed(err_msg) => {
                    if requested_harness == "auto" && idx + 1 < candidate_harnesses.len() {
                        collected_steps.push(format!("⚠ {} failed ({}) — trying next harness", h_label, err_msg));
                        continue;
                    }
                    let (builtin_md, mut b_steps) = run_builtin_turn(
                        &pool,
                        pid,
                        &ns_scope,
                        &digest,
                        &user_prompt,
                        &channel_notify,
                    )
                    .await;
                    collected_steps.append(&mut b_steps);
                    used_harness = "builtin".to_string();
                    final_text = format!(
                        "> **Note:** `{}` exited early (`{}`); switched to built-in MEMNOS digest:\n\n{}",
                        h_label, err_msg, builtin_md
                    );
                    break;
                }
            }
        }
    } else {
        let (builtin_md, mut b_steps) = run_builtin_turn(
            &pool,
            pid,
            &ns_scope,
            &digest,
            &user_prompt,
            &channel_notify,
        )
        .await;
        collected_steps.append(&mut b_steps);
        used_harness = "builtin".to_string();
        final_text = builtin_md;
    }

    eng.kill_senders.lock().unwrap().remove(&thread_id);

    let steps_json = json!(collected_steps);
    let created_iso = chrono::Utc::now().to_rfc3339();
    if let Ok(c) = pool.get().await {
        let _ = c
            .execute(
                "UPDATE memnos_control.copilot_messages
                 SET harness = $2, model = $3, content = $4, steps = $5, status = $6
                 WHERE id = $1",
                &[
                    &asst_msg_id,
                    &used_harness,
                    &requested_model,
                    &final_text,
                    &steps_json,
                    &status,
                ],
            )
            .await;
        let _ = c
            .execute(
                "UPDATE memnos_control.copilot_threads SET updated_at = now() WHERE id = $1",
                &[&thread_id],
            )
            .await;
    }

    channel_notify.notify_waiters();
    let _ = event_tx.send(json!({
        "type": "activity",
        "op": "copilot/done",
        "target_principal_id": pid,
        "thread_id": thread_id,
        "message_id": asst_msg_id,
        "harness": &used_harness,
        "status": &status,
        "content": &final_text,
        "steps": &steps_json,
        "namespace": &ns_scope,
        "actor": "copilot",
        "detail": "Briefing ready",
        "ok": status == "done",
        "ts": &created_iso,
    }));
    let _ = event_tx.send(json!({
        "type": "refresh",
        "namespace": "*",
        "ts": &created_iso,
    }));

    json!({
        "id": asst_msg_id,
        "role": "assistant",
        "harness": used_harness,
        "model": requested_model,
        "content": final_text,
        "steps": steps_json,
        "status": status,
        "created_at": created_iso,
    })
}

enum HarnessTurnOutcome {
    Success(String),
    Stopped(String),
    QuotaExceeded(String),
    Failed(String),
}

#[allow(clippy::too_many_arguments)]
async fn run_single_harness_turn(
    pool: &Pool,
    port: u16,
    pid: i64,
    thread_id: i64,
    asst_msg_id: i64,
    h_key: &str,
    exe_path: &PathBuf,
    model_trim: &str,
    ns_scope: &str,
    window_hours: i64,
    digest: &Value,
    prior_turns: &[(String, String)],
    user_prompt: &str,
    event_tx: &broadcast::Sender<Value>,
    collected_steps: &mut Vec<String>,
    kill_rx: &mut oneshot::Receiver<()>,
) -> HarnessTurnOutcome {
    // Mint a temporary token scoped to the exact caller `pid` and purge its row right after the turn
    let eph_tok = match Control::mint_token(pool, pid, Some("copilot-ephemeral"), Some(1)).await {
        Ok(t) => t,
        Err(e) => return HarnessTurnOutcome::Failed(format!("token mint failed: {:?}", e)),
    };

    let scratch_dir = config_dir().join("copilot-workspace");
    let _ = std::fs::create_dir_all(&scratch_dir);

    let default_ns = if ns_scope == "*" || ns_scope.is_empty() {
        digest
            .get("channels")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .and_then(|c| c.get("namespace").and_then(|x| x.as_str()))
            .unwrap_or("default")
    } else {
        ns_scope
    };

    let mut mcp_cfg_file: Option<PathBuf> = None;
    if h_key == "claude-code" {
        let tmp_dir = config_dir().join("tmp");
        let _ = std::fs::create_dir_all(&tmp_dir);
        let cfg_path = tmp_dir.join(format!("mcp-copilot-{}-{}.json", thread_id, asst_msg_id));
        let cfg_payload = json!({
            "mcpServers": {
                "memnos": {
                    "type": "http",
                    "url": format!("http://127.0.0.1:{}/mcp", port),
                    "headers": {
                        "Authorization": format!("Bearer {}", eph_tok),
                        "X-Memnos-Namespace": default_ns,
                    }
                }
            }
        });
        if let Ok(raw) = serde_json::to_vec_pretty(&cfg_payload) {
            if write_private_file(&cfg_path, &raw).is_ok() {
                mcp_cfg_file = Some(cfg_path);
            }
        }
    }

    let full_prompt = build_copilot_prompt(ns_scope, window_hours, digest, prior_turns, user_prompt);

    let mut cmd = Command::new(exe_path);
    cmd.current_dir(&scratch_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .env_clear();

    #[cfg(unix)]
    {
        cmd.process_group(0);
    }

    for k in SAFE_OS_ENV_ALLOWLIST {
        if let Some(val) = env::var_os(k) {
            cmd.env(k, val);
        }
    }
    for k in API_KEY_ENV_VARS {
        if let Some(val) = env::var_os(k) {
            cmd.env(k, val);
        }
    }

    // Ensure the running `memnos` binary directory is first on PATH so CLI actions always succeed
    let mut path_entries: Vec<PathBuf> = Vec::new();
    if let Ok(cur_exe) = env::current_exe() {
        if let Some(p) = cur_exe.parent() {
            path_entries.push(p.to_path_buf());
        }
    }
    path_entries.push(config_dir().join("bin"));
    if let Some(existing_path) = env::var_os("PATH") {
        path_entries.extend(env::split_paths(&existing_path));
    }
    if let Ok(joined) = env::join_paths(path_entries) {
        cmd.env("PATH", joined);
    }

    cmd.env("TERM", "dumb")
        .env("NO_COLOR", "1")
        .env("MEMNOS_HEADLESS", "1")
        .env("MEMNOS_SESSION", "copilot")
        .env("MEMNOS_NS", default_ns)
        .env("MEMNOS_URL", format!("http://127.0.0.1:{}", port))
        .env("MEMNOS_TOKEN", &eph_tok);

    let mut pipe_stdin = false;
    match h_key {
        "claude-code" => {
            pipe_stdin = true;
            cmd.args([
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--settings",
                r#"{"hooks":{}}"#,
                "--dangerously-skip-permissions",
            ]);
            if let Some(ref mp) = mcp_cfg_file {
                cmd.args(["--mcp-config", &mp.to_string_lossy(), "--strict-mcp-config"]);
            }
            if !model_trim.is_empty() {
                cmd.args(["--model", model_trim]);
            }
        }
        "antigravity" => {
            cmd.arg("--output-format=stream-json")
                .arg("--dangerously-skip-permissions");
            if !model_trim.is_empty() {
                cmd.arg(format!("--model={}", model_trim));
            }
            cmd.arg(format!("--print={}", full_prompt));
        }
        "codex" => {
            pipe_stdin = true;
            cmd.args(["exec", "--json", "--full-auto", "--skip-git-repo-check"]);
            if !model_trim.is_empty() {
                cmd.args(["--model", model_trim]);
            }
            cmd.arg("-");
        }
        "gemini" => {
            cmd.args(["--output-format", "stream-json", "--yolo"]);
            if !model_trim.is_empty() {
                cmd.args(["--model", model_trim]);
            }
            cmd.args(["-p", &full_prompt]);
        }
        "cursor" => {
            cmd.args(["--output-format", "stream-json"]);
            if !model_trim.is_empty() {
                cmd.args(["--model", model_trim]);
            }
            cmd.args(["-p", &full_prompt]);
        }
        _ => {}
    }

    if !pipe_stdin {
        cmd.stdin(Stdio::null());
    }

    let mut child = match cmd.spawn() {
        Ok(ch) => ch,
        Err(e) => {
            cleanup_ephemeral_token(pool, &eph_tok, mcp_cfg_file.as_ref()).await;
            return HarnessTurnOutcome::Failed(format!("spawn error: {}", e));
        }
    };

    if pipe_stdin {
        if let Some(mut cin) = child.stdin.take() {
            let bytes = full_prompt.into_bytes();
            tokio::spawn(async move {
                let _ = cin.write_all(&bytes).await;
                let _ = cin.shutdown().await;
            });
        }
    }

    let child_pid = child.id();
    let stdout_opt = child.stdout.take();
    let stderr_opt = child.stderr.take();

    let sid_slot = Arc::new(Mutex::new(Option::<String>::None));
    let reply_slot = Arc::new(Mutex::new(String::new()));
    let quota_slot = Arc::new(Mutex::new(Option::<String>::None));
    let steps_slot = Arc::new(Mutex::new(Vec::<String>::new()));

    let sid_c = Arc::clone(&sid_slot);
    let reply_c = Arc::clone(&reply_slot);
    let quota_c = Arc::clone(&quota_slot);
    let steps_c = Arc::clone(&steps_slot);
    let ev_c = event_tx.clone();
    let ns_c = ns_scope.to_string();
    let hk_c = h_key.to_string();

    let mut out_task = tokio::spawn(async move {
        let Some(out) = stdout_opt else { return };
        let mut reader = BufReader::new(out);
        let mut last_emitted_len = 0usize;
        while let Some(line) = read_bounded_line(&mut reader, 65_536).await {
            if let Some(q_err) = structured_json_quota_error(&line) {
                let mut g = quota_c.lock().unwrap();
                if g.is_none() {
                    *g = Some(q_err);
                }
            }
            extract_session_and_reply(&line, &sid_c, &reply_c);
            let cur_reply = reply_c.lock().unwrap().clone();
            if cur_reply.len() != last_emitted_len && !cur_reply.is_empty() {
                last_emitted_len = cur_reply.len();
                let _ = ev_c.send(json!({
                    "type": "activity",
                    "op": "copilot/delta",
                    "target_principal_id": pid,
                    "thread_id": thread_id,
                    "message_id": asst_msg_id,
                    "harness": &hk_c,
                    "content": cur_reply,
                    "namespace": &ns_c,
                    "actor": "copilot",
                    "ok": true,
                    "ts": chrono::Utc::now().to_rfc3339(),
                }));
            }
            if let Some(step) = summarize_stream_line(&line) {
                {
                    let mut st = steps_c.lock().unwrap();
                    if st.len() < 40 {
                        st.push(step.clone());
                    }
                }
                let _ = ev_c.send(json!({
                    "type": "activity",
                    "op": "copilot/step",
                    "target_principal_id": pid,
                    "thread_id": thread_id,
                    "message_id": asst_msg_id,
                    "harness": &hk_c,
                    "namespace": &ns_c,
                    "actor": "copilot",
                    "detail": step,
                    "ok": true,
                    "ts": chrono::Utc::now().to_rfc3339(),
                }));
            }
        }
    });

    let stderr_buf = Arc::new(Mutex::new(String::new()));
    let stderr_c = Arc::clone(&stderr_buf);
    let mut err_task = tokio::spawn(async move {
        let Some(err) = stderr_opt else { return };
        let mut reader = BufReader::new(err);
        while let Some(line) = read_bounded_line(&mut reader, 8_192).await {
            let trimmed = line.trim();
            if !trimmed.is_empty() {
                let mut g = stderr_c.lock().unwrap();
                if g.len() < 600 {
                    if !g.is_empty() {
                        g.push(' ');
                    }
                    g.push_str(trimmed);
                }
            }
        }
    });

    let mut stopped = false;
    let mut timed_out = false;
    let exit_code = tokio::select! {
        res = child.wait() => res.ok().and_then(|s| s.code()),
        _ = kill_rx => {
            stopped = true;
            terminate_process_tree(&mut child, child_pid).await;
            None
        }
        _ = tokio::time::sleep(Duration::from_secs(180)) => {
            timed_out = true;
            terminate_process_tree(&mut child, child_pid).await;
            None
        }
    };

    kill_process_group(child_pid);
    if tokio::time::timeout(Duration::from_secs(2), &mut out_task).await.is_err() {
        out_task.abort();
    }
    if tokio::time::timeout(Duration::from_secs(1), &mut err_task).await.is_err() {
        err_task.abort();
    }

    cleanup_ephemeral_token(pool, &eph_tok, mcp_cfg_file.as_ref()).await;

    let extra_steps = steps_slot.lock().unwrap().clone();
    collected_steps.extend(extra_steps);

    let reply = reply_slot.lock().unwrap().trim().to_string();
    if stopped {
        return HarnessTurnOutcome::Stopped(reply);
    }
    if timed_out {
        return HarnessTurnOutcome::Failed("timed out after 180s".to_string());
    }
    if let Some(q) = quota_slot.lock().unwrap().clone() {
        return HarnessTurnOutcome::QuotaExceeded(q);
    }
    if !reply.is_empty() {
        return HarnessTurnOutcome::Success(reply);
    }
    let err_txt = stderr_buf.lock().unwrap().chars().take(160).collect::<String>();
    HarnessTurnOutcome::Failed(if err_txt.is_empty() {
        format!("exit code {:?}", exit_code)
    } else {
        err_txt
    })
}

async fn cleanup_ephemeral_token(pool: &Pool, tok: &str, mcp_file: Option<&PathBuf>) {
    if let Some(p) = mcp_file {
        let _ = std::fs::remove_file(p);
    }
    let _ = Control::revoke_token(pool, tok).await;
    if let Ok(c) = pool.get().await {
        let _ = c
            .execute(
                "DELETE FROM memnos_control.api_tokens WHERE label = 'copilot-ephemeral' AND revoked = true",
                &[],
            )
            .await;
    }
}

fn build_copilot_prompt(
    ns_scope: &str,
    window_hours: i64,
    digest: &Value,
    prior_turns: &[(String, String)],
    user_prompt: &str,
) -> String {
    let mut history_block = String::new();
    if !prior_turns.is_empty() {
        history_block.push_str("\n## Prior Conversation Turns\n");
        for (role, text) in prior_turns {
            let tag = if role == "user" { "Owner" } else { "MEMNOS Copilot" };
            let clipped: String = text.chars().take(1200).collect();
            history_block.push_str(&format!("**{}:** {}\n\n", tag, clipped));
        }
    }

    let digest_pretty = serde_json::to_string_pretty(digest).unwrap_or_else(|_| "{}".to_string());

    format!(
        "You are **MEMNOS Copilot**, the executive AI briefing and multi-agent coordination assistant built into the MEMNOS Desktop Workspace.\n\
         The owner uses you to:\n\
         1. Catch up effortlessly on what happened while they were asleep or away (decisions, fixes, incidents, `#channel` conversations, task claims, `@inbox` handoffs).\n\
         2. Understand how attached `@handles` (agents/sessions across harnesses) are coordinating right now without opening terminal controls.\n\
         3. Manage `#channels`, `@handles`, tasks, quotas, and `namespaces` directly via chat.\n\n\
         ## Ground Rules\n\
         - **Answer directly from the Live Digest below whenever it already contains the facts/messages/handles needed** — keep responses crisp, structured with clear Markdown headings/bullets, and always cite exact `#channel`, `@handle`, and `namespace` names.\n\
         - If the owner asks about older history beyond the {window_hours}h digest, run `memnos recall \"<query>\" --namespace <ns>` (or use `recall` / `recall_wide` MCP tools if attached).\n\
         - **When the owner asks to perform a workspace action**, execute it immediately using the `memnos` CLI (`MEMNOS_URL` and `MEMNOS_TOKEN` are already set in your environment):\n\
           • Create channel: `memnos channel create \"#name\" -d \"desc\" --namespace <ns>`\n\
           • Delete channel: `memnos channel rm \"#name\" --namespace <ns>`\n\
           • Subscribe handle: `memnos channel sub \"#name\" --session <handle> --namespace <ns>`\n\
           • Unsubscribe handle: `memnos channel unsub \"#name\" --session <handle> --namespace <ns>`\n\
           • Broadcast or post claimable task: `memnos channel pub \"#name\" \"<msg>\" --intent task --claimable --session owner --namespace <ns>`\n\
           • Send direct message / task to handle: `memnos inbox send <handle> \"<msg>\" --intent task --session owner --namespace <ns>`\n\
           • Rename / unregister / quota-pause handle: `memnos session rename <old> <new> --namespace <ns>`, `memnos session rm <handle> --namespace <ns>`, `memnos session quota <handle> --pause|--resume --namespace <ns>`\n\
           • Create / merge / inspect namespaces: `memnos namespace add <ns> --desc \"...\"`, `memnos namespace merge <src> <dst> --dry-run`\n\
           • Health diagnostics: `memnos doctor --json`\n\
         - Never modify external source repositories unless explicitly asked.\n\n\
         ## Live MEMNOS Digest (Scope: `{ns_scope}`, Window: Last {window_hours}h)\n\
         ```json\n\
         {digest_pretty}\n\
         ```\n\
         {history_block}\n\
         ## Owner's Request\n\
         {user_prompt}"
    )
}

async fn run_builtin_turn(
    pool: &Pool,
    pid: i64,
    ns_scope: &str,
    digest: &Value,
    user_prompt: &str,
    channel_notify: &Arc<Notify>,
) -> (String, Vec<String>) {
    let mut steps: Vec<String> = Vec::new();
    let mut action_notes: Vec<String> = Vec::new();
    let lower = user_prompt.to_lowercase();

    let default_ns = if ns_scope != "*" && !ns_scope.is_empty() {
        ns_scope.to_string()
    } else {
        digest
            .get("channels")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .and_then(|c| c.get("namespace").and_then(|x| x.as_str()))
            .or_else(|| {
                digest
                    .get("online_handles")
                    .and_then(|v| v.as_array())
                    .and_then(|a| a.first())
                    .and_then(|s| s.get("namespace").and_then(|x| x.as_str()))
            })
            .unwrap_or("default")
            .to_string()
    };

    let target_ns = extract_ns_mention(user_prompt).unwrap_or(default_ns);
    let ch_token = extract_prefixed_token(user_prompt, '#');
    let hd_token = extract_prefixed_token(user_prompt, '@');

    // Direct built-in coordination execution when requested!
    if (lower.contains("create channel") || lower.starts_with("create #")) && ch_token.is_some() {
        let ch = ch_token.clone().unwrap();
        if Control::authorize(pool, pid, &target_ns, true).await.unwrap_or(false) {
            let ad = Advert {
                owner_session: "owner",
                description: "Created via Ask MEMNOS",
                what_you_get: "",
                intents: None,
                metadata: None,
            };
            if Control::channel_advertise(pool, &target_ns, &ch, pid, ad)
                .await
                .is_ok()
            {
                channel_notify.notify_waiters();
                steps.push(format!("Created `{}` in `{}`", ch, target_ns));
                action_notes.push(format!("✅ Created channel **`{}`** in namespace **`{}`**.", ch, target_ns));
            }
        } else {
            action_notes.push(format!("⚠️ Write grant required on `{}` to create `{}`.", target_ns, ch));
        }
    } else if (lower.contains("delete channel") || lower.contains("remove channel")) && ch_token.is_some() {
        let ch = ch_token.clone().unwrap();
        match Control::workspace_delete_channel(pool, pid, &target_ns, &ch).await {
            Ok(_) => {
                channel_notify.notify_waiters();
                steps.push(format!("Deleted `{}` from `{}`", ch, target_ns));
                action_notes.push(format!("✅ Deleted channel **`{}`** from **`{}`**.", ch, target_ns));
            }
            Err(e) => action_notes.push(format!("⚠️ Could not delete `{}`: {:?}", ch, e)),
        }
    } else if lower.contains("unsubscribe") && ch_token.is_some() && hd_token.is_some() {
        let ch = ch_token.clone().unwrap();
        let hd = hd_token.clone().unwrap();
        let clean_hd = hd.trim_start_matches('@');
        match Control::workspace_manage_subscription(pool, pid, &target_ns, &ch, clean_hd, false).await {
            Ok(_) => {
                channel_notify.notify_waiters();
                steps.push(format!("Unsubscribed `{}` from `{}`", hd, ch));
                action_notes.push(format!("✅ Unsubscribed **`{}`** from **`{}`** in `{}`.", hd, ch, target_ns));
            }
            Err(e) => action_notes.push(format!("⚠️ Unsubscribe failed: {:?}", e)),
        }
    } else if lower.contains("subscribe") && ch_token.is_some() && hd_token.is_some() {
        let ch = ch_token.clone().unwrap();
        let hd = hd_token.clone().unwrap();
        let clean_hd = hd.trim_start_matches('@');
        match Control::workspace_manage_subscription(pool, pid, &target_ns, &ch, clean_hd, true).await {
            Ok(_) => {
                channel_notify.notify_waiters();
                steps.push(format!("Subscribed `{}` to `{}`", hd, ch));
                action_notes.push(format!("✅ Subscribed **`{}`** to **`{}`** in `{}`.", hd, ch, target_ns));
            }
            Err(e) => action_notes.push(format!("⚠️ Subscribe failed: {:?}", e)),
        }
    } else if lower.contains("unregister") && hd_token.is_some() {
        let hd = hd_token.clone().unwrap();
        let clean_hd = hd.trim_start_matches('@');
        match Control::workspace_delete_session(pool, pid, &target_ns, clean_hd).await {
            Ok(_) => {
                channel_notify.notify_waiters();
                steps.push(format!("Unregistered `{}` in `{}`", hd, target_ns));
                action_notes.push(format!("✅ Unregistered handle **`{}`** from `{}`.", hd, target_ns));
            }
            Err(e) => action_notes.push(format!("⚠️ Could not unregister `{}`: {:?}", hd, e)),
        }
    } else if let Some((_, payload)) = user_prompt.split_once(':') {
        let body_txt = payload.trim();
        if !body_txt.is_empty() {
            if (lower.starts_with("post ") || lower.starts_with("publish ") || lower.starts_with("broadcast ")) && ch_token.is_some() {
                let ch = ch_token.unwrap();
                if Control::authorize(pool, pid, &target_ns, true).await.unwrap_or(false) {
                    let claimable = lower.contains("task");
                    let opts = PublishOpts {
                        intent: if claimable { "task".to_string() } else { "message".to_string() },
                        metadata: json!({}),
                        description: String::new(),
                        what_you_get: String::new(),
                        claimable,
                        reply_to: None,
                    };
                    if Control::channel_publish(pool, &target_ns, &ch, "owner", pid, body_txt, &opts).await.is_ok() {
                        channel_notify.notify_waiters();
                        steps.push(format!("Published to `{}` in `{}`", ch, target_ns));
                        action_notes.push(format!("✅ Published message to **`{}`** (`{}`): _{}_", ch, target_ns, body_txt));
                    }
                }
            } else if (lower.starts_with("dm ") || lower.starts_with("send ") || lower.starts_with("message ") || lower.starts_with("assign ")) && hd_token.is_some() {
                let hd = hd_token.unwrap();
                let clean_hd = hd.trim_start_matches('@');
                if Control::authorize(pool, pid, &target_ns, true).await.unwrap_or(false) {
                    let opts = SendOpts {
                        subject: String::new(),
                        intent: "task".to_string(),
                        priority: "normal".to_string(),
                        thread_key: String::new(),
                        replaces_id: None,
                        replace_pending: false,
                        in_reply_to: None,
                        metadata: json!({}),
                    };
                    if Control::inbox_send(pool, &target_ns, clean_hd, "owner", pid, body_txt, &opts).await.is_ok() {
                        channel_notify.notify_waiters();
                        steps.push(format!("Sent direct task to `{}` in `{}`", hd, target_ns));
                        action_notes.push(format!("✅ Sent direct `@inbox` message to **`{}`** (`{}`): _{}_", hd, target_ns, body_txt));
                    }
                }
            }
        }
    }

    if !action_notes.is_empty() {
        return (action_notes.join("\n\n"), steps);
    }

    // Structured executive briefing from live digest
    steps.push("Synthesized briefing from MEMNOS PostgreSQL ledger".to_string());
    let counts = &digest["counts"];
    let hrs = digest["window_hours"].as_i64().unwrap_or(8);
    let scope_lbl = digest["scope"].as_str().unwrap_or("*");

    let mut md = format!(
        "### Executive MEMNOS Briefing (Last {}h · Scope `{}`)\n\n\
         - **Online Handles:** `{}` active (`{}` total registered)  ·  **Active Channels:** `{}`  ·  **Active File Leases:** `{}`\n\
         - **Activity in Last {}h:** `{}` new memories (`{}` decisions/fixes/incidents), `{}` `#channel` posts (`{}` open tasks, `{}` claimed, `{}` completed), `{}` `@inbox` DMs\n\n",
        hrs,
        scope_lbl,
        counts["online_handles"].as_i64().unwrap_or(0),
        counts["total_handles"].as_i64().unwrap_or(0),
        counts["active_channels"].as_i64().unwrap_or(0),
        counts["active_leases"].as_i64().unwrap_or(0),
        hrs,
        counts["new_facts"].as_i64().unwrap_or(0),
        counts["decisions_and_fixes"].as_i64().unwrap_or(0),
        counts["channel_posts"].as_i64().unwrap_or(0),
        counts["open_tasks"].as_i64().unwrap_or(0),
        counts["claimed_tasks"].as_i64().unwrap_or(0),
        counts["completed_tasks"].as_i64().unwrap_or(0),
        counts["inbox_messages"].as_i64().unwrap_or(0),
    );

    if let Some(handles) = digest["online_handles"].as_array() {
        md.push_str("#### Live Agent Coordination (`@Handles` Online Now)\n");
        if handles.is_empty() {
            md.push_str("_No `@handles` are actively heartbeating right now._\n\n");
        } else {
            for h in handles.iter().take(12) {
                let nm = h["name"].as_str().unwrap_or("agent");
                let ns = h["namespace"].as_str().unwrap_or("default");
                let har = h["harness"].as_str().unwrap_or("cli");
                let mdl = h["model"].as_str().unwrap_or("");
                let task = h["current_task"].as_str().unwrap_or("");
                let chs = h["channels"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|x| x.as_str())
                            .collect::<Vec<_>>()
                            .join(" ")
                    })
                    .unwrap_or_default();
                let mdl_part = if mdl.is_empty() {
                    String::new()
                } else {
                    format!(" · `{}`", mdl)
                };
                let ch_part = if chs.is_empty() {
                    String::new()
                } else {
                    format!(" · listening on {}", chs)
                };
                let task_part = if task.is_empty() {
                    String::new()
                } else {
                    format!(" — _{}_", task)
                };
                md.push_str(&format!(
                    "- **`@{}`** (`{}` · {}{}{})\n",
                    nm, ns, har, mdl_part, ch_part
                ));
                if !task_part.is_empty() {
                    md.push_str(&format!("  {}\n", task_part.trim_start_matches(" — ")));
                }
            }
            md.push('\n');
        }
    }

    if let Some(ch_msgs) = digest["recent_channel_messages"].as_array() {
        if !ch_msgs.is_empty() {
            md.push_str(&format!("#### `#Channel` Broadcasts & Tasks (Last {}h)\n", hrs));
            for m in ch_msgs.iter().take(10) {
                let ch = m["channel"].as_str().unwrap_or("#general");
                let ns = m["namespace"].as_str().unwrap_or("");
                let sender = m["sender"].as_str().unwrap_or("agent");
                let content = m["content"].as_str().unwrap_or("");
                let claimable = m["claimable"].as_bool().unwrap_or(false);
                let cst = m["claim_status"].as_str().unwrap_or("none");
                let cby = m["claimed_by"].as_str().unwrap_or("");
                let tag = if claimable {
                    match cst {
                        "done" => " `[DONE]`".to_string(),
                        "claimed" => format!(" `[CLAIMED by @{}]`", cby),
                        _ => " `[OPEN TASK]`".to_string(),
                    }
                } else {
                    String::new()
                };
                md.push_str(&format!(
                    "- **{}** (`{}`) from **`@{}`**{}: {}\n",
                    ch, ns, sender, tag, content
                ));
            }
            md.push('\n');
        }
    }

    if let Some(inb) = digest["recent_inbox_messages"].as_array() {
        if !inb.is_empty() {
            md.push_str(&format!("#### Direct `@Inbox` Handoffs (Last {}h)\n", hrs));
            for dm in inb.iter().take(8) {
                md.push_str(&format!(
                    "- **`@{}` → `@{}`** (`{}` · `{}`): {}\n",
                    dm["from"].as_str().unwrap_or(""),
                    dm["to"].as_str().unwrap_or(""),
                    dm["namespace"].as_str().unwrap_or(""),
                    dm["status"].as_str().unwrap_or("pending"),
                    dm["content"].as_str().unwrap_or(""),
                ));
            }
            md.push('\n');
        }
    }

    if let Some(facts) = digest["recent_facts"].as_array() {
        if !facts.is_empty() {
            md.push_str(&format!("#### Key Decisions, Fixes & Facts Recorded (Last {}h)\n", hrs));
            for f in facts.iter().take(10) {
                md.push_str(&format!(
                    "- **`[{}]`** (`{}`): {}\n",
                    f["type"].as_str().unwrap_or("fact").to_uppercase(),
                    f["namespace"].as_str().unwrap_or(""),
                    f["statement"].as_str().unwrap_or(""),
                ));
            }
            md.push('\n');
        }
    }

    md.push_str(
        "_Tip: You can also manage coordination directly here — e.g. `Create channel #release-sync in proj:payments-sync`, `Subscribe @agent to #release-sync`, or `Send to @agent: Review PR`._",
    );
    (md, steps)
}

fn extract_prefixed_token(text: &str, prefix: char) -> Option<String> {
    for raw in text.split_whitespace() {
        let trimmed = raw.trim_matches(|c: char| {
            matches!(c, ',' | '.' | ':' | ';' | '\'' | '"' | '`' | '(' | ')')
        });
        if trimmed.starts_with(prefix) && trimmed.len() > 1 {
            return Some(trimmed.to_string());
        }
    }
    None
}

fn extract_ns_mention(text: &str) -> Option<String> {
    let words: Vec<&str> = text.split_whitespace().collect();
    for (i, w) in words.iter().enumerate() {
        if w.eq_ignore_ascii_case("in") || w.eq_ignore_ascii_case("namespace") {
            if let Some(next) = words.get(i + 1) {
                let clean = next.trim_matches(|c: char| {
                    matches!(c, ',' | '.' | ':' | ';' | '\'' | '"' | '`' | '(' | ')')
                });
                if !clean.is_empty() && !clean.starts_with('#') && !clean.starts_with('@') {
                    return Some(clean.to_string());
                }
            }
        }
    }
    None
}
