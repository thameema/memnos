//! `memnos serve` — Unified HTTP Server (`server.rs`).
//!
//! Serves 100% of the MEMNOS REST API, the workspace & admin JSON APIs + WebSocket bus,
//! the Streamable-HTTP `/mcp` JSON-RPC 2.0 mount, and the `LISTEN memnos_channel`
//! `/channel/wait` long-polling bus.

use crate::config::{dsn, load_config, server_port, BUILD_SHA, VERSION};
use crate::control::{
    Advert, Control, ControlError, HeartbeatOpts, PublishOpts, SendOpts, SessionOpts, Webhook,
    SECRET_NS_PREFIX,
};
use crate::db::{build_pool, spawn_channel_listener};
use crate::embed::Embedder;
use crate::extract::Extractor;
use crate::mcp::{handle_jsonrpc_message, McpContext};
use crate::nsresolve::is_local_pid_alive;
use crate::store::{BrainStore, RecallOpts};
use crate::vault::{Vault, VaultError};
use axum::{
    body::Bytes,
    extract::{
        ws::{Message as WsMessage, WebSocketUpgrade},
        Path as AxumPath, Query, State,
    },
    http::{header, HeaderMap, Method, StatusCode, Uri},
    response::{
        sse::{Event, KeepAlive, Sse},
        IntoResponse, Response,
    },
    routing::{any, delete, get, post},
    Json, Router,
};
use deadpool_postgres::Pool;
use futures_util::{stream, SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::convert::Infallible;
use std::env;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::{Notify, Semaphore};
use tower_http::cors::CorsLayer;

const ALLOWED_MEMORY_TYPES: &[&str] = &[
    "decision",
    "incident",
    "constraint",
    "skill",
    "fact",
    "handoff",
    "fix",
];

#[derive(Clone)]
pub struct AppState {
    pub port: u16,
    pub pool: Pool,
    pub store: Arc<BrainStore>,
    pub embedder: Arc<Embedder>,
    pub extractor: Arc<Extractor>,
    pub channel_notify: Arc<Notify>,
    pub event_tx: tokio::sync::broadcast::Sender<Value>,
    pub warm_proven: Arc<AtomicBool>,
    pub embed_configured: Arc<AtomicBool>,
    pub setup_reason: Arc<Mutex<String>>,
    pub ns_cache: Arc<Mutex<Option<(Instant, Vec<Value>)>>>,
    pub mcp_sessions: Arc<Mutex<HashMap<String, Arc<Mutex<String>>>>>,
    pub mcp_semaphore: Arc<Semaphore>,
    pub spawner: Arc<crate::spawner::HeadlessSpawner>,
    pub copilot: Arc<crate::copilot::CopilotEngine>,
    pub started_at: Instant,
}

fn current_process_memory_mb() -> (Option<f64>, Option<f64>) {
    #[cfg(target_os = "macos")]
    {
        #[repr(C)]
        struct RusageInfoV4 {
            ri_uuid: [u8; 16],
            ri_user_time: u64,
            ri_system_time: u64,
            ri_pkg_idle_wkups: u64,
            ri_interrupt_wkups: u64,
            ri_pageins: u64,
            ri_wired_size: u64,
            ri_resident_size: u64,
            ri_phys_footprint: u64,
            _pad: [u64; 30],
        }
        extern "C" {
            fn proc_pid_rusage(pid: libc::c_int, flavor: libc::c_int, buffer: *mut RusageInfoV4) -> libc::c_int;
        }
        unsafe {
            let mut info: RusageInfoV4 = std::mem::zeroed();
            if proc_pid_rusage(std::process::id() as libc::c_int, 4, &mut info) == 0 {
                let rss = (info.ri_resident_size as f64) / (1024.0 * 1024.0);
                let fp = (info.ri_phys_footprint as f64) / (1024.0 * 1024.0);
                return (
                    Some((rss * 10.0).round() / 10.0),
                    Some((fp * 10.0).round() / 10.0),
                );
            }
        }
        (None, None)
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(statm) = std::fs::read_to_string("/proc/self/statm") {
            let mut parts = statm.split_whitespace();
            let _ = parts.next();
            if let Some(rss_pages) = parts.next().and_then(|s| s.parse::<u64>().ok()) {
                let page_sz = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(4096) as u64;
                let rss = ((rss_pages * page_sz) as f64) / (1024.0 * 1024.0);
                let r = (rss * 10.0).round() / 10.0;
                return (Some(r), Some(r));
            }
        }
        (None, None)
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        (None, None)
    }
}

fn bearer_token(headers: &HeaderMap) -> Option<String> {
    let raw = headers.get("authorization")?.to_str().ok()?;
    let rest = raw
        .strip_prefix("Bearer ")
        .or_else(|| raw.strip_prefix("bearer "))?;
    let trimmed = rest.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn json_resp(code: StatusCode, body: Value) -> Response {
    (code, Json(body)).into_response()
}

fn ctrl_err_resp(err: ControlError) -> Response {
    let (status, msg) = match err {
        ControlError::BadRequest(m) => (StatusCode::BAD_REQUEST, m),
        ControlError::Unauthorized(m) => (StatusCode::UNAUTHORIZED, m),
        ControlError::Forbidden(m) => (StatusCode::FORBIDDEN, m),
        ControlError::NotFound(m) => (StatusCode::NOT_FOUND, m),
        ControlError::Conflict(m) => (StatusCode::CONFLICT, m),
        ControlError::Db(m) => {
            if m.starts_with("pool:") {
                (StatusCode::SERVICE_UNAVAILABLE, m)
            } else {
                (StatusCode::INTERNAL_SERVER_ERROR, m)
            }
        }
    };
    json_resp(status, json!({ "error": msg }))
}

fn is_write_op(path: &str) -> bool {
    matches!(
        path,
        "/remember"
            | "/consolidate"
            | "/memory/write"
            | "/memory/delete"
            | "/corpus/ingest"
            | "/corpus/deviation"
            | "/ingest/file"
            | "/episode/segment"
            | "/episode/decay"
            | "/namespace/copy"
            | "/lease/acquire"
            | "/lease/heartbeat"
            | "/lease/release"
            | "/session/register"
            | "/session/rename"
            | "/session/quota"
            | "/session/heartbeat"
            | "/session/unregister"
            | "/channel/publish"
            | "/channel/subscribe"
            | "/channel/unsubscribe"
            | "/channel/advertise"
            | "/channel/registry"
            | "/channel/ack"
            | "/channel/cleanup"
            | "/inbox/send"
            | "/inbox/recall"
            | "/inbox/retract"
            | "/inbox/act"
    )
}

fn sync_local_session_rename(info: &Value) {
    let host = info.get("host").and_then(|v| v.as_str()).unwrap_or("");
    let local_host = crate::nsresolve::hostname();
    if !host.is_empty() && host != local_host {
        return;
    }
    let new_s = info.get("new_name").and_then(|v| v.as_str()).unwrap_or("");
    let old_s = info.get("old_name").and_then(|v| v.as_str()).unwrap_or("");
    let ns = info.get("namespace").and_then(|v| v.as_str()).unwrap_or("");
    let cwd = info.get("cwd").and_then(|v| v.as_str()).unwrap_or("");
    let tty = info.get("tty").and_then(|v| v.as_str()).unwrap_or("");
    let harness = info.get("harness").and_then(|v| v.as_str()).unwrap_or("");
    if new_s.is_empty() {
        return;
    }
    // 1. Update any active_sessions.json keys matching old_name or this TTY
    let p = crate::config::config_dir().join("active_sessions.json");
    if let Ok(raw) = std::fs::read_to_string(&p) {
        if let Ok(mut map) = serde_json::from_str::<serde_json::Map<String, Value>>(&raw) {
            let mut changed = false;
            for (_, v) in map.iter_mut() {
                let matches_old = !old_s.is_empty()
                    && v.get("name").and_then(|x| x.as_str()) == Some(old_s);
                let matches_tty = !tty.is_empty()
                    && v.get("tty").and_then(|x| x.as_str()) == Some(tty);
                if matches_old || matches_tty {
                    if let Some(obj) = v.as_object_mut() {
                        obj.insert("name".to_string(), json!(new_s));
                        changed = true;
                    }
                }
            }
            if changed {
                let _ = std::fs::write(&p, serde_json::to_string_pretty(&map).unwrap_or_default());
            }
        }
    }
    if !cwd.is_empty() || !tty.is_empty() {
        crate::nsresolve::save_active_session_for_tty(new_s, cwd, ns, tty, harness);
    }
    // 2. Bust local statusline badge cache files that still hold old_name so statusline updates immediately!
    let sm_dir = crate::config::config_dir().join("sessmark");
    if let Ok(entries) = std::fs::read_dir(&sm_dir) {
        for ent in entries.flatten() {
            let fname = ent.file_name();
            if fname.to_string_lossy().starts_with("badge_") {
                if let Ok(raw_b) = std::fs::read_to_string(ent.path()) {
                    if !old_s.is_empty() && raw_b.contains(old_s) {
                        let _ = std::fs::remove_file(ent.path());
                    }
                }
            }
        }
    }
}

fn requires_embedding_setup(path: &str) -> bool {
    matches!(
        path,
        "/recall"
            | "/recall_v2"
            | "/recall/wide"
            | "/context"
            | "/remember"
            | "/memory/write"
            | "/memory/search"
            | "/memory/by_type"
            | "/memory/delete"
            | "/reconcile"
            | "/consolidate"
            | "/corpus/ingest"
            | "/corpus/check"
            | "/corpus/deviation"
            | "/corpus/diff"
            | "/corpus/list"
            | "/ingest/file"
            | "/episode/segment"
            | "/episode/recall"
            | "/episode/get"
            | "/episode/decay"
            | "/entity"
            | "/entity/related"
            | "/entity/dossier"
            | "/graph/query"
            | "/community/search"
            | "/contradictions"
            | "/provenance"
    )
}

async fn healthz_handler() -> Response {
    json_resp(
        StatusCode::OK,
        json!({
            "ok": true,
            "version": VERSION,
            "build": BUILD_SHA
        }),
    )
}

async fn readyz_handler(State(st): State<AppState>) -> Response {
    let db_ok = match st.pool.get().await {
        Ok(c) => c.query_one("SELECT 1", &[]).await.is_ok(),
        Err(_) => false,
    };
    let configured = st.embed_configured.load(Ordering::Acquire);
    let models_ready = configured
        && crate::rerank::models_ready_for_server(st.embedder.is_openai());
    let warm = st.warm_proven.load(Ordering::Relaxed) && models_ready;
    let ready = db_ok && warm;
    if ready {
        json_resp(
            StatusCode::OK,
            json!({
                "ready": true,
                "db": true,
                "warm": true,
                "version": VERSION,
                "build": BUILD_SHA
            }),
        )
    } else {
        let reason = if !db_ok {
            "database unreachable".to_string()
        } else if !configured {
            st.setup_reason
                .lock()
                .ok()
                .map(|g| g.clone())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| {
                    "memnos is not set up yet, run `memnos setup embeddings`".to_string()
                })
        } else if let Some(err) = crate::rerank::prewarm_error() {
            err
        } else {
            "loading neural models and warming indexes".to_string()
        };
        json_resp(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({
                "ready": false,
                "db": db_ok,
                "warm": false,
                "reason": reason,
                "error": reason,
                "version": VERSION,
                "build": BUILD_SHA
            }),
        )
    }
}

async fn metrics_handler(
    State(st): State<AppState>,
    headers: HeaderMap,
    Query(qs): Query<HashMap<String, String>>,
) -> Response {
    let tok = bearer_token(&headers).unwrap_or_default();
    match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(_)) => {}
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    }
    let hours = qs
        .get("hours")
        .and_then(|s| s.parse::<i32>().ok())
        .unwrap_or(24)
        .clamp(1, 720);
    match Control::stats(&st.pool, hours).await {
        Ok(ops) => json_resp(
            StatusCode::OK,
            json!({ "window_hours": hours, "ops": ops }),
        ),
        Err(e) => ctrl_err_resp(e),
    }
}

async fn metrics_prometheus_handler(State(st): State<AppState>) -> Response {
    let warm = if st.warm_proven.load(Ordering::Relaxed) {
        1
    } else {
        0
    };
    let mut lines = vec![
        "# HELP memnos_up Process liveness".to_string(),
        "# TYPE memnos_up gauge".to_string(),
        "memnos_up 1".to_string(),
        "# HELP memnos_ready Readiness including DB + HNSW warm".to_string(),
        "# TYPE memnos_ready gauge".to_string(),
        format!("memnos_ready {}", warm),
    ];
    if let Ok(ops) = Control::stats(&st.pool, 24).await {
        lines.push("# HELP memnos_ops_24h Operations in last 24h".to_string());
        lines.push("# TYPE memnos_ops_24h gauge".to_string());
        for row in ops {
            let act = row.get("action").and_then(|v| v.as_str()).unwrap_or("unknown");
            let total = row
                .get("calls")
                .or_else(|| row.get("total"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            lines.push(format!("memnos_ops_24h{{action={:?}}} {}", act, total));
        }
    }
    (
        StatusCode::OK,
        [(
            header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        format!("{}\n", lines.join("\n")),
    )
        .into_response()
}

async fn root_status_handler() -> Response {
    json_resp(
        StatusCode::OK,
        json!({
            "ok": true,
            "service": "memnos",
            "version": VERSION,
            "build": BUILD_SHA,
            "hint": "Use the MEMNOS desktop app (`memnos app`) or `memnos` CLI."
        }),
    )
}

async fn mcp_http_handler(
    State(st): State<AppState>,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::GET {
        return json_resp(
            StatusCode::OK,
            json!({ "ok": true, "transport": "streamable-http" }),
        );
    }
    if method == Method::DELETE {
        return (StatusCode::NO_CONTENT, "").into_response();
    }
    if method != Method::POST {
        return json_resp(
            StatusCode::METHOD_NOT_ALLOWED,
            json!({ "error": "method not allowed" }),
        );
    }

    // Origin guard against browser DNS-rebinding / cross-origin requests on loopback
    if let Some(origin) = headers.get("origin").and_then(|v| v.to_str().ok()) {
        let allow_all = env::var("MEMNOS_MCP_ALLOW_ORIGINS").unwrap_or_default() == "*";
        if !allow_all {
            let ok_origin = url::Url::parse(origin)
                .ok()
                .and_then(|u| u.host_str().map(|h| h.to_ascii_lowercase()))
                .map(|h| matches!(h.as_str(), "localhost" | "127.0.0.1" | "::1" | "[::1]"))
                .unwrap_or(false);
            if !ok_origin {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": "origin not allowed on /mcp" }),
                );
            }
        }
    }

    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };

    let default_ns = headers
        .get("x-memnos-namespace")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "default".to_string());

    let mcp_sid = headers
        .get("mcp-session-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .trim();
    let sess_key = format!("{}:{}:{}", pid, default_ns, mcp_sid);
    let active_session = {
        let mut map = st.mcp_sessions.lock().unwrap();
        if map.len() > 2048 {
            map.clear();
        }
        map.entry(sess_key)
            .or_insert_with(|| Arc::new(Mutex::new(String::new())))
            .clone()
    };

    let Ok(req_val) = serde_json::from_slice::<Value>(&body) else {
        return json_resp(
            StatusCode::BAD_REQUEST,
            json!({
                "jsonrpc": "2.0",
                "id": null,
                "error": { "code": -32700, "message": "Parse error" }
            }),
        );
    };

    let Ok(_permit) = st.mcp_semaphore.clone().acquire_owned().await else {
        return json_resp(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({ "error": "MCP concurrency limit reached" }),
        );
    };

    let ctx = McpContext {
        url: format!("http://127.0.0.1:{}", st.port),
        token: tok,
        default_ns,
        active_session,
    };

    let resp_opt = tokio::task::spawn_blocking(move || {
        if let Some(arr) = req_val.as_array() {
            let mut out = Vec::new();
            for item in arr {
                if let Some(r) = handle_jsonrpc_message(&ctx, item) {
                    out.push(r);
                }
            }
            if out.is_empty() {
                None
            } else {
                Some(Value::Array(out))
            }
        } else {
            handle_jsonrpc_message(&ctx, &req_val)
        }
    })
    .await
    .ok()
    .flatten();

    match resp_opt {
        Some(v) => json_resp(StatusCode::OK, v),
        None => (StatusCode::ACCEPTED, "").into_response(),
    }
}

async fn secret_resolve_handler(
    State(st): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let t0 = Instant::now();
    let Ok(req) = serde_json::from_slice::<Value>(&body) else {
        return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "invalid json" }));
    };
    let name = req
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if name.is_empty() {
        return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "name required" }));
    }
    if name.contains('*') {
        return json_resp(
            StatusCode::BAD_REQUEST,
            json!({ "error": "name must not contain '*'" }),
        );
    }

    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };

    let pseudo_ns = format!("{}{}", SECRET_NS_PREFIX, name);
    let allowed = match Control::authorize(&st.pool, pid, &pseudo_ns, false).await {
        Ok(b) => b,
        Err(e) => return ctrl_err_resp(e),
    };
    if !allowed {
        let _ = Control::audit(
            &st.pool,
            Some(pid),
            "secret/resolve",
            Some(&pseudo_ns),
            false,
            Some(json!({ "name": name, "reason": "forbidden" })),
            Some(t0.elapsed().as_millis() as i32),
            None,
            Some(403),
        )
        .await;
        return json_resp(
            StatusCode::FORBIDDEN,
            json!({ "error": "forbidden for secret" }),
        );
    }

    let obj = match st.pool.get().await {
        Ok(o) => o,
        Err(e) => {
            return json_resp(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({ "error": format!("database unreachable: {}", e) }),
            )
        }
    };

    match Vault::get(&obj, &name).await {
        Ok(Some(val)) => {
            let _ = Control::audit(
                &st.pool,
                Some(pid),
                "secret/resolve",
                Some(&pseudo_ns),
                true,
                Some(json!({ "name": name })),
                Some(t0.elapsed().as_millis() as i32),
                None,
                Some(200),
            )
            .await;
            json_resp(StatusCode::OK, json!({ "name": name, "value": val }))
        }
        Ok(None) => {
            let _ = Control::audit(
                &st.pool,
                Some(pid),
                "secret/resolve",
                Some(&pseudo_ns),
                false,
                Some(json!({ "name": name, "reason": "not_found" })),
                Some(t0.elapsed().as_millis() as i32),
                None,
                Some(404),
            )
            .await;
            json_resp(
                StatusCode::NOT_FOUND,
                json!({ "error": "secret not found" }),
            )
        }
        Err(VaultError::Locked(m)) => {
            let _ = Control::audit(
                &st.pool,
                Some(pid),
                "secret/resolve",
                Some(&pseudo_ns),
                false,
                Some(json!({ "name": name, "reason": "vault_locked" })),
                Some(t0.elapsed().as_millis() as i32),
                None,
                Some(409),
            )
            .await;
            json_resp(
                StatusCode::CONFLICT,
                json!({ "error": "vault locked", "msg": m }),
            )
        }
        Err(e) => json_resp(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "error": e.to_string() }),
        ),
    }
}

async fn user_routes_handler(
    State(st): State<AppState>,
    method: Method,
    uri: Uri,
    Query(qs): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };

    let path = uri.path();
    let b: Value = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(&body) {
            Ok(v) => v,
            Err(_) => return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "invalid json" })),
        }
    };

    if path == "/bindings/recap" && method == Method::GET {
        let days = qs
            .get("days")
            .and_then(|s| s.parse::<i32>().ok())
            .unwrap_or(7);
        return match Control::write_recap(&st.pool, pid, days, 50).await {
            Ok(recap) => json_resp(StatusCode::OK, json!({ "days": days, "recap": recap })),
            Err(e) => ctrl_err_resp(e),
        };
    }

    if path == "/bindings" && method == Method::GET {
        let bindings = match Control::list_bindings(&st.pool, pid).await {
            Ok(v) => v,
            Err(e) => return ctrl_err_resp(e),
        };
        let hosts = match Control::list_hosts(&st.pool, pid).await {
            Ok(v) => v,
            Err(e) => return ctrl_err_resp(e),
        };
        return json_resp(
            StatusCode::OK,
            json!({ "bindings": bindings, "hosts": hosts }),
        );
    }

    if path == "/bindings" && method == Method::POST {
        let kt = b.get("key_type").and_then(|v| v.as_str()).unwrap_or("").trim();
        let key = b.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
        let ns = b
            .get("namespace")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let host_id = b
            .get("host_id")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty());
        if !matches!(kt, "repo" | "host_repo" | "host_path") {
            return json_resp(
                StatusCode::BAD_REQUEST,
                json!({ "error": "key_type must be 'repo'|'host_repo'|'host_path'" }),
            );
        }
        if key.is_empty() || ns.is_empty() || ns.len() > 200 {
            return json_resp(
                StatusCode::BAD_REQUEST,
                json!({ "error": "key and namespace required (namespace <=200 chars)" }),
            );
        }
        if matches!(kt, "host_repo" | "host_path") && host_id.is_none() {
            return json_resp(
                StatusCode::BAD_REQUEST,
                json!({ "error": "host_id required for host-scoped binding" }),
            );
        }
        return match Control::upsert_binding(&st.pool, pid, kt, key, ns, host_id).await {
            Ok(row) => json_resp(StatusCode::OK, json!({ "binding": row })),
            Err(e) => ctrl_err_resp(e),
        };
    }

    if let Some(rest) = path.strip_prefix("/bindings/") {
        if method == Method::DELETE {
            let Ok(bid) = rest.parse::<i64>() else {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "binding id (int) required" }),
                );
            };
            return match Control::delete_binding(&st.pool, pid, bid).await {
                Ok(true) => json_resp(StatusCode::OK, json!({ "ok": true })),
                Ok(false) => json_resp(
                    StatusCode::NOT_FOUND,
                    json!({ "error": "binding not found" }),
                ),
                Err(e) => ctrl_err_resp(e),
            };
        }
    }

    if path == "/nudges" && method == Method::GET {
        return match Control::take_pending_nudges(&st.pool, pid, 20).await {
            Ok(nudges) => json_resp(StatusCode::OK, json!({ "nudges": nudges })),
            Err(e) => ctrl_err_resp(e),
        };
    }

    if path == "/hosts" && method == Method::GET {
        return match Control::list_hosts(&st.pool, pid).await {
            Ok(hosts) => json_resp(StatusCode::OK, json!({ "hosts": hosts })),
            Err(e) => ctrl_err_resp(e),
        };
    }

    if path == "/hosts" && method == Method::POST {
        let mid = b
            .get("machine_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let fname = b
            .get("friendly_name")
            .and_then(|v| v.as_str())
            .map(|s| s.trim())
            .filter(|s| !s.is_empty());
        if mid.is_empty() {
            return json_resp(
                StatusCode::BAD_REQUEST,
                json!({ "error": "machine_id required" }),
            );
        }
        return match Control::upsert_host(&st.pool, pid, mid, fname).await {
            Ok(host) => json_resp(StatusCode::OK, json!({ "host": host })),
            Err(e) => ctrl_err_resp(e),
        };
    }

    json_resp(
        StatusCode::NOT_FOUND,
        json!({ "error": "unknown user route" }),
    )
}

async fn admin_api_handler(
    State(st): State<AppState>,
    AxumPath(sub): AxumPath<String>,
    method: Method,
    Query(qs): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };
    match Control::is_admin(&st.pool, pid).await {
        Ok(true) => {}
        Ok(false) => {
            return json_resp(
                StatusCode::FORBIDDEN,
                json!({ "error": "admin token required ('*' grant)" }),
            )
        }
        Err(e) => return ctrl_err_resp(e),
    }

    let b: Value = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(&body) {
            Ok(v) => v,
            Err(_) => return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "invalid json" })),
        }
    };

    match (sub.as_str(), method.clone()) {
        ("namespaces", Method::GET) => {
            if let Ok(guard) = st.ns_cache.lock() {
                if let Some((ts, ref cached)) = *guard {
                    if ts.elapsed() < Duration::from_secs(10) {
                        return json_resp(StatusCode::OK, json!({ "namespaces": cached }));
                    }
                }
            }
            match Control::list_namespaces(&st.pool).await {
                Ok(list) => {
                    if let Ok(mut guard) = st.ns_cache.lock() {
                        *guard = Some((Instant::now(), list.clone()));
                    }
                    json_resp(StatusCode::OK, json!({ "namespaces": list }))
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespaces", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            if name.is_empty() || name.len() > 200 {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "name required (<=200 chars)" }),
                );
            }
            let desc = b.get("description").and_then(|v| v.as_str());
            if let Err(e) = Control::create_namespace(&st.pool, name, Some(pid), desc).await {
                return ctrl_err_resp(e);
            }
            if let Some(kind) = b.get("kind").and_then(|v| v.as_str()).filter(|s| !s.is_empty()) {
                if let Err(e) = Control::set_namespace_kind(&st.pool, name, kind).await {
                    return ctrl_err_resp(e);
                }
            }
            if let Ok(mut g) = st.ns_cache.lock() {
                *g = None;
            }
            json_resp(StatusCode::OK, json!({ "ok": true, "name": name }))
        }
        ("namespaces", Method::DELETE) => {
            let name = qs.get("name").map(|s| s.trim()).unwrap_or("");
            if name.is_empty() {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "name required" }));
            }
            let purge = qs.get("purge").map(|s| s == "1").unwrap_or(false);
            if let Err(e) = Control::delete_namespace(&st.pool, name, purge).await {
                return ctrl_err_resp(e);
            }
            if let Ok(mut g) = st.ns_cache.lock() {
                *g = None;
            }
            json_resp(StatusCode::OK, json!({ "ok": true }))
        }
        ("namespaces/links", Method::GET) => {
            let src = qs
                .get("ns")
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            match Control::list_links(&st.pool, src).await {
                Ok(links) => json_resp(StatusCode::OK, json!({ "links": links })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespaces/links", Method::POST) => {
            let src = b.get("src").and_then(|v| v.as_str()).unwrap_or("").trim();
            let dst = b.get("dst").and_then(|v| v.as_str()).unwrap_or("").trim();
            let kind = b
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("link")
                .trim();
            if src.is_empty() || dst.is_empty() || src == dst {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "distinct src and dst required" }),
                );
            }
            match Control::link_namespaces(&st.pool, src, dst, Some(pid), kind).await {
                Ok(()) => json_resp(
                    StatusCode::OK,
                    json!({ "ok": true, "src": src, "dst": dst, "kind": kind }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespaces/links", Method::DELETE) => {
            let src = qs.get("src").map(|s| s.trim()).unwrap_or("");
            let dst = qs.get("dst").map(|s| s.trim()).unwrap_or("");
            if src.is_empty() || dst.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "src and dst required" }),
                );
            }
            match Control::unlink_namespaces(&st.pool, src, dst).await {
                Ok(removed) => json_resp(StatusCode::OK, json!({ "ok": true, "removed": removed })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespaces/kind", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let kind = b.get("kind").and_then(|v| v.as_str()).unwrap_or("").trim();
            if name.is_empty() || !matches!(kind, "memory" | "knowledge") {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "name and kind ('memory'|'knowledge') required" }),
                );
            }
            match Control::set_namespace_kind(&st.pool, name, kind).await {
                Ok(()) => {
                    if let Ok(mut g) = st.ns_cache.lock() {
                        *g = None;
                    }
                    json_resp(StatusCode::OK, json!({ "ok": true, "name": name, "kind": kind }))
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespaces/inherit-ancestors", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let Some(inherit) = b.get("inherit").and_then(|v| v.as_bool()) else {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "name and inherit (bool) required" }),
                );
            };
            match Control::set_namespace_inherit_ancestors(&st.pool, name, inherit).await {
                Ok(()) => {
                    if let Ok(mut g) = st.ns_cache.lock() {
                        *g = None;
                    }
                    json_resp(
                        StatusCode::OK,
                        json!({ "ok": true, "name": name, "inherit_ancestors": inherit }),
                    )
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("principals", Method::GET) => match Control::list_principals(&st.pool).await {
            Ok(p) => json_resp(StatusCode::OK, json!({ "principals": p, "caller_id": pid })),
            Err(e) => ctrl_err_resp(e),
        },
        ("principals", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let kind = b.get("kind").and_then(|v| v.as_str()).unwrap_or("user");
            if name.is_empty() {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "name required" }));
            }
            match Control::create_principal(&st.pool, name, kind).await {
                Ok(id) => json_resp(StatusCode::OK, json!({ "ok": true, "id": id })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("principals", Method::DELETE) => {
            let id = qs
                .get("id")
                .and_then(|s| s.parse::<i64>().ok())
                .or_else(|| b.get("id").and_then(|v| v.as_i64()))
                .unwrap_or(0);
            if id <= 0 {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "id required" }));
            }
            match Control::delete_principal(&st.pool, id, pid).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("tokens", Method::GET) => {
            let p = qs
                .get("principal")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            match Control::list_tokens(&st.pool, p).await {
                Ok(t) => json_resp(StatusCode::OK, json!({ "tokens": t })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("tokens", Method::POST) => {
            let p = b.get("principal_id").and_then(|v| v.as_i64()).unwrap_or(0);
            let label = b.get("label").and_then(|v| v.as_str());
            let ttl = b
                .get("ttl_days")
                .and_then(|v| v.as_i64())
                .map(|x| x as i32);
            match Control::mint_token(&st.pool, p, label, ttl).await {
                Ok(tok) => json_resp(StatusCode::OK, json!({ "token": tok })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("tokens/revoke", Method::POST) => {
            let id = b.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
            match Control::revoke_token_by_id(&st.pool, id).await {
                Ok(()) => json_resp(StatusCode::OK, json!({ "ok": true })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("tokens/reveal", Method::GET) => {
            let id = qs
                .get("id")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            if id <= 0 {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "id required" }));
            }
            match Control::reveal_token(&st.pool, id).await {
                Ok(v) => {
                    let _ = Control::audit(
                        &st.pool,
                        Some(pid),
                        "token.reveal",
                        None,
                        true,
                        Some(json!({ "token_id": id })),
                        Some(1),
                        None,
                        Some(200),
                    )
                    .await;
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("grants", Method::GET) => {
            let p = qs
                .get("principal")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            match Control::authorized_namespaces(&st.pool, p).await {
                Ok(g) => json_resp(StatusCode::OK, json!({ "grants": g })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("grants", Method::POST) => {
            let p = b.get("principal_id").and_then(|v| v.as_i64()).unwrap_or(0);
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("");
            let cr = b.get("can_read").and_then(|v| v.as_bool()).unwrap_or(true);
            let cw = b.get("can_write").and_then(|v| v.as_bool()).unwrap_or(true);
            match Control::grant(&st.pool, p, ns, cr, cw).await {
                Ok(()) => json_resp(StatusCode::OK, json!({ "ok": true })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("grants", Method::DELETE) => {
            let p = qs
                .get("principal")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            let ns = qs.get("namespace").map(|s| s.as_str()).unwrap_or("");
            match Control::revoke_grant(&st.pool, p, ns).await {
                Ok(()) => json_resp(StatusCode::OK, json!({ "ok": true })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("stats", Method::GET) => {
            let hours = qs
                .get("hours")
                .and_then(|s| s.parse::<i32>().ok())
                .unwrap_or(24)
                .clamp(1, 720);
            match Control::stats(&st.pool, hours).await {
                Ok(ops) => json_resp(
                    StatusCode::OK,
                    json!({ "ops": ops, "window_hours": hours }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("usage", Method::GET) => {
            let days = qs
                .get("days")
                .and_then(|s| s.parse::<i32>().ok())
                .unwrap_or(30)
                .clamp(1, 365);
            let ns = qs
                .get("namespace")
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            match Control::usage_summary(&st.pool, days, ns).await {
                Ok(mut u) => {
                    if let Some(obj) = u.as_object_mut() {
                        obj.insert("period_days".to_string(), json!(days));
                        obj.insert("namespace".to_string(), json!(ns));
                    }
                    json_resp(StatusCode::OK, u)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("budget", Method::GET) => {
            let daily = env::var("MEMNOS_BUDGET_DAILY_USD")
                .ok()
                .and_then(|s| s.parse::<f64>().ok());
            let monthly = env::var("MEMNOS_BUDGET_MONTHLY_USD")
                .ok()
                .and_then(|s| s.parse::<f64>().ok());
            match Control::budget_status(&st.pool, daily, monthly).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("memory/feed", Method::GET) => {
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(50)
                .clamp(1, 200);
            let offset = qs
                .get("offset")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0)
                .max(0);
            let fns = qs
                .get("namespace")
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            let ftype = qs
                .get("type")
                .map(|s| s.trim().to_lowercase())
                .filter(|s| !s.is_empty());
            if let Some(ref ft) = ftype {
                if !ALLOWED_MEMORY_TYPES.contains(&ft.as_str()) {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": format!("unknown type {:?}", ft) }),
                    );
                }
            }
            match Control::memory_feed(&st.pool, limit, offset, fns, ftype.as_deref()).await {
                Ok(m) => json_resp(
                    StatusCode::OK,
                    json!({ "memories": m, "limit": limit, "offset": offset }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("audit", Method::GET) => {
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(100)
                .clamp(1, 1000);
            let offset = qs
                .get("offset")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0)
                .max(0);
            let audit = match Control::recent_audit(&st.pool, limit, offset).await {
                Ok(v) => v,
                Err(e) => return ctrl_err_resp(e),
            };
            let total = Control::audit_total(&st.pool).await.unwrap_or(0);
            json_resp(
                StatusCode::OK,
                json!({
                    "audit": audit,
                    "total": total,
                    "total_estimated": true,
                    "limit": limit,
                    "offset": offset
                }),
            )
        }
        ("health", Method::GET) => match Control::health(&st.pool, 24).await {
            Ok(v) => {
                let findings: Vec<Value> = v
                    .as_array()
                    .map(|arr| {
                        arr.iter()
                            .map(|item| {
                                if let Some(pair) = item.as_array() {
                                    json!({
                                        "level": pair.first().and_then(|x| x.as_str()).unwrap_or("INFO"),
                                        "msg": pair.get(1).and_then(|x| x.as_str()).unwrap_or(""),
                                    })
                                } else {
                                    item.clone()
                                }
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                json_resp(StatusCode::OK, json!({ "findings": findings }))
            }
            Err(e) => ctrl_err_resp(e),
        },
        ("quality", Method::GET) => {
            match Control::eval_trend(&st.pool, "stale_suppression", "rate", 10).await {
                Ok(trend) => json_resp(StatusCode::OK, json!({ "trend": trend })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("subscriptions", Method::GET) => {
            let target = qs
                .get("principal")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(pid);
            match Control::list_subscriptions(&st.pool, target).await {
                Ok(subs) => json_resp(StatusCode::OK, json!({ "subscriptions": subs })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("deliver", Method::POST) => {
            let d1 = Control::deliver_pending(&st.pool, 50, 10)
                .await
                .unwrap_or_default();
            let d2 = Control::deliver_pending_sessions(&st.pool, 50, 10)
                .await
                .unwrap_or_default();
            json_resp(
                StatusCode::OK,
                json!({ "delivered": d1, "delivered_sessions": d2 }),
            )
        }
        ("provider", Method::GET) => json_resp(
            StatusCode::OK,
            json!({
                "mode": if st.embedder.is_openai() { "openai" } else { "local" },
                "dim": st.embedder.dim,
                "key_present": env::var("OPENAI_API_KEY").map(|s| !s.is_empty()).unwrap_or(false),
                "extraction": st.extractor.enabled(),
                "extract_base_url": env::var("MEMNOS_EXTRACT_BASE_URL").ok(),
                "extract_model": if st.extractor.enabled() { Some(st.extractor.model.clone()) } else { None },
                "vault_unlocked": Vault::available()
            }),
        ),
        ("secrets", Method::GET) => {
            let Ok(obj) = st.pool.get().await else {
                return json_resp(
                    StatusCode::SERVICE_UNAVAILABLE,
                    json!({ "error": "database unreachable" }),
                );
            };
            match Vault::list(&obj).await {
                Ok(secrets) => json_resp(
                    StatusCode::OK,
                    json!({ "secrets": secrets, "unlocked": Vault::available() }),
                ),
                Err(e) => json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e.to_string() }),
                ),
            }
        }
        ("secrets", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let val = b.get("value").and_then(|v| v.as_str()).unwrap_or("").trim();
            let desc = b.get("description").and_then(|v| v.as_str());
            if name.is_empty() || val.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "name and value required" }),
                );
            }
            let Ok(obj) = st.pool.get().await else {
                return json_resp(
                    StatusCode::SERVICE_UNAVAILABLE,
                    json!({ "error": "database unreachable" }),
                );
            };
            match Vault::set(&obj, name, val, desc).await {
                Ok(()) => json_resp(StatusCode::OK, json!({ "ok": true, "name": name })),
                Err(VaultError::Locked(m)) => json_resp(
                    StatusCode::CONFLICT,
                    json!({ "error": "vault locked", "msg": m }),
                ),
                Err(VaultError::BadValue(m)) => json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "bad secret value", "msg": m }),
                ),
                Err(e) => json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e.to_string() }),
                ),
            }
        }
        ("secrets", Method::DELETE) => {
            let name = qs.get("name").map(|s| s.trim()).unwrap_or("");
            let Ok(obj) = st.pool.get().await else {
                return json_resp(
                    StatusCode::SERVICE_UNAVAILABLE,
                    json!({ "error": "database unreachable" }),
                );
            };
            match Vault::delete(&obj, name).await {
                Ok(_) => json_resp(StatusCode::OK, json!({ "ok": true })),
                Err(e) => json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e.to_string() }),
                ),
            }
        }
        _ => json_resp(
            StatusCode::NOT_FOUND,
            json!({ "error": "unknown admin route" }),
        ),
    }
}

async fn workspace_ws_handler(
    State(st): State<AppState>,
    Query(qs): Query<HashMap<String, String>>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> Response {
    let tok = bearer_token(&headers)
        .or_else(|| {
            qs.get("token")
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };
    let is_admin = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
    let auth_ns: HashSet<String> = if is_admin {
        HashSet::new()
    } else {
        Control::readable_namespaces(&st.pool, pid, None, None)
            .await
            .unwrap_or_default()
            .into_iter()
            .collect()
    };

    ws.on_upgrade(move |socket| async move {
        let (mut tx, mut rx) = socket.split();
        let mut event_rx = st.event_tx.subscribe();
        let mut seq: u64 = 1;

        let hello = json!({
            "type": "connected",
            "transport": "websocket",
            "seq": 0,
            "version": VERSION,
            "build": BUILD_SHA,
        });
        if tx.send(WsMessage::Text(hello.to_string())).await.is_err() {
            return;
        }

        let mut hb = tokio::time::interval(Duration::from_secs(15));
        hb.tick().await;

        loop {
            tokio::select! {
                res = event_rx.recv() => {
                    match res {
                        Ok(mut v) => {
                            if let Some(tp) = v.get("target_principal_id").and_then(|x| x.as_i64()) {
                                if tp != pid {
                                    continue;
                                }
                            }
                            let ev_ns = v.get("namespace").and_then(|x| x.as_str()).unwrap_or("");
                            if is_admin || ev_ns.is_empty() || ev_ns == "*" || auth_ns.contains(ev_ns) {
                                if let Some(obj) = v.as_object_mut() {
                                    obj.insert("seq".to_string(), json!(seq));
                                }
                                seq += 1;
                                if tx.send(WsMessage::Text(v.to_string())).await.is_err() {
                                    break;
                                }
                            }
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                            let msg = json!({
                                "type": "refresh",
                                "seq": seq,
                                "ts": chrono::Utc::now().to_rfc3339(),
                            });
                            seq += 1;
                            if tx.send(WsMessage::Text(msg.to_string())).await.is_err() {
                                break;
                            }
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    }
                }
                incoming = rx.next() => {
                    let Some(Ok(msg)) = incoming else { break; };
                    match msg {
                        WsMessage::Text(txt) => {
                            let Ok(cmd) = serde_json::from_str::<Value>(&txt) else { continue; };
                            let kind = cmd.get("type").and_then(|v| v.as_str()).unwrap_or("");
                            let req_id = cmd.get("req_id").cloned().unwrap_or(Value::Null);
                            match kind {
                                "ping" => {
                                    let pong = json!({
                                        "type": "pong",
                                        "req_id": req_id,
                                        "ts": chrono::Utc::now().to_rfc3339(),
                                    });
                                    if tx.send(WsMessage::Text(pong.to_string())).await.is_err() {
                                        break;
                                    }
                                }
                                "publish_channel" => {
                                    let ns = cmd.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
                                    let ch = cmd.get("channel").and_then(|v| v.as_str()).unwrap_or("").trim();
                                    let sender = cmd
                                        .get("sender_session")
                                        .and_then(|v| v.as_str())
                                        .map(|s| s.trim().trim_start_matches('@'))
                                        .filter(|s| !s.is_empty())
                                        .unwrap_or("owner");
                                    let content = cmd.get("content").and_then(|v| v.as_str()).unwrap_or("");
                                    let claimable = cmd.get("claimable").and_then(|v| v.as_bool()).unwrap_or(false);
                                    let reply = if ns.is_empty() || ch.is_empty() || content.trim().is_empty() {
                                        json!({ "type": "ack", "req_id": req_id, "ok": false, "error": "namespace, channel, and content required" })
                                    } else if !Control::authorize(&st.pool, pid, ns, true).await.unwrap_or(false) {
                                        json!({ "type": "ack", "req_id": req_id, "ok": false, "error": format!("write grant required on '{}'", ns) })
                                    } else {
                                        let opts = PublishOpts {
                                            intent: if claimable { "task".to_string() } else { "message".to_string() },
                                            metadata: cmd.get("metadata").cloned().unwrap_or_else(|| json!({})),
                                            description: String::new(),
                                            what_you_get: String::new(),
                                            claimable,
                                            reply_to: None,
                                        };
                                        match Control::channel_publish(&st.pool, ns, ch, sender, pid, content, &opts).await {
                                            Ok(v) => {
                                                st.channel_notify.notify_waiters();
                                                let _ = st.event_tx.send(json!({
                                                    "type": "activity",
                                                    "op": "channel/publish",
                                                    "namespace": ns,
                                                    "actor": sender,
                                                    "detail": ch,
                                                    "ok": true,
                                                    "status": 200,
                                                    "latency_ms": 1,
                                                    "ts": chrono::Utc::now().to_rfc3339(),
                                                }));
                                                json!({ "type": "ack", "req_id": req_id, "ok": true, "result": v })
                                            }
                                            Err(e) => json!({ "type": "ack", "req_id": req_id, "ok": false, "error": format!("{:?}", e) }),
                                        }
                                    };
                                    if tx.send(WsMessage::Text(reply.to_string())).await.is_err() {
                                        break;
                                    }
                                }
                                "send_dm" => {
                                    let ns = cmd.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
                                    let to_s = cmd.get("to_session").and_then(|v| v.as_str()).map(|s| s.trim().trim_start_matches('@')).unwrap_or("");
                                    let from_s = cmd.get("from_session").and_then(|v| v.as_str()).map(|s| s.trim().trim_start_matches('@')).filter(|s| !s.is_empty()).unwrap_or("owner");
                                    let content = cmd.get("content").and_then(|v| v.as_str()).unwrap_or("");
                                    let intent = cmd.get("intent").and_then(|v| v.as_str()).unwrap_or("message").to_string();
                                    let reply = if ns.is_empty() || to_s.is_empty() || content.trim().is_empty() {
                                        json!({ "type": "ack", "req_id": req_id, "ok": false, "error": "namespace, to_session, and content required" })
                                    } else if !Control::authorize(&st.pool, pid, ns, true).await.unwrap_or(false) {
                                        json!({ "type": "ack", "req_id": req_id, "ok": false, "error": format!("write grant required on '{}'", ns) })
                                    } else {
                                        let opts = SendOpts {
                                            subject: String::new(),
                                            intent,
                                            priority: "normal".to_string(),
                                            thread_key: String::new(),
                                            replaces_id: None,
                                            replace_pending: false,
                                            in_reply_to: None,
                                            metadata: json!({}),
                                        };
                                        match Control::inbox_send(&st.pool, ns, to_s, from_s, pid, content, &opts).await {
                                            Ok(mut v) => {
                                                let is_adm = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
                                                if let Some(hd_mode) = st.spawner.deliver_dm(
                                                    &st.pool,
                                                    st.port,
                                                    pid,
                                                    is_adm,
                                                    st.event_tx.clone(),
                                                    Arc::clone(&st.channel_notify),
                                                    ns,
                                                    to_s,
                                                    from_s,
                                                    content,
                                                ).await {
                                                    v["headless_delivery"] = json!(hd_mode);
                                                }
                                                st.channel_notify.notify_waiters();
                                                let _ = st.event_tx.send(json!({
                                                    "type": "activity",
                                                    "op": "inbox/send",
                                                    "namespace": ns,
                                                    "actor": from_s,
                                                    "detail": format!("@{}", to_s),
                                                    "ok": true,
                                                    "status": 200,
                                                    "latency_ms": 1,
                                                    "ts": chrono::Utc::now().to_rfc3339(),
                                                }));
                                                json!({ "type": "ack", "req_id": req_id, "ok": true, "result": v })
                                            }
                                            Err(e) => json!({ "type": "ack", "req_id": req_id, "ok": false, "error": format!("{:?}", e) }),
                                        }
                                    };
                                    if tx.send(WsMessage::Text(reply.to_string())).await.is_err() {
                                        break;
                                    }
                                }
                                _ => {}
                            }
                        }
                        WsMessage::Ping(p) => {
                            if tx.send(WsMessage::Pong(p)).await.is_err() {
                                break;
                            }
                        }
                        WsMessage::Close(_) => break,
                        _ => {}
                    }
                }
                _ = hb.tick() => {
                    let beat = json!({
                        "type": "heartbeat",
                        "seq": seq,
                        "ts": chrono::Utc::now().to_rfc3339(),
                    });
                    seq += 1;
                    if tx.send(WsMessage::Text(beat.to_string())).await.is_err() {
                        break;
                    }
                }
            }
        }
    })
}

async fn workspace_events_handler(
    State(st): State<AppState>,
    Query(qs): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let tok = bearer_token(&headers)
        .or_else(|| qs.get("token").map(|s| s.trim().to_string()).filter(|s| !s.is_empty()))
        .unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };
    let is_admin = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
    let auth_ns: HashSet<String> = if is_admin {
        HashSet::new()
    } else {
        Control::readable_namespaces(&st.pool, pid, None, None)
            .await
            .unwrap_or_default()
            .into_iter()
            .collect()
    };

    let event_rx = st.event_tx.subscribe();
    let s = stream::unfold(
        (event_rx, auth_ns, is_admin, pid, 0u64),
        |(mut event_rx, auth_ns, is_admin, pid, seq)| async move {
            if seq == 0 {
                // 2 KB comment preamble flushes macOS WKWebView / CFNetwork's initial chunk buffer immediately
                let ev = Event::default()
                    .comment(" ".repeat(2048))
                    .data(json!({ "type": "connected", "seq": 0, "version": VERSION, "build": BUILD_SHA }).to_string());
                return Some((Ok::<Event, Infallible>(ev), (event_rx, auth_ns, is_admin, pid, 1)));
            }
            let payload = loop {
                tokio::select! {
                    res = event_rx.recv() => {
                        match res {
                            Ok(mut v) => {
                                if let Some(tp) = v.get("target_principal_id").and_then(|x| x.as_i64()) {
                                    if tp != pid {
                                        continue;
                                    }
                                }
                                let ev_ns = v.get("namespace").and_then(|x| x.as_str()).unwrap_or("");
                                if is_admin || ev_ns.is_empty() || ev_ns == "*" || auth_ns.contains(ev_ns) {
                                    if let Some(obj) = v.as_object_mut() {
                                        obj.insert("seq".to_string(), json!(seq));
                                    }
                                    break v;
                                }
                            }
                            Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                                break json!({
                                    "type": "refresh",
                                    "seq": seq,
                                    "ts": chrono::Utc::now().to_rfc3339(),
                                });
                            }
                            Err(tokio::sync::broadcast::error::RecvError::Closed) => return None,
                        }
                    }
                    _ = tokio::time::sleep(Duration::from_secs(10)) => {
                        break json!({
                            "type": "heartbeat",
                            "seq": seq,
                            "ts": chrono::Utc::now().to_rfc3339(),
                        });
                    }
                }
            };
            let ev = Event::default().data(payload.to_string());
            Some((
                Ok::<Event, Infallible>(ev),
                (event_rx, auth_ns, is_admin, pid, seq + 1),
            ))
        },
    );

    let mut resp = Sse::new(s)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(15)).text("keepalive"))
        .into_response();
    resp.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-cache, no-transform"),
    );
    resp.headers_mut().insert(
        header::HeaderName::from_static("x-accel-buffering"),
        header::HeaderValue::from_static("no"),
    );
    resp
}

async fn workspace_api_handler(
    State(st): State<AppState>,
    AxumPath(sub): AxumPath<String>,
    method: Method,
    Query(qs): Query<HashMap<String, String>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method == Method::OPTIONS {
        return StatusCode::NO_CONTENT.into_response();
    }
    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };

    let b: Value = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(&body) {
            Ok(v) => v,
            Err(_) => return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "invalid json" })),
        }
    };

    match (sub.as_str(), method.clone()) {
        ("overview", Method::GET) => {
            let ns_filter = qs.get("namespace").map(|s| s.as_str());
            match Control::workspace_overview(&st.pool, pid, ns_filter).await {
                Ok(mut v) => {
                    let (rss_mb, footprint_mb) = current_process_memory_mb();
                    let emb_mode = if st.embedder.is_openai() {
                        format!("OpenAI ({}-d)", st.embedder.dim)
                    } else {
                        format!("Local {} ({}-d)", st.embedder.model_name(), st.embedder.dim)
                    };
                    if let Some(vitals) = v.get_mut("vitals").and_then(|x| x.as_object_mut()) {
                        vitals.insert("version".into(), json!(VERSION));
                        vitals.insert("build".into(), json!(BUILD_SHA));
                        vitals.insert("uptime_secs".into(), json!(st.started_at.elapsed().as_secs()));
                        vitals.insert("rss_mb".into(), json!(rss_mb));
                        vitals.insert("footprint_mb".into(), json!(footprint_mb));
                        vitals.insert("embedding_mode".into(), json!(emb_mode));
                        vitals.insert("ready".into(), json!(st.embed_configured.load(Ordering::Acquire)));
                        vitals.insert("warm".into(), json!(st.warm_proven.load(Ordering::Relaxed)));
                    }
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel/messages", Method::GET) => {
            let ns = qs.get("namespace").map(|s| s.trim()).unwrap_or("");
            let ch = qs.get("channel").map(|s| s.trim()).unwrap_or("");
            if ns.is_empty() || ch.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and channel required" }),
                );
            }
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(120);
            let before_id = qs.get("before_id").and_then(|s| s.parse::<i64>().ok());
            match Control::workspace_channel_messages(&st.pool, pid, ns, ch, limit, before_id).await {
                Ok(msgs) => json_resp(
                    StatusCode::OK,
                    json!({ "namespace": ns, "channel": ch, "messages": msgs }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel/create", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let raw_ch = b.get("channel").and_then(|v| v.as_str()).unwrap_or("").trim();
            if ns.is_empty() || raw_ch.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and channel required" }),
                );
            }
            match Control::authorize(&st.pool, pid, ns, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required on namespace '{}'", ns) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            let ch = if raw_ch.starts_with('#') {
                raw_ch.to_string()
            } else {
                format!("#{}", raw_ch)
            };
            let desc = b.get("description").and_then(|v| v.as_str()).unwrap_or("");
            let ad = Advert {
                owner_session: "owner",
                description: desc,
                what_you_get: "",
                intents: None,
                metadata: None,
            };
            match Control::channel_advertise(&st.pool, ns, &ch, pid, ad).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel", Method::DELETE) => {
            let ns = qs
                .get("namespace")
                .map(|s| s.as_str())
                .or_else(|| b.get("namespace").and_then(|v| v.as_str()))
                .unwrap_or("")
                .trim();
            let ch = qs
                .get("channel")
                .map(|s| s.as_str())
                .or_else(|| b.get("channel").and_then(|v| v.as_str()))
                .unwrap_or("")
                .trim();
            if ns.is_empty() || ch.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and channel required" }),
                );
            }
            match Control::workspace_delete_channel(&st.pool, pid, ns, ch).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel/subscribe", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let ch = b.get("channel").and_then(|v| v.as_str()).unwrap_or("").trim();
            let sess = b
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            match Control::workspace_manage_subscription(&st.pool, pid, ns, ch, sess, true).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel/unsubscribe", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let ch = b.get("channel").and_then(|v| v.as_str()).unwrap_or("").trim();
            let sess = b
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            match Control::workspace_manage_subscription(&st.pool, pid, ns, ch, sess, false).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("channel/publish", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let ch = b.get("channel").and_then(|v| v.as_str()).unwrap_or("").trim();
            let sender = b
                .get("sender_session")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().trim_start_matches('@'))
                .filter(|s| !s.is_empty())
                .unwrap_or("owner");
            let content = b.get("content").and_then(|v| v.as_str()).unwrap_or("");
            if ns.is_empty() || ch.is_empty() || content.trim().is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace, channel, and content required" }),
                );
            }
            match Control::authorize(&st.pool, pid, ns, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required on namespace '{}'", ns) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            let claimable = b.get("claimable").and_then(|v| v.as_bool()).unwrap_or(false);
            let default_intent = if claimable { "task" } else { "message" };
            let opts = PublishOpts {
                intent: b
                    .get("intent")
                    .and_then(|v| v.as_str())
                    .unwrap_or(default_intent)
                    .to_string(),
                metadata: b.get("metadata").cloned().unwrap_or_else(|| json!({})),
                description: String::new(),
                what_you_get: String::new(),
                claimable,
                reply_to: b.get("reply_to").and_then(|v| v.as_i64()).filter(|&x| x > 0),
            };
            match Control::channel_publish(&st.pool, ns, ch, sender, pid, content, &opts).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    let _ = st.event_tx.send(json!({
                        "type": "activity",
                        "op": "channel/publish",
                        "namespace": ns,
                        "actor": sender,
                        "detail": ch,
                        "ok": true,
                        "status": 200,
                        "latency_ms": 1,
                        "ts": chrono::Utc::now().to_rfc3339(),
                    }));
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("session/messages", Method::GET) => {
            let ns = qs.get("namespace").map(|s| s.trim()).unwrap_or("");
            let sess = qs.get("session").map(|s| s.trim()).unwrap_or("");
            if ns.is_empty() || sess.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and session required" }),
                );
            }
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(120);
            match Control::workspace_session_messages(&st.pool, pid, ns, sess, limit).await {
                Ok(msgs) => json_resp(
                    StatusCode::OK,
                    json!({ "namespace": ns, "session": sess, "messages": msgs }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("session/send", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let to_s = b
                .get("to_session")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().trim_start_matches('@'))
                .unwrap_or("");
            let from_s = b
                .get("from_session")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().trim_start_matches('@'))
                .filter(|s| !s.is_empty())
                .unwrap_or("owner");
            let content = b.get("content").and_then(|v| v.as_str()).unwrap_or("");
            if ns.is_empty() || to_s.is_empty() || content.trim().is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace, to_session, and content required" }),
                );
            }
            match Control::authorize(&st.pool, pid, ns, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required on namespace '{}'", ns) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            let opts = SendOpts {
                subject: b
                    .get("subject")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                intent: b
                    .get("intent")
                    .and_then(|v| v.as_str())
                    .unwrap_or("message")
                    .to_string(),
                priority: b
                    .get("priority")
                    .and_then(|v| v.as_str())
                    .unwrap_or("normal")
                    .to_string(),
                thread_key: String::new(),
                replaces_id: None,
                replace_pending: false,
                in_reply_to: None,
                metadata: json!({}),
            };
            match Control::inbox_send(&st.pool, ns, to_s, from_s, pid, content, &opts).await {
                Ok(mut v) => {
                    let is_adm = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
                    if let Some(hd_mode) = st
                        .spawner
                        .deliver_dm(
                            &st.pool,
                            st.port,
                            pid,
                            is_adm,
                            st.event_tx.clone(),
                            Arc::clone(&st.channel_notify),
                            ns,
                            to_s,
                            from_s,
                            content,
                        )
                        .await
                    {
                        v["headless_delivery"] = json!(hd_mode);
                    }
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("spawner/status", Method::GET) => {
            let is_admin = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
            let allowed_ns: Option<Vec<String>> = if is_admin {
                None
            } else if let Ok(c) = st.pool.get().await {
                Control::workspace_authorized_ns_map(&**c, pid)
                    .await
                    .ok()
                    .map(|v| v.into_iter().map(|(n, _)| n).collect())
            } else {
                Some(vec![])
            };
            json_resp(
                StatusCode::OK,
                st.spawner.status_snapshot(allowed_ns.as_deref(), is_admin),
            )
        }
        ("spawner/launch", Method::POST) => {
            let is_admin = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
            if !is_admin {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": "admin role required to spawn local headless workers" }),
                );
            }
            let lreq: crate::spawner::LaunchRequest = match serde_json::from_value(b.clone()) {
                Ok(r) => r,
                Err(e) => {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": format!("invalid launch payload: {}", e) }),
                    )
                }
            };
            match st
                .spawner
                .launch(
                    st.pool.clone(),
                    st.port,
                    pid,
                    st.event_tx.clone(),
                    Arc::clone(&st.channel_notify),
                    lreq,
                )
                .await
            {
                Ok(rec) => json_resp(StatusCode::OK, json!({ "ok": true, "worker": rec })),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("spawner/stop", Method::POST) => {
            let is_admin = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
            if !is_admin {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": "admin role required to stop local headless workers" }),
                );
            }
            let run_id = b.get("run_id").and_then(|v| v.as_u64());
            let handle = b.get("handle").and_then(|v| v.as_str());
            let ns_opt = b.get("namespace").and_then(|v| v.as_str());
            match st.spawner.stop_worker(run_id, handle, ns_opt) {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("session/quota", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let sess = b
                .get("session_name")
                .or_else(|| b.get("name"))
                .or_else(|| b.get("session"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            let paused = b.get("paused").and_then(|v| v.as_bool()).unwrap_or(true);
            let mins = b.get("cooldown_minutes").and_then(|v| v.as_i64()).unwrap_or(60);
            let reason = b.get("reason").and_then(|v| v.as_str()).unwrap_or("");
            if ns.is_empty() || sess.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and session_name required" }),
                );
            }
            match Control::workspace_set_session_quota(&st.pool, pid, ns, sess, paused, mins, reason)
                .await
            {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("session/rename", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let old_s = b.get("old_name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let new_s = b.get("new_name").and_then(|v| v.as_str()).unwrap_or("").trim();
            if ns.is_empty() || old_s.is_empty() || new_s.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace, old_name, and new_name required" }),
                );
            }
            match Control::workspace_rename_session(&st.pool, pid, ns, old_s, new_s).await {
                Ok(v) => {
                    sync_local_session_rename(&v);
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("session", Method::DELETE) => {
            let ns = qs
                .get("namespace")
                .map(|s| s.as_str())
                .or_else(|| b.get("namespace").and_then(|v| v.as_str()))
                .unwrap_or("")
                .trim();
            let sess = qs
                .get("session")
                .map(|s| s.as_str())
                .or_else(|| b.get("session_name").and_then(|v| v.as_str()))
                .unwrap_or("")
                .trim();
            if ns.is_empty() || sess.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and session required" }),
                );
            }
            match Control::workspace_delete_session(&st.pool, pid, ns, sess).await {
                Ok(v) => {
                    st.channel_notify.notify_waiters();
                    json_resp(StatusCode::OK, v)
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("memories", Method::GET) => {
            let ns = qs.get("namespace").map(|s| s.as_str());
            let ftype = qs
                .get("type")
                .map(|s| s.trim().to_lowercase())
                .filter(|s| !s.is_empty());
            if let Some(ref ft) = ftype {
                if !ALLOWED_MEMORY_TYPES.contains(&ft.as_str()) {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": format!("unknown type {:?}", ft) }),
                    );
                }
            }
            let q = qs.get("q").map(|s| s.as_str());
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(50);
            let offset = qs
                .get("offset")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            match Control::workspace_memories(
                &st.pool,
                pid,
                ns,
                ftype.as_deref(),
                q,
                limit,
                offset,
            )
            .await
            {
                Ok(memories) => json_resp(
                    StatusCode::OK,
                    json!({ "memories": memories, "limit": limit, "offset": offset }),
                ),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("activity", Method::GET) => {
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(60);
            let after_id = qs.get("after_id").and_then(|s| s.parse::<i64>().ok());
            match Control::workspace_recent_activity(&st.pool, pid, limit, after_id).await {
                Ok(events) => json_resp(StatusCode::OK, json!({ "events": events })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("constraints", Method::GET) => {
            let ns = qs.get("namespace").map(|s| s.as_str());
            match Control::workspace_constraints(&st.pool, pid, ns).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("constraints/validate", Method::POST) => {
            let ns = b
                .get("namespace")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            let tool_subj = b
                .get("tool_subject")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            let statement = b
                .get("statement")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();

            let mut out = serde_json::Map::new();
            out.insert("ok".to_string(), json!(true));

            if !tool_subj.is_empty() {
                match Control::workspace_validate_tool(&st.pool, pid, ns, tool_subj).await {
                    Ok(tv) => {
                        out.insert("tool_validation".to_string(), tv);
                    }
                    Err(e) => return ctrl_err_resp(e),
                }
            }

            if !statement.is_empty() {
                let cs_data = match Control::workspace_constraints(&st.pool, pid, ns).await {
                    Ok(d) => d,
                    Err(e) => return ctrl_err_resp(e),
                };
                let target_nss: Vec<String> = cs_data
                    .get("target_namespaces")
                    .and_then(|v| v.as_array())
                    .map(|a| {
                        a.iter()
                            .filter_map(|x| x.as_str().map(|s| s.to_string()))
                            .collect()
                    })
                    .unwrap_or_default();

                let corpus_hits = if !target_nss.is_empty() {
                    st.store
                        .corpus_check(&target_nss, statement, 10, None)
                        .await
                        .unwrap_or_default()
                } else {
                    vec![]
                };

                // Also rank pinned constraints by token/substring overlap so prose constraints match deterministically even before corpus ingest
                let pinned = cs_data
                    .get("pinned_constraints")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                let words: Vec<String> = statement
                    .to_lowercase()
                    .split(|c: char| !c.is_alphanumeric() && c != '_' && c != '-')
                    .filter(|w| w.len() >= 3)
                    .map(|w| w.to_string())
                    .collect();
                let mut pinned_matches: Vec<Value> = pinned
                    .into_iter()
                    .filter_map(|mut p| {
                        let txt = p
                            .get("content")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_lowercase();
                        let subj = p
                            .get("subject")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .to_lowercase();
                        let hits = words
                            .iter()
                            .filter(|w| txt.contains(w.as_str()) || subj.contains(w.as_str()))
                            .count();
                        if hits > 0 || words.is_empty() {
                            if let Some(obj) = p.as_object_mut() {
                                obj.insert("matched_terms".to_string(), json!(hits));
                            }
                            Some((hits, p))
                        } else {
                            None
                        }
                    })
                    .collect::<Vec<_>>()
                    .into_iter()
                    .map(|(_, p)| p)
                    .collect();
                pinned_matches.sort_by(|a, b| {
                    let ha = a.get("matched_terms").and_then(|x| x.as_u64()).unwrap_or(0);
                    let hb = b.get("matched_terms").and_then(|x| x.as_u64()).unwrap_or(0);
                    hb.cmp(&ha)
                });
                pinned_matches.truncate(12);

                let reconcile = if let Some(single_ns) = ns.filter(|s| *s != "*") {
                    st.store
                        .reconcile_claim(single_ns, statement, None, None)
                        .await
                        .ok()
                } else {
                    None
                };

                out.insert(
                    "statement_validation".to_string(),
                    json!({
                        "statement": statement,
                        "pinned_matches": pinned_matches,
                        "corpus_matches": corpus_hits,
                        "reconcile": reconcile,
                    }),
                );
            }

            json_resp(StatusCode::OK, Value::Object(out))
        }
        ("constraints/enforcement", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("");
            let rule_text = b.get("rule_text").and_then(|v| v.as_str()).unwrap_or("");
            let level = b
                .get("enforce_level")
                .and_then(|v| v.as_str())
                .unwrap_or("block");
            let matcher = b.get("tool_matcher").and_then(|v| v.as_str()).unwrap_or("");
            match Control::workspace_add_enforcement(&st.pool, pid, ns, rule_text, level, matcher)
                .await
            {
                Ok(id) => json_resp(StatusCode::OK, json!({ "ok": true, "id": id })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("constraints/enforcement", Method::DELETE) => {
            let id = qs
                .get("id")
                .and_then(|s| s.parse::<i64>().ok())
                .or_else(|| b.get("id").and_then(|v| v.as_i64()))
                .unwrap_or(0);
            if id <= 0 {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "id required" }));
            }
            match Control::workspace_remove_enforcement(&st.pool, pid, id).await {
                Ok(removed) => json_resp(StatusCode::OK, json!({ "ok": true, "removed": removed })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("constraints/pin", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let text = b.get("text").and_then(|v| v.as_str()).unwrap_or("").trim();
            let subject = b
                .get("subject")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            if ns.is_empty() || text.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and text required" }),
                );
            }
            match Control::authorize(&st.pool, pid, ns, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required on namespace '{}'", ns) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            let p_name = Control::principal_info(&st.pool, pid)
                .await
                .ok()
                .flatten()
                .and_then(|v| v.get("name").and_then(|s| s.as_str()).map(|s| s.to_string()));
            let _ = Control::create_namespace(&st.pool, ns, Some(pid), None).await;
            match st
                .store
                .remember_ex(
                    ns,
                    text,
                    Some("user"),
                    None,
                    Some("constraint"),
                    subject,
                    p_name.as_deref(),
                    None,
                    None,
                    true,
                    false,
                )
                .await
            {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("constraints/pin", Method::DELETE) => {
            let id = qs
                .get("id")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            let ns = qs.get("namespace").map(|s| s.as_str()).unwrap_or("");
            if id <= 0 || ns.trim().is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "id and namespace required" }),
                );
            }
            match Control::workspace_retire_pinned_constraint(&st.pool, pid, ns, id).await {
                Ok(retired) => json_resp(StatusCode::OK, json!({ "ok": true, "retired": retired })),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespace/detail", Method::GET) => {
            let ns = qs.get("namespace").map(|s| s.as_str()).unwrap_or("").trim();
            if ns.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace required" }),
                );
            }
            match Control::workspace_namespace_detail(&st.pool, pid, ns).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespace/create", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            if name.is_empty()
                || name.len() > 200
                || name == "*"
                || name.contains('*')
                || name.starts_with("secret:")
            {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "valid concrete namespace name required (<=200 chars)" }),
                );
            }
            match Control::authorize(&st.pool, pid, name, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required to create namespace '{}'", name) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            let desc = b
                .get("description")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            if let Err(e) = Control::create_namespace(&st.pool, name, Some(pid), desc).await {
                return ctrl_err_resp(e);
            }
            let kind = b
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("memory")
                .trim();
            if matches!(kind, "memory" | "knowledge") {
                if let Err(e) = Control::set_namespace_kind(&st.pool, name, kind).await {
                    return ctrl_err_resp(e);
                }
            }
            if let Some(inh) = b.get("inherit_ancestors").and_then(|v| v.as_bool()) {
                let _ = Control::set_namespace_inherit_ancestors(&st.pool, name, inh).await;
            }
            if let Some(dst) = b
                .get("link_to")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty() && *s != name)
            {
                let lkind = b
                    .get("link_kind")
                    .and_then(|v| v.as_str())
                    .unwrap_or("link")
                    .trim();
                if Control::authorize(&st.pool, pid, dst, false)
                    .await
                    .unwrap_or(false)
                {
                    let _ = Control::link_namespaces(&st.pool, name, dst, Some(pid), lkind).await;
                }
            }
            if let Some(bkey) = b
                .get("bind_key")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
            {
                let ktype = b
                    .get("bind_key_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("repo")
                    .trim();
                let _ = Control::upsert_binding(&st.pool, pid, ktype, bkey, name, None).await;
            }
            if let Ok(mut g) = st.ns_cache.lock() {
                *g = None;
            }
            let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": name }));
            json_resp(StatusCode::OK, json!({ "ok": true, "name": name }))
        }
        ("namespace/update", Method::POST) => {
            let name = b.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            if name.is_empty() || name == "*" || name.starts_with("secret:") {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "concrete namespace name required" }),
                );
            }
            match Control::authorize(&st.pool, pid, name, true).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::FORBIDDEN,
                        json!({ "error": format!("write grant required on namespace '{}'", name) }),
                    )
                }
                Err(e) => return ctrl_err_resp(e),
            }
            if let Some(desc) = b.get("description").and_then(|v| v.as_str()) {
                if let Err(e) =
                    Control::create_namespace(&st.pool, name, Some(pid), Some(desc.trim())).await
                {
                    return ctrl_err_resp(e);
                }
            }
            if let Some(kind) = b.get("kind").and_then(|v| v.as_str()).map(|s| s.trim()) {
                if matches!(kind, "memory" | "knowledge") {
                    if let Err(e) = Control::set_namespace_kind(&st.pool, name, kind).await {
                        return ctrl_err_resp(e);
                    }
                }
            }
            if let Some(inh) = b.get("inherit_ancestors").and_then(|v| v.as_bool()) {
                if let Err(e) = Control::set_namespace_inherit_ancestors(&st.pool, name, inh).await
                {
                    return ctrl_err_resp(e);
                }
            }
            if let Ok(mut g) = st.ns_cache.lock() {
                *g = None;
            }
            let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": name }));
            json_resp(StatusCode::OK, json!({ "ok": true, "name": name }))
        }
        ("namespace/link", Method::POST) => {
            let src = b.get("src").and_then(|v| v.as_str()).unwrap_or("").trim();
            let dst = b.get("dst").and_then(|v| v.as_str()).unwrap_or("").trim();
            let kind = b
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("link")
                .trim();
            if src.is_empty() || dst.is_empty() || src == dst {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "distinct src and dst namespaces required" }),
                );
            }
            if !Control::authorize(&st.pool, pid, src, true)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("write grant required on '{}'", src) }),
                );
            }
            if !Control::authorize(&st.pool, pid, dst, false)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("read grant required on target '{}'", dst) }),
                );
            }
            match Control::link_namespaces(&st.pool, src, dst, Some(pid), kind).await {
                Ok(()) => {
                    if let Ok(mut g) = st.ns_cache.lock() {
                        *g = None;
                    }
                    let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": src }));
                    json_resp(
                        StatusCode::OK,
                        json!({ "ok": true, "src": src, "dst": dst, "kind": kind }),
                    )
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespace/unlink", Method::POST) => {
            let src = b.get("src").and_then(|v| v.as_str()).unwrap_or("").trim();
            let dst = b.get("dst").and_then(|v| v.as_str()).unwrap_or("").trim();
            if src.is_empty() || dst.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "src and dst required" }),
                );
            }
            if !Control::authorize(&st.pool, pid, src, true)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("write grant required on '{}'", src) }),
                );
            }
            match Control::unlink_namespaces(&st.pool, src, dst).await {
                Ok(removed) => {
                    if let Ok(mut g) = st.ns_cache.lock() {
                        *g = None;
                    }
                    let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": src }));
                    json_resp(StatusCode::OK, json!({ "ok": true, "removed": removed }))
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespace/bind", Method::POST) => {
            let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("").trim();
            let ktype = b
                .get("key_type")
                .and_then(|v| v.as_str())
                .unwrap_or("repo")
                .trim();
            let key = b.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
            let host_id = b
                .get("host_id")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());
            if ns.is_empty() || key.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "namespace and key required" }),
                );
            }
            if !Control::authorize(&st.pool, pid, ns, true)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("write grant required on '{}'", ns) }),
                );
            }
            match Control::upsert_binding(&st.pool, pid, ktype, key, ns, host_id).await {
                Ok(v) => {
                    let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": ns }));
                    json_resp(StatusCode::OK, json!({ "ok": true, "binding": v }))
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("namespace/unbind", Method::POST) => {
            let id = b
                .get("id")
                .and_then(|v| v.as_i64())
                .or_else(|| qs.get("id").and_then(|s| s.parse::<i64>().ok()))
                .unwrap_or(0);
            if id <= 0 {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "id required" }));
            }
            let is_adm = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
            let removed = if is_adm {
                if let Ok(c) = st.pool.get().await {
                    c.execute("DELETE FROM memnos_control.bindings WHERE id = $1", &[&id])
                        .await
                        .map(|n| n > 0)
                        .unwrap_or(false)
                } else {
                    false
                }
            } else {
                Control::delete_binding(&st.pool, pid, id)
                    .await
                    .unwrap_or(false)
            };
            let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": "*" }));
            json_resp(StatusCode::OK, json!({ "ok": true, "removed": removed }))
        }
        ("namespace/transfer", Method::POST) => {
            let src = b.get("src").and_then(|v| v.as_str()).unwrap_or("").trim();
            let dst = b.get("dst").and_then(|v| v.as_str()).unwrap_or("").trim();
            let mode = b
                .get("mode")
                .and_then(|v| v.as_str())
                .unwrap_or("merge")
                .trim();
            let dry_run = b.get("dry_run").and_then(|v| v.as_bool()).unwrap_or(false);
            let like_filter = b
                .get("like_filter")
                .and_then(|v| v.as_str())
                .map(|s| s.trim())
                .filter(|s| !s.is_empty());

            if src.is_empty() || src == "*" || src.starts_with("secret:") {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "concrete source namespace required" }),
                );
            }
            if !matches!(mode, "merge" | "move" | "copy") {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "mode must be 'merge', 'move', or 'copy'" }),
                );
            }

            if dry_run {
                return match Control::workspace_namespace_transfer_preview(
                    &st.pool,
                    pid,
                    src,
                    like_filter,
                )
                .await
                {
                    Ok(v) => json_resp(StatusCode::OK, v),
                    Err(e) => ctrl_err_resp(e),
                };
            }

            if dst.is_empty()
                || dst.len() > 200
                || dst == "*"
                || dst.contains('*')
                || dst.starts_with("secret:")
                || dst == src
            {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "distinct concrete target namespace required" }),
                );
            }

            let need_src_write = mode != "copy";
            if !Control::authorize(&st.pool, pid, src, need_src_write)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({
                        "error": format!(
                            "{} grant required on source namespace '{}'",
                            if need_src_write { "write" } else { "read" },
                            src
                        )
                    }),
                );
            }
            if !Control::authorize(&st.pool, pid, dst, true)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("write grant required on target namespace '{}'", dst) }),
                );
            }

            let is_adm = Control::is_admin(&st.pool, pid).await.unwrap_or(false);
            let include_memories = b
                .get("include_memories")
                .and_then(|v| v.as_bool())
                .unwrap_or(true);
            let include_coordination = b
                .get("include_coordination")
                .and_then(|v| v.as_bool())
                .unwrap_or(mode != "copy");
            let include_routing = b
                .get("include_routing")
                .and_then(|v| v.as_bool())
                .unwrap_or(mode == "merge");
            let delete_source = b
                .get("delete_source")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);

            if let Err(e) = Control::create_namespace(&st.pool, dst, Some(pid), None).await {
                return ctrl_err_resp(e);
            }

            let mem_res = if include_memories {
                let mem_mode = if mode == "copy" { "copy" } else { "move" };
                match st
                    .store
                    .namespace_copy(src, dst, mem_mode, like_filter, Some(pid))
                    .await
                {
                    Ok(v) => v,
                    Err(e) => return json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
                }
            } else {
                json!({ "raw_turns": 0, "facts": 0, "episodes": 0 })
            };

            let ctrl_res = if mode != "copy" && (include_coordination || include_routing) {
                match Control::workspace_transfer_control_plane(
                    &st.pool,
                    pid,
                    is_adm,
                    src,
                    dst,
                    like_filter,
                    include_coordination,
                    include_routing,
                )
                .await
                {
                    Ok(v) => v,
                    Err(e) => return ctrl_err_resp(e),
                }
            } else {
                json!({
                    "channels_merged": 0,
                    "channel_messages_moved": 0,
                    "channel_subs_merged": 0,
                    "sessions_merged": 0,
                    "inbox_moved": 0,
                    "bindings_repointed": 0,
                    "guardrails_merged": 0,
                    "links_merged": 0,
                })
            };

            let mut source_deleted = false;
            if delete_source && mode == "merge" && like_filter.is_none() && is_adm {
                if Control::delete_namespace(&st.pool, src, true).await.is_ok() {
                    source_deleted = true;
                }
            }

            if let Ok(mut g) = st.ns_cache.lock() {
                *g = None;
            }
            st.channel_notify.notify_waiters();
            let _ = st.event_tx.send(json!({
                "type": "activity",
                "op": format!("namespace/{}", mode),
                "namespace": dst,
                "actor": "workspace",
                "detail": format!("{} -> {}", src, dst),
                "ok": true,
            }));
            let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": "*" }));

            json_resp(
                StatusCode::OK,
                json!({
                    "ok": true,
                    "mode": mode,
                    "src": src,
                    "dst": dst,
                    "memories": mem_res,
                    "coordination": ctrl_res,
                    "source_deleted": source_deleted,
                }),
            )
        }
        ("namespace", Method::DELETE) => {
            if !Control::is_admin(&st.pool, pid).await.unwrap_or(false) {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": "admin role required to delete namespaces" }),
                );
            }
            let ns = qs
                .get("namespace")
                .or_else(|| qs.get("name"))
                .map(|s| s.as_str())
                .or_else(|| b.get("namespace").and_then(|v| v.as_str()))
                .unwrap_or("")
                .trim();
            let purge = qs
                .get("purge")
                .map(|s| s == "1" || s == "true")
                .or_else(|| b.get("purge").and_then(|v| v.as_bool()))
                .unwrap_or(false);
            if ns.is_empty() || ns == "*" || ns.starts_with("secret:") {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "concrete namespace required" }),
                );
            }
            match Control::delete_namespace(&st.pool, ns, purge).await {
                Ok(()) => {
                    if let Ok(mut g) = st.ns_cache.lock() {
                        *g = None;
                    }
                    let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": "*" }));
                    json_resp(StatusCode::OK, json!({ "ok": true, "deleted": ns, "purged": purge }))
                }
                Err(e) => ctrl_err_resp(e),
            }
        }
        ("doctor", Method::GET) => {
            let ns_opt = qs.get("namespace").map(|s| s.as_str());
            match crate::doctor::run_doctor_scan(&st.pool, Some(pid), ns_opt).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("Forbidden:") => json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": e.trim_start_matches("Forbidden:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("doctor/run", Method::POST) => {
            let ns_opt = b
                .get("namespace")
                .and_then(|v| v.as_str())
                .or_else(|| qs.get("namespace").map(|s| s.as_str()));
            let dry_run = b.get("dry_run").and_then(|v| v.as_bool()).unwrap_or(false);
            let actions: Vec<String> = b
                .get("actions")
                .and_then(|v| v.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter_map(|x| x.as_str().map(|s| s.trim().to_string()))
                        .filter(|s| !s.is_empty())
                        .collect()
                })
                .or_else(|| {
                    b.get("action")
                        .and_then(|v| v.as_str())
                        .map(|s| vec![s.trim().to_string()])
                })
                .unwrap_or_else(|| vec!["all".to_string()]);

            match crate::doctor::execute_doctor_actions(
                &st.pool,
                &st.store,
                Some(pid),
                ns_opt,
                &actions,
                dry_run,
                Some(&st.event_tx),
            )
            .await
            {
                Ok(v) => {
                    if !dry_run {
                        if let Ok(mut g) = st.ns_cache.lock() {
                            *g = None;
                        }
                        st.channel_notify.notify_waiters();
                        let _ = st.event_tx.send(json!({
                            "type": "activity",
                            "op": "doctor/run",
                            "namespace": ns_opt.unwrap_or("*"),
                            "actor": "doctor",
                            "detail": actions.join(","),
                            "ok": true,
                        }));
                        let _ = st.event_tx.send(json!({ "type": "refresh", "namespace": "*" }));
                    }
                    json_resp(StatusCode::OK, v)
                }
                Err(e) if e.starts_with("Forbidden:") => json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": e.trim_start_matches("Forbidden:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("chat/threads", Method::GET) => match st.copilot.list_threads(&st.pool, pid).await {
            Ok(v) => json_resp(StatusCode::OK, v),
            Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
        },
        ("chat/messages", Method::GET) => {
            let tid = qs
                .get("thread_id")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            if tid <= 0 {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "thread_id required" }),
                );
            }
            match st.copilot.get_thread_messages(&st.pool, pid, tid).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("NotFound:") => json_resp(
                    StatusCode::NOT_FOUND,
                    json!({ "error": e.trim_start_matches("NotFound:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("chat/digest", Method::GET) => {
            let ns_opt = qs.get("namespace").map(|s| s.as_str());
            let hrs = qs
                .get("hours")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(8);
            match crate::copilot::CopilotEngine::build_digest(&st.pool, pid, ns_opt, hrs).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("Forbidden:") => json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": e.trim_start_matches("Forbidden:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("chat/send", Method::POST) => {
            let creq: crate::copilot::CopilotSendRequest = match serde_json::from_value(b.clone()) {
                Ok(r) => r,
                Err(e) => {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": format!("invalid chat payload: {}", e) }),
                    )
                }
            };
            match st
                .copilot
                .send_message(
                    st.pool.clone(),
                    st.port,
                    pid,
                    st.event_tx.clone(),
                    Arc::clone(&st.channel_notify),
                    creq,
                )
                .await
            {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("Forbidden:") => json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": e.trim_start_matches("Forbidden:").trim() }),
                ),
                Err(e) if e.starts_with("NotFound:") => json_resp(
                    StatusCode::NOT_FOUND,
                    json!({ "error": e.trim_start_matches("NotFound:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("chat/stop", Method::POST) => {
            let tid = b
                .get("thread_id")
                .and_then(|v| v.as_i64())
                .or_else(|| qs.get("thread_id").and_then(|s| s.parse::<i64>().ok()))
                .unwrap_or(0);
            if tid <= 0 {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "thread_id required" }),
                );
            }
            match st.copilot.stop_thread(&st.pool, pid, tid).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("NotFound:") => json_resp(
                    StatusCode::NOT_FOUND,
                    json!({ "error": e.trim_start_matches("NotFound:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        ("chat/thread", Method::DELETE) => {
            let tid = qs
                .get("thread_id")
                .and_then(|s| s.parse::<i64>().ok())
                .or_else(|| b.get("thread_id").and_then(|v| v.as_i64()))
                .unwrap_or(0);
            if tid <= 0 {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "thread_id required" }),
                );
            }
            match st.copilot.delete_thread(&st.pool, pid, tid).await {
                Ok(v) => json_resp(StatusCode::OK, v),
                Err(e) if e.starts_with("NotFound:") => json_resp(
                    StatusCode::NOT_FOUND,
                    json!({ "error": e.trim_start_matches("NotFound:").trim() }),
                ),
                Err(e) => json_resp(StatusCode::BAD_REQUEST, json!({ "error": e })),
            }
        }
        _ => json_resp(
            StatusCode::NOT_FOUND,
            json!({ "error": "unknown workspace route" }),
        ),
    }
}

async fn api_post_handler(
    State(st): State<AppState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return json_resp(StatusCode::NOT_FOUND, json!({ "error": "not found" }));
    }
    let t0 = Instant::now();
    let path = uri.path();
    let action = path.trim_start_matches('/');

    let req: Value = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(&body) {
            Ok(v) if v.is_object() => v,
            _ => return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "invalid json" })),
        }
    };

    let tok = bearer_token(&headers).unwrap_or_default();
    let pid = match Control::authenticate(&st.pool, &tok).await {
        Ok(Some(p)) => p,
        Ok(None) => return json_resp(StatusCode::UNAUTHORIZED, json!({ "error": "unauthorized" })),
        Err(e) => return ctrl_err_resp(e),
    };

    if requires_embedding_setup(path) {
        let configured = st.embed_configured.load(Ordering::Acquire);
        let models_ready = configured
            && crate::rerank::models_ready_for_server(st.embedder.is_openai());
        if !models_ready {
            let reason = if !configured {
                st.setup_reason
                    .lock()
                    .ok()
                    .map(|g| g.clone())
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| {
                        "memnos is not set up yet, run `memnos setup embeddings`".to_string()
                    })
            } else if let Some(err) = crate::rerank::prewarm_error() {
                err
            } else {
                "memnos models are still loading — wait for /readyz before querying".to_string()
            };
            return json_resp(StatusCode::SERVICE_UNAVAILABLE, json!({ "error": reason }));
        }
    }

    let principal_name = Control::principal_info(&st.pool, pid)
        .await
        .ok()
        .flatten()
        .and_then(|v| v.get("name").and_then(|s| s.as_str()).map(|s| s.to_string()));

    let actor_model_hint = req
        .get("actor_model")
        .or_else(|| req.get("model"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let actor_hint = req
        .get("actor_session")
        .or_else(|| req.get("session_name"))
        .or_else(|| req.get("name"))
        .or_else(|| req.get("sender_session"))
        .or_else(|| req.get("from_session"))
        .or_else(|| req.get("agent"))
        .or_else(|| req.get("author"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let speaker_prefix = req
        .get("speaker")
        .and_then(|v| v.as_str())
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(|s| format!("[{}] ", s))
        .unwrap_or_default();
    let op_detail = req
        .get("query")
        .or_else(|| req.get("channel"))
        .or_else(|| req.get("statement"))
        .or_else(|| req.get("subject"))
        .or_else(|| req.get("to_session"))
        .or_else(|| req.get("content"))
        .or_else(|| req.get("text"))
        .and_then(|v| v.as_str())
        .map(|s| {
            let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
            format!("{}{}", speaker_prefix, flat)
                .chars()
                .take(84)
                .collect::<String>()
        })
        .unwrap_or_default();

    let raw_ns = req
        .get("namespace")
        .and_then(|v| v.as_str())
        .or_else(|| {
            headers
                .get("x-memnos-namespace")
                .and_then(|h| h.to_str().ok())
        })
        .map(|s| s.trim())
        .filter(|s| !s.is_empty());

    let scope_wide = path == "/recall/wide"
        || matches!(
            req.get("scope").and_then(|v| v.as_str()),
            Some("all" | "wide")
        );
    let ns_optional = (scope_wide
        && matches!(
            path,
            "/recall" | "/recall/wide" | "/recall_v2" | "/memory/search" | "/memory/context"
        ))
        || path == "/recall/explain";
    let ns = match raw_ns {
        Some(s) => s.to_string(),
        None if ns_optional => "default".to_string(),
        None => {
            return json_resp(
                StatusCode::BAD_REQUEST,
                json!({ "error": "namespace required" }),
            );
        }
    };

    let write = is_write_op(path);

    if !(scope_wide
        && matches!(
            path,
            "/recall" | "/recall/wide" | "/recall_v2" | "/memory/search" | "/memory/context"
        ))
        && path != "/recall/explain"
    {
        match Control::authorize(&st.pool, pid, &ns, write).await {
            Ok(true) => {}
            Ok(false) => {
                let _ = Control::audit(
                    &st.pool,
                    Some(pid),
                    action,
                    Some(&ns),
                    false,
                    Some(json!({ "reason": "forbidden" })),
                    Some(t0.elapsed().as_millis() as i32),
                    None,
                    Some(403),
                )
                .await;
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": format!("forbidden for namespace {:?}", ns) }),
                );
            }
            Err(e) => return ctrl_err_resp(e),
        }
    }

    let result: Result<(StatusCode, Value), Response> = match path {
        "/remember" | "/memory/write" => {
            let text = req
                .get("text")
                .or_else(|| req.get("content"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if text.is_empty() {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "text required" }));
            }
            let mtype: Option<&str> = match req.get("type").or_else(|| req.get("memory_type")) {
                None | Some(Value::Null) => None,
                Some(Value::String(s)) if s.trim().is_empty() => None,
                Some(Value::String(s)) => {
                    let t = s.trim();
                    if !ALLOWED_MEMORY_TYPES.contains(&t) {
                        return json_resp(
                            StatusCode::BAD_REQUEST,
                            json!({ "error": format!("unknown memory type {:?}", t) }),
                        );
                    }
                    Some(t)
                }
                _ => {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": "type must be a string" }),
                    );
                }
            };
            let speaker = req.get("speaker").and_then(|v| v.as_str()).or(Some("user"));
            let session_id = req
                .get("session_id")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let csubj = req
                .get("constraint_subject")
                .or_else(|| req.get("subject"))
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let code_anchors = req.get("code_anchors").cloned();
            let do_extract = req.get("extract").and_then(|v| v.as_bool()).unwrap_or(true);
            let async_mode = req.get("async").and_then(|v| v.as_bool()).unwrap_or(false);

            let _ = Control::create_namespace(&st.pool, &ns, Some(pid), None).await;

            match st
                .store
                .remember_ex(
                    &ns,
                    text,
                    speaker,
                    session_id,
                    mtype,
                    csubj,
                    principal_name.as_deref(),
                    None,
                    code_anchors,
                    do_extract,
                    async_mode,
                )
                .await
            {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/recall" | "/recall/wide" | "/recall_v2" | "/memory/search" | "/memory/context" => {
            let query = req
                .get("query")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if query.is_empty() {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "query required" }));
            }
            let type_filter: Option<String> = match req.get("type") {
                None | Some(Value::Null) => None,
                Some(Value::String(s)) if s.trim().is_empty() => None,
                Some(Value::String(s)) => {
                    let t = s.trim();
                    if !ALLOWED_MEMORY_TYPES.contains(&t) {
                        return json_resp(
                            StatusCode::BAD_REQUEST,
                            json!({ "error": format!("unknown memory type {:?}", t) }),
                        );
                    }
                    Some(t.to_string())
                }
                _ => {
                    return json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": "type must be a string" }),
                    );
                }
            };
            let constraint_cap: Option<usize> = match req.get("constraint_cap") {
                None | Some(Value::Null) => None,
                Some(v) => match v.as_i64() {
                    Some(n) => Some(n.max(0) as usize),
                    None => {
                        return json_resp(
                            StatusCode::BAD_REQUEST,
                            json!({ "error": "constraint_cap must be an integer" }),
                        );
                    }
                },
            };
            let deadline: Option<Instant> = match req.get("deadline_ms") {
                None | Some(Value::Null) => None,
                Some(v) => match v.as_i64() {
                    Some(ms) if ms < 0 => Some(t0),
                    Some(0) => None,
                    Some(ms) => Some(t0 + Duration::from_millis(ms as u64)),
                    None => {
                        return json_resp(
                            StatusCode::BAD_REQUEST,
                            json!({ "error": "deadline_ms must be an integer" }),
                        );
                    }
                },
            };
            let limit: Option<usize> = match req.get("limit") {
                None | Some(Value::Null) => None,
                Some(v) => match v.as_i64() {
                    Some(n) => Some(n.max(0) as usize),
                    None => {
                        return json_resp(
                            StatusCode::BAD_REQUEST,
                            json!({ "error": "limit must be an integer" }),
                        );
                    }
                },
            };
            let k = req.get("k").and_then(|v| v.as_u64()).unwrap_or(40) as usize;
            let raw_quota = req.get("raw_quota").and_then(|v| v.as_u64()).unwrap_or(11) as usize;
            let fact_quota = req.get("fact_quota").and_then(|v| v.as_u64()).unwrap_or(8) as usize;
            let max_chars = req
                .get("max_chars")
                .and_then(|v| v.as_u64())
                .unwrap_or(9000) as usize;
            let subject = req
                .get("subject")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty());
            let author_filter = req
                .get("author")
                .and_then(|v| v.as_str())
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty());
            let repo_root = req
                .get("repo_root")
                .or_else(|| req.get("cwd"))
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(PathBuf::from);

            if scope_wide {
                let nss = match Control::readable_namespaces(&st.pool, pid, None, Some(200)).await {
                    Ok(v) => v,
                    Err(e) => return ctrl_err_resp(e),
                };
                if nss.is_empty() {
                    return json_resp(
                        StatusCode::OK,
                        json!({
                            "query": query,
                            "context": "",
                            "results": [],
                            "memories": [],
                            "namespaces_searched": [],
                            "count": 0
                        }),
                    );
                }
                let mut ordered = Vec::with_capacity(nss.len());
                if nss.contains(&ns) {
                    ordered.push(ns.clone());
                }
                for n in nss {
                    if !ordered.contains(&n) {
                        ordered.push(n);
                    }
                }
                let ov = Control::get_constraint_overrides(&st.pool, &ordered)
                    .await
                    .unwrap_or_default()
                    .into_iter()
                    .collect::<HashSet<_>>();
                let opts = RecallOpts {
                    k: k.clamp(1, 100),
                    raw_quota,
                    fact_quota,
                    max_chars,
                    ancestors: vec![],
                    inherited_skipped: vec![],
                    grounded_in: vec![],
                    links_skipped: vec![],
                    overrides: ov,
                    subject,
                    type_filter,
                    author_filter,
                    constraint_cap,
                    limit,
                    deadline,
                    viewer: principal_name.clone(),
                    repo_root,
                };
                match st.store.recall_wide(&ordered, query, &opts).await {
                    Ok(v) => Ok((StatusCode::OK, v)),
                    Err(e) => Err(json_resp(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "error": e }),
                    )),
                }
            } else {
                let mut grounded_in = Vec::new();
                let mut links_skipped = Vec::new();
                if let Ok(linked) = Control::linked_namespaces(&st.pool, &ns).await {
                    for lns in linked {
                        if Control::authorize(&st.pool, pid, &lns, false)
                            .await
                            .unwrap_or(false)
                        {
                            grounded_in.push(lns);
                        } else {
                            links_skipped.push(lns);
                        }
                    }
                }
                let mut inherited = Vec::new();
                let mut inherited_skipped = Vec::new();
                if constraint_cap != Some(0) {
                    if let Ok(ancs) = Control::effective_ancestors(&st.pool, &ns).await {
                        for ans in ancs {
                            if Control::authorize(&st.pool, pid, &ans, false)
                                .await
                                .unwrap_or(false)
                            {
                                inherited.push(ans);
                            } else {
                                inherited_skipped.push(ans);
                            }
                        }
                    }
                }
                let mut all_nss = vec![ns.clone()];
                all_nss.extend(grounded_in.iter().cloned());
                all_nss.extend(inherited.iter().cloned());
                let ov = Control::get_constraint_overrides(&st.pool, &all_nss)
                    .await
                    .unwrap_or_default()
                    .into_iter()
                    .collect::<HashSet<_>>();
                let opts = RecallOpts {
                    k: k.clamp(1, 100),
                    raw_quota,
                    fact_quota,
                    max_chars,
                    ancestors: inherited,
                    inherited_skipped,
                    grounded_in,
                    links_skipped,
                    overrides: ov,
                    subject,
                    type_filter,
                    author_filter,
                    constraint_cap,
                    limit,
                    deadline,
                    viewer: principal_name.clone(),
                    repo_root,
                };
                let res = match path {
                    "/memory/search" => st.store.memory_search(&ns, query, &opts).await,
                    "/memory/context" => st.store.memory_context(&ns, query, &opts).await,
                    _ => st.store.recall(&ns, query, &opts).await,
                };
                match res {
                    Ok(mut v) => {
                        let inc_peers = req
                            .get("include_peers")
                            .and_then(|x| x.as_bool())
                            .unwrap_or(false);
                        let poll_inb = req
                            .get("poll_inbox")
                            .and_then(|x| x.as_bool())
                            .unwrap_or(false);
                        let sess_n = req
                            .get("session_name")
                            .and_then(|x| x.as_str())
                            .unwrap_or("")
                            .trim();
                        if inc_peers || poll_inb || !sess_n.is_empty() {
                            if let Ok(pd) = Control::discover(&st.pool, &ns).await {
                                v["peer_directory"] = pd;
                            }
                        }
                        if poll_inb && !sess_n.is_empty() {
                            if let Ok(pr) =
                                Control::channel_poll(&st.pool, &ns, sess_n, pid, None, false, 50)
                                    .await
                            {
                                v["inbox"] = pr
                                    .get("messages")
                                    .cloned()
                                    .unwrap_or_else(|| json!([]));
                            }
                        }
                        Ok((StatusCode::OK, v))
                    }
                    Err(e) => Err(json_resp(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "error": e }),
                    )),
                }
            }
        }
        "/recall/explain" => {
            let limit = req
                .get("limit")
                .and_then(|v| v.as_u64())
                .unwrap_or(5)
                .clamp(1, 50) as usize;
            let entries = st.store.recall_explain(limit);
            Ok((
                StatusCode::OK,
                json!({ "entries": entries, "count": entries.len() }),
            ))
        }
        "/memory/delete" => {
            let id = req
                .get("id")
                .or_else(|| req.get("semantic_id"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            if id <= 0 {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "id required" }));
            }
            match st.store.memory_delete(&ns, id).await {
                Ok(v) => {
                    if v.get("deleted").and_then(|x| x.as_bool()).unwrap_or(false) {
                        Ok((
                            StatusCode::OK,
                            json!({
                                "ok": true,
                                "deleted": 1,
                                "id": id,
                                "statement": v.get("statement").cloned().unwrap_or(Value::Null),
                            }),
                        ))
                    } else {
                        Ok((
                            StatusCode::NOT_FOUND,
                            json!({ "error": "memory not found" }),
                        ))
                    }
                }
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/memory/by_type" => {
            let mtype = req
                .get("type")
                .or_else(|| req.get("memory_type"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if mtype.is_empty() || !ALLOWED_MEMORY_TYPES.contains(&mtype) {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": format!("unknown memory type {:?}", mtype) }),
                );
            }
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
            match st.store.list_by_type(&ns, mtype, limit).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/reconcile" => {
            let statement = req
                .get("statement")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if statement.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "statement required" }),
                );
            }
            let subj = req
                .get("subject")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let pred = req
                .get("predicate")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            match st.store.reconcile_claim(&ns, statement, subj, pred).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e }),
                )),
            }
        }
        "/contradictions" => match st.store.check_contradictions(&ns).await {
            Ok(v) => Ok((StatusCode::OK, v)),
            Err(e) => Err(json_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "error": e }),
            )),
        },
        "/knowledge/health" => match st.store.knowledge_health(&ns).await {
            Ok(v) => Ok((StatusCode::OK, v)),
            Err(e) => Err(json_resp(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "error": e }),
            )),
        },
        "/feedback" => {
            let query = req.get("query").and_then(|v| v.as_str()).unwrap_or("");
            let helpful = req.get("helpful").and_then(|v| v.as_bool()).unwrap_or(true);
            let note = req.get("note").and_then(|v| v.as_str());
            let _ = Control::record_feedback(&st.pool, Some(pid), &ns, query, helpful, note).await;
            Ok((StatusCode::OK, json!({ "ok": true })))
        }
        "/entity" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let depth = req.get("depth").and_then(|v| v.as_i64()).unwrap_or(1) as i32;
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
            match st.store.get_entity(&ns, name, depth, limit).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((StatusCode::NOT_FOUND, json!({ "error": "entity not found" }))),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/entity/dossier" => {
            let name = req
                .get("entity")
                .or_else(|| req.get("name"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            match st.store.get_entity_dossier(&ns, name).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((StatusCode::NOT_FOUND, json!({ "error": "dossier not found" }))),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/related" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(30);
            match st.store.get_related(&ns, name, limit).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/graph" => {
            let entities: Vec<String> = req
                .get("entities")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .or_else(|| {
                    req.get("entity")
                        .and_then(|v| v.as_str())
                        .map(|s| vec![s.to_string()])
                })
                .unwrap_or_default();
            let hops = req.get("hops").and_then(|v| v.as_i64()).unwrap_or(2) as i32;
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
            match st.store.graph_query(&ns, &entities, hops, limit).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/community" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("").trim();
            let max_nodes = req.get("max_nodes").and_then(|v| v.as_u64()).unwrap_or(25) as usize;
            match st.store.community_search(&ns, name, max_nodes).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((
                    StatusCode::NOT_FOUND,
                    json!({ "error": "community not found" }),
                )),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/provenance" => {
            let id = req
                .get("fact_id")
                .or_else(|| req.get("id"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            match st.store.get_provenance(&ns, id).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((StatusCode::NOT_FOUND, json!({ "error": "fact not found" }))),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/ingest/file" => {
            let filename = req
                .get("filename")
                .or_else(|| req.get("path"))
                .or_else(|| req.get("name"))
                .and_then(|v| v.as_str())
                .unwrap_or("document")
                .trim();
            let text_opt: Option<String> = req
                .get("text")
                .or_else(|| req.get("content"))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .or_else(|| {
                    use base64::Engine;
                    let b64 = req.get("content_b64")?.as_str()?;
                    let bytes = base64::engine::general_purpose::STANDARD.decode(b64.trim()).ok()?;
                    Some(String::from_utf8_lossy(&bytes).to_string())
                });
            let Some(text) = text_opt.filter(|t| !t.trim().is_empty()) else {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "text or content_b64 required" }),
                );
            };
            let chunk_size = req
                .get("chunk_size")
                .and_then(|v| v.as_u64())
                .unwrap_or(1500) as usize;
            let do_extract = req.get("extract").and_then(|v| v.as_bool()).unwrap_or(false);
            let _ = Control::create_namespace(&st.pool, &ns, Some(pid), None).await;
            match st
                .store
                .ingest_file_opts(
                    &ns,
                    filename,
                    &text,
                    chunk_size,
                    do_extract,
                    principal_name.as_deref(),
                )
                .await
            {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/episode/segment" => {
            let gap = req
                .get("gap_minutes")
                .and_then(|v| v.as_i64())
                .unwrap_or(30);
            let max_ep = req
                .get("max_episodes")
                .and_then(|v| v.as_u64())
                .unwrap_or(20) as usize;
            match st.store.segment_episodes(&ns, gap, max_ep).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e }),
                )),
            }
        }
        "/episode/recall" => {
            let q = req.get("query").and_then(|v| v.as_str()).unwrap_or("");
            let k = req.get("k").and_then(|v| v.as_i64()).unwrap_or(5);
            match st.store.recall_episodes(&ns, q, k).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e }),
                )),
            }
        }
        "/episode" => {
            let id = req
                .get("episode_id")
                .or_else(|| req.get("id"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            match st.store.get_episode(&ns, id).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((
                    StatusCode::NOT_FOUND,
                    json!({ "error": "episode not found" }),
                )),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/episode/decay" => {
            let hl = req
                .get("half_life_days")
                .and_then(|v| v.as_f64())
                .unwrap_or(30.0);
            match st.store.decay_episodes(&ns, hl).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e }),
                )),
            }
        }
        "/consolidate" => {
            let _ = Control::channel_cleanup(&st.pool, &ns, 7, 30, false).await;
            match st.store.consolidate(&ns).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "error": e }),
                )),
            }
        }
        "/namespace/copy" => {
            let src = req
                .get("src")
                .or_else(|| req.get("source"))
                .or_else(|| req.get("from_namespace"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if src.is_empty() || src == ns {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "src and dst must be two different non-empty namespaces" }),
                );
            }
            if !Control::authorize(&st.pool, pid, src, false)
                .await
                .unwrap_or(false)
            {
                return json_resp(
                    StatusCode::FORBIDDEN,
                    json!({ "error": "forbidden for source namespace" }),
                );
            }
            let mode = req.get("mode").and_then(|v| v.as_str()).unwrap_or("copy");
            let like_f = req.get("like").and_then(|v| v.as_str());
            let _ = Control::create_namespace(&st.pool, &ns, Some(pid), None).await;
            match st
                .store
                .namespace_copy(src, &ns, mode, like_f, Some(pid))
                .await
            {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/subscribe" => {
            let wh = req
                .get("webhook")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            Control::subscribe(&st.pool, pid, &ns, wh)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/feed" => {
            let sid = req
                .get("subscription_id")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
            match Control::feed(&st.pool, pid, sid, &ns, limit).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((
                    StatusCode::NOT_FOUND,
                    json!({ "error": "subscription not found" }),
                )),
                Err(e) => Err(ctrl_err_resp(e)),
            }
        }
        "/unsubscribe" => {
            let sid = req
                .get("subscription_id")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            Control::unsubscribe(&st.pool, pid, sid)
                .await
                .map(|ok| (StatusCode::OK, json!({ "unsubscribed": ok, "ok": ok })))
                .map_err(ctrl_err_resp)
        }
        "/corpus/ingest" => {
            let source = req
                .get("source")
                .or_else(|| req.get("name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let text = req
                .get("text")
                .or_else(|| req.get("content"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let since = req.get("since").and_then(|v| v.as_str());
            let until = req.get("until").and_then(|v| v.as_str());
            let kind = req.get("kind").and_then(|v| v.as_str());
            let git_sha = req.get("git_sha").and_then(|v| v.as_str());
            match st
                .store
                .corpus_ingest(&ns, source, text, principal_name.as_deref(), since, until)
                .await
            {
                Ok(v) => {
                    let cnt = v
                        .get("ids")
                        .and_then(|a| a.as_array())
                        .map(|a| a.len() as i32)
                        .unwrap_or(0);
                    let _ = Control::corpus_record(&st.pool, &ns, source, kind, git_sha, cnt).await;
                    Ok((StatusCode::OK, v))
                }
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/corpus/check" => {
            let snippet = req.get("snippet").and_then(|v| v.as_str()).unwrap_or("");
            let k = req.get("k").and_then(|v| v.as_u64()).unwrap_or(10) as usize;
            let ver = req.get("version").and_then(|v| v.as_str());
            let mut nss = vec![ns.clone()];
            if let Ok(anc) = Control::effective_ancestors(&st.pool, &ns).await {
                nss.extend(anc);
            }
            match st.store.corpus_check(&nss, snippet, k, ver).await {
                Ok(rows) => Ok((
                    StatusCode::OK,
                    json!({ "constraints": rows, "count": rows.len() }),
                )),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/corpus/check_diff" => {
            let diff = req.get("diff").and_then(|v| v.as_str()).unwrap_or("");
            if diff.trim().is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "diff required" }),
                );
            }
            let name = req.get("name").and_then(|v| v.as_str());
            let k = req.get("k").and_then(|v| v.as_u64()).unwrap_or(15) as usize;
            let ver = req.get("version").and_then(|v| v.as_str());
            let mut nss = vec![ns.clone()];
            if let Ok(anc) = Control::effective_ancestors(&st.pool, &ns).await {
                nss.extend(anc);
            }
            match st.store.corpus_check_diff(&nss, diff, name, k, ver).await {
                Ok(v) => Ok((StatusCode::OK, v)),
                Err(e) => Err(json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }))),
            }
        }
        "/corpus/deviation" => {
            let cid = req
                .get("constraint_id")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let rationale = req
                .get("rationale")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if cid <= 0 || rationale.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "constraint_id and rationale required" }),
                );
            }
            let until = req
                .get("until")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty());
            if let Some(u) = until {
                if let Err(e) = crate::store::parse_semver_key(u) {
                    return json_resp(StatusCode::BAD_REQUEST, json!({ "error": e }));
                }
            }
            match st.store.constraint_exists(&ns, cid).await {
                Ok(true) => {}
                Ok(false) => {
                    return json_resp(
                        StatusCode::NOT_FOUND,
                        json!({ "error": "constraint not found" }),
                    );
                }
                Err(e) => {
                    return json_resp(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "error": e }),
                    );
                }
            }
            let approved_by = req
                .get("approved_by")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .or(principal_name.as_deref())
                .unwrap_or("unknown");
            Control::corpus_deviation_record(
                &st.pool,
                &ns,
                cid,
                rationale,
                approved_by,
                until,
                Some(pid),
            )
            .await
            .map(|v| (StatusCode::OK, v))
            .map_err(ctrl_err_resp)
        }
        "/corpus/list" => Control::corpus_list(&st.pool, &ns)
            .await
            .map(|rows| (StatusCode::OK, json!({ "sources": rows })))
            .map_err(ctrl_err_resp),
        "/lease/acquire" => {
            let key = req.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
            let holder = req
                .get("holder_id")
                .or_else(|| req.get("holder"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if key.is_empty() || holder.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "key and holder_id required" }),
                );
            }
            let ttl = req
                .get("ttl_seconds")
                .and_then(|v| v.as_f64())
                .unwrap_or(1200.0);
            Control::lease_acquire(&st.pool, &ns, key, holder, Some(pid), ttl)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/lease/heartbeat" => {
            let key = req.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
            let holder = req
                .get("holder_id")
                .or_else(|| req.get("holder"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if key.is_empty() || holder.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "key and holder_id required" }),
                );
            }
            let ttl = req
                .get("ttl_seconds")
                .and_then(|v| v.as_f64())
                .unwrap_or(1200.0);
            Control::lease_heartbeat(&st.pool, &ns, key, holder, ttl)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/lease/release" => {
            let key = req.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
            let holder = req
                .get("holder_id")
                .or_else(|| req.get("holder"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if key.is_empty() || holder.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "key and holder_id required" }),
                );
            }
            Control::lease_release(&st.pool, &ns, key, holder)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/lease/who_holds" => {
            let key = req.get("key").and_then(|v| v.as_str()).unwrap_or("").trim();
            if key.is_empty() {
                return json_resp(StatusCode::BAD_REQUEST, json!({ "error": "key required" }));
            }
            Control::lease_who_holds(&st.pool, &ns, key)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/lease/list" => Control::lease_list(&st.pool, &ns)
            .await
            .map(|leases| (StatusCode::OK, json!({ "leases": leases })))
            .map_err(ctrl_err_resp),
        "/session/register" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let meta = req.get("metadata").cloned().unwrap_or_else(|| json!({}));
            let auto_named = req
                .get("auto_named")
                .and_then(|v| v.as_bool())
                .or_else(|| meta.get("auto_named").and_then(|v| v.as_bool()))
                .unwrap_or(false);
            let channels: Vec<String> = req
                .get("channels")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            let webhook = match req.get("webhook") {
                None | Some(Value::Null) => Webhook::Unset,
                Some(Value::String(s)) => Webhook::Set(s.clone()),
                _ => Webhook::Unset,
            };
            let opts = SessionOpts {
                harness: req
                    .get("harness")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                model: req
                    .get("model")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                role: req
                    .get("role")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                host: req
                    .get("host")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                cwd: req
                    .get("cwd")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                status: req
                    .get("status")
                    .and_then(|v| v.as_str())
                    .unwrap_or("active")
                    .to_string(),
                current_task: req
                    .get("current_task")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                metadata: meta,
                channels,
                channel_ads: req.get("channel_ads").cloned().unwrap_or_else(|| json!({})),
                ttl_seconds: req
                    .get("ttl_seconds")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(1800),
                webhook,
                machine_id: req
                    .get("machine_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                pid: req.get("pid").and_then(|v| v.as_i64()),
                tty: req
                    .get("tty")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                run_state: req
                    .get("run_state")
                    .and_then(|v| v.as_str())
                    .unwrap_or("idle")
                    .to_string(),
                auto_named,
            };
            Control::session_register(&st.pool, &ns, name, pid, &opts)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/session/quota" => {
            let sess = req
                .get("session_name")
                .or_else(|| req.get("name"))
                .or_else(|| req.get("session"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let paused = req.get("paused").and_then(|v| v.as_bool()).unwrap_or(true);
            let mins = req
                .get("cooldown_minutes")
                .and_then(|v| v.as_i64())
                .unwrap_or(60);
            let reason = req.get("reason").and_then(|v| v.as_str()).unwrap_or("");
            Control::workspace_set_session_quota(&st.pool, pid, &ns, sess, paused, mins, reason)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/session/rename" => {
            let old_s = req.get("old_name").and_then(|v| v.as_str()).unwrap_or("");
            let new_s = req.get("new_name").and_then(|v| v.as_str()).unwrap_or("");
            match Control::workspace_rename_session(&st.pool, pid, &ns, old_s, new_s).await {
                Ok(v) => {
                    sync_local_session_rename(&v);
                    Ok((StatusCode::OK, v))
                }
                Err(e) => Err(ctrl_err_resp(e)),
            }
        }
        "/session/heartbeat" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let opts = HeartbeatOpts {
                status: req
                    .get("status")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                current_task: req
                    .get("current_task")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                metadata: req.get("metadata").cloned(),
                ttl_seconds: req.get("ttl_seconds").and_then(|v| v.as_i64()),
                publish_status: req
                    .get("publish_status")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(true),
                machine_id: req
                    .get("machine_id")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                pid: req.get("pid").and_then(|v| v.as_i64()),
                tty: req
                    .get("tty")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                run_state: req
                    .get("run_state")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
            };
            match Control::session_heartbeat(&st.pool, &ns, name, pid, &opts).await {
                Ok(Some(v)) => Ok((StatusCode::OK, v)),
                Ok(None) => Ok((
                    StatusCode::NOT_FOUND,
                    json!({ "error": format!("session {:?} not registered", name) }),
                )),
                Err(e) => Err(ctrl_err_resp(e)),
            }
        }
        "/session/unregister" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
            Control::session_unregister(&st.pool, &ns, name, pid)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/session/list" => {
            let inc_exp = req
                .get("include_expired")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let role = req
                .get("role")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let harness = req
                .get("harness")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            Control::session_list(&st.pool, &ns, inc_exp, role, harness)
                .await
                .map(|sessions| {
                    let cnt = sessions.len();
                    (
                        StatusCode::OK,
                        json!({ "sessions": sessions, "count": cnt }),
                    )
                })
                .map_err(ctrl_err_resp)
        }
        "/channel/publish" => {
            let ch = req.get("channel").and_then(|v| v.as_str()).unwrap_or("");
            let sender = req
                .get("sender_session")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let content = req.get("content").and_then(|v| v.as_str()).unwrap_or("");
            let remember_flag = req
                .get("remember")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let opts = PublishOpts {
                intent: req
                    .get("intent")
                    .and_then(|v| v.as_str())
                    .unwrap_or("message")
                    .to_string(),
                metadata: req.get("metadata").cloned().unwrap_or_else(|| json!({})),
                description: req
                    .get("description")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                what_you_get: req
                    .get("what_you_get")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                claimable: req
                    .get("claimable")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                reply_to: req
                    .get("reply_to")
                    .and_then(|v| v.as_i64())
                    .filter(|&x| x > 0),
            };
            match Control::channel_publish(&st.pool, &ns, ch, sender, pid, content, &opts).await {
                Ok(mut v) => {
                    if remember_flag && !content.trim().is_empty() {
                        let mem_text = format!(
                            "[{} -> {} ({})] {}",
                            if sender.is_empty() { "session" } else { sender },
                            ch,
                            opts.intent,
                            content
                        );
                        let upper = content.trim_start().to_ascii_uppercase();
                        let inferred_type = if upper.starts_with("DECISION:") {
                            "decision"
                        } else if upper.starts_with("CONSTRAINT:") {
                            "constraint"
                        } else if upper.starts_with("INCIDENT:") {
                            "incident"
                        } else {
                            "handoff"
                        };
                        let _ = st
                            .store
                            .remember(
                                &ns,
                                &mem_text,
                                Some("user"),
                                Some(inferred_type),
                                None,
                                principal_name.as_deref(),
                                None,
                                None,
                            )
                            .await;
                        v["remembered"] = json!(true);
                    }
                    Ok((StatusCode::OK, v))
                }
                Err(e) => Err(ctrl_err_resp(e)),
            }
        }
        "/channel/registry" => {
            let topic = req.get("topic").and_then(|v| v.as_str()).unwrap_or("");
            let sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let task = req
                .get("task")
                .and_then(|v| v.as_str())
                .filter(|s| !s.trim().is_empty());
            let from_beg = req
                .get("from_beginning")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let mut base = SessionOpts::default();
            if let Some(d) = req.get("description").and_then(|v| v.as_str()) {
                base.metadata["description"] = json!(d);
            }
            Control::channel_registry(&st.pool, &ns, topic, sn, pid, task, from_beg, &base)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/ack" => {
            let sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let mid = req
                .get("message_id")
                .or_else(|| req.get("id"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let status = req
                .get("action")
                .or_else(|| req.get("status"))
                .and_then(|v| v.as_str())
                .unwrap_or("done");
            let note = req
                .get("resolution")
                .or_else(|| req.get("note"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            Control::channel_ack(&st.pool, &ns, sn, pid, mid, status, note)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/subscribe" => {
            let sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let channels: Vec<String> = req
                .get("channels")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            let from_beg = req
                .get("from_beginning")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let desc = req.get("description").and_then(|v| v.as_str()).unwrap_or("");
            let wyg = req
                .get("what_you_get")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            Control::channel_subscribe(&st.pool, &ns, sn, pid, &channels, from_beg, desc, wyg)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/unsubscribe" => {
            let sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let channels: Vec<String> = req
                .get("channels")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                })
                .unwrap_or_default();
            Control::channel_unsubscribe(&st.pool, &ns, sn, pid, &channels)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/advertise" => {
            let ch = req.get("channel").and_then(|v| v.as_str()).unwrap_or("");
            let owner = req
                .get("owner_session")
                .or_else(|| req.get("session_name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let desc = req.get("description").and_then(|v| v.as_str()).unwrap_or("");
            let wyg = req
                .get("what_you_get")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let intents: Option<Vec<String>> = req
                .get("intents")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str().map(|s| s.to_string()))
                        .collect()
                });
            let ad = Advert {
                owner_session: owner,
                description: desc,
                what_you_get: wyg,
                intents: intents.as_deref(),
                metadata: req.get("metadata"),
            };
            Control::channel_advertise(&st.pool, &ns, ch, pid, ad)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/poll" => {
            let mut sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            let host = req.get("host").and_then(|v| v.as_str()).unwrap_or("").trim();
            let cwd = req.get("cwd").and_then(|v| v.as_str()).unwrap_or("").trim();
            if sn.is_empty() && !host.is_empty() && !cwd.is_empty() {
                if let Ok(Some(resolved)) =
                    Control::resolve_session_for_cwd(&st.pool, &ns, host, cwd).await
                {
                    sn = resolved;
                }
            }
            if sn.is_empty() {
                if !host.is_empty() && !cwd.is_empty() {
                    Ok((
                        StatusCode::OK,
                        json!({ "session_name": "", "messages": [], "count": 0 }),
                    ))
                } else {
                    Err(json_resp(
                        StatusCode::BAD_REQUEST,
                        json!({ "error": "session_name required" }),
                    ))
                }
            } else {
                let ch = req
                    .get("channel")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty());
                let peek = req.get("peek").and_then(|v| v.as_bool()).unwrap_or(false);
                let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
                Control::channel_poll(&st.pool, &ns, &sn, pid, ch, peek, limit)
                    .await
                    .map(|v| (StatusCode::OK, v))
                    .map_err(ctrl_err_resp)
            }
        }
        "/channel/wait" => {
            let sn = req
                .get("session_name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim();
            if sn.is_empty() {
                return json_resp(
                    StatusCode::BAD_REQUEST,
                    json!({ "error": "session_name required" }),
                );
            }
            let ch = req
                .get("channel")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let timeout_secs = req
                .get("timeout_seconds")
                .or_else(|| req.get("timeout"))
                .and_then(|v| v.as_u64())
                .unwrap_or(60)
                .clamp(1, 600);
            let deadline = Instant::now() + Duration::from_secs(timeout_secs);

            loop {
                // Arm & register notification waiter BEFORE polling so a notification arriving mid-poll is never missed!
                let notified = st.channel_notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                match Control::channel_poll(&st.pool, &ns, sn, pid, ch, false, 50).await {
                    Ok(mut v) => {
                        let cnt = v.get("count").and_then(|x| x.as_u64()).unwrap_or(0);
                        let inb = v
                            .get("inbox_items")
                            .and_then(|x| x.as_array())
                            .map(|a| a.len())
                            .unwrap_or(0);
                        if cnt > 0 || inb > 0 {
                            v["timed_out"] = json!(false);
                            break Ok((StatusCode::OK, v));
                        }
                        if Instant::now() >= deadline {
                            v["timed_out"] = json!(true);
                            break Ok((StatusCode::OK, v));
                        }
                    }
                    Err(e) => break Err(ctrl_err_resp(e)),
                }
                let rem = deadline.saturating_duration_since(Instant::now());
                let wait_dur = rem.min(Duration::from_secs(5));
                let _ = tokio::time::timeout(wait_dur, notified).await;
            }
        }
        "/channel/cleanup" => {
            let idle = req
                .get("idle_days")
                .and_then(|v| v.as_i64())
                .unwrap_or(7);
            let ret = req
                .get("message_retention_days")
                .or_else(|| req.get("retention_days"))
                .and_then(|v| v.as_i64())
                .unwrap_or(30);
            let dry_run = req
                .get("dry_run")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            Control::channel_cleanup(&st.pool, &ns, idle, ret, dry_run)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/channel/list" => Control::discover(&st.pool, &ns)
            .await
            .map(|v| (StatusCode::OK, v))
            .map_err(ctrl_err_resp),
        "/inbox/send" => {
            let to_s = req
                .get("to_session")
                .or_else(|| req.get("to"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let from_s = req
                .get("from_session")
                .or_else(|| req.get("sender_session"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let content = req.get("content").and_then(|v| v.as_str()).unwrap_or("");
            let opts = SendOpts {
                subject: req
                    .get("subject")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                intent: req
                    .get("intent")
                    .and_then(|v| v.as_str())
                    .unwrap_or("task")
                    .to_string(),
                priority: req
                    .get("priority")
                    .and_then(|v| v.as_str())
                    .unwrap_or("normal")
                    .to_string(),
                thread_key: req
                    .get("thread_key")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                replaces_id: req
                    .get("replaces_id")
                    .and_then(|v| v.as_i64())
                    .filter(|&x| x > 0),
                replace_pending: req
                    .get("replace_pending")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                in_reply_to: req
                    .get("in_reply_to")
                    .and_then(|v| v.as_i64())
                    .filter(|&x| x > 0),
                metadata: req.get("metadata").cloned().unwrap_or_else(|| json!({})),
            };
            Control::inbox_send(&st.pool, &ns, to_s, from_s, pid, content, &opts)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/inbox/recall" | "/inbox/retract" => {
            let mid = req
                .get("message_id")
                .or_else(|| req.get("id"))
                .and_then(|v| v.as_i64())
                .filter(|&x| x > 0);
            let cmid = req
                .get("channel_message_id")
                .and_then(|v| v.as_i64())
                .filter(|&x| x > 0);
            let tkey = req
                .get("thread_key")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let from_s = req
                .get("from_session")
                .or_else(|| req.get("sender_session"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let reason = req.get("reason").and_then(|v| v.as_str()).unwrap_or("");
            Control::inbox_recall(&st.pool, &ns, pid, mid, cmid, tkey, from_s, reason)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        "/inbox/list" => {
            let mut to_s = req
                .get("to_session")
                .or_else(|| req.get("session_name"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if to_s.is_empty() {
                let host = req.get("host").and_then(|v| v.as_str()).unwrap_or("");
                let cwd = req.get("cwd").and_then(|v| v.as_str()).unwrap_or("");
                if !host.is_empty() && !cwd.is_empty() {
                    if let Ok(Some(resolved)) =
                        Control::resolve_session_for_cwd(&st.pool, &ns, host, cwd).await
                    {
                        to_s = resolved;
                    }
                }
            }
            let status = req
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("active");
            let prio = req
                .get("priority")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty());
            let inc_rec = req
                .get("include_recalled")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let mark_read = req
                .get("mark_read")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let limit = req.get("limit").and_then(|v| v.as_i64()).unwrap_or(50);
            Control::inbox_list(
                &st.pool, &ns, &to_s, pid, status, prio, inc_rec, mark_read, limit,
            )
            .await
            .map(|v| (StatusCode::OK, v))
            .map_err(ctrl_err_resp)
        }
        "/inbox/act" => {
            let to_s = req
                .get("to_session")
                .or_else(|| req.get("session_name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let mid = req
                .get("message_id")
                .or_else(|| req.get("id"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let act = req
                .get("action")
                .and_then(|v| v.as_str())
                .unwrap_or("acted");
            let res_txt = req
                .get("resolution")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let reply = req.get("reply").and_then(|v| v.as_str()).unwrap_or("");
            Control::inbox_act(&st.pool, &ns, to_s, pid, mid, act, res_txt, reply)
                .await
                .map(|v| (StatusCode::OK, v))
                .map_err(ctrl_err_resp)
        }
        _ => Err(json_resp(
            StatusCode::NOT_FOUND,
            json!({ "error": format!("unknown route {}", path) }),
        )),
    };

    match result {
        Ok((code, payload)) => {
            let elapsed_ms = t0.elapsed().as_millis() as i32;
            let actor = actor_hint
                .clone()
                .or_else(|| principal_name.clone())
                .unwrap_or_else(|| format!("p:{}", pid));
            if let Some(ref sess_name) = actor_hint {
                let clean_sn = sess_name.trim_start_matches('@');
                let model_up = actor_model_hint.as_deref().unwrap_or("");
                if !clean_sn.is_empty() && !ns.is_empty() {
                    if let Ok(c) = st.pool.get().await {
                        let _ = c
                            .execute(
                                "UPDATE memnos_control.agent_sessions
                                 SET last_heartbeat_at = now(),
                                     expires_at = GREATEST(expires_at, now() + interval '30 minutes'),
                                     status = CASE WHEN status = 'offline' THEN 'online' ELSE status END,
                                     model = CASE WHEN $3 <> '' THEN $3 ELSE model END
                                 WHERE namespace = $1 AND name = $2",
                                &[&ns, &clean_sn, &model_up],
                            )
                            .await;
                    }
                }
            }
            let mut final_detail = op_detail.clone();
            let mut is_empty_poll = false;
            if action == "channel/poll" {
                let mc = payload
                    .get("messages")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                let ic = payload
                    .get("inbox_items")
                    .and_then(|v| v.as_array())
                    .map(|a| a.len())
                    .unwrap_or(0);
                if mc + ic == 0 {
                    is_empty_poll = true;
                } else if final_detail.is_empty() {
                    final_detail = format!("{} unread delivered", mc + ic);
                }
            }
            let audit_detail = if action != "session/heartbeat" {
                Some(json!({ "actor": actor, "detail": final_detail }))
            } else {
                None
            };
            let _ = Control::audit(
                &st.pool,
                Some(pid),
                action,
                Some(&ns),
                code.is_success(),
                audit_detail,
                Some(elapsed_ms),
                None,
                Some(code.as_u16() as i32),
            )
            .await;
            if !matches!(action, "session/heartbeat" | "channel/list") && !is_empty_poll {
                let _ = st.event_tx.send(json!({
                    "type": "activity",
                    "op": action,
                    "namespace": if scope_wide { "*" } else { ns.as_str() },
                    "actor": actor,
                    "principal": principal_name.unwrap_or_default(),
                    "detail": final_detail,
                    "ok": code.is_success(),
                    "status": code.as_u16(),
                    "latency_ms": elapsed_ms,
                    "ts": chrono::Utc::now().to_rfc3339(),
                }));
            }
            if matches!(
                action,
                "channel/ack"
                    | "channel/subscribe"
                    | "channel/unsubscribe"
                    | "channel/advertise"
                    | "channel/cleanup"
                    | "inbox/send"
                    | "inbox/act"
                    | "inbox/retract"
                    | "inbox/recall"
                    | "session/register"
                    | "session/rename"
                    | "session/quota"
                    | "session/unregister"
            ) {
                st.channel_notify.notify_waiters();
            }
            json_resp(code, payload)
        }
        Err(resp) => resp,
    }
}

pub async fn run_server(port_override: Option<u16>) -> Result<(), String> {
    run_server_opts(port_override, false).await
}

pub async fn run_server_opts(
    port_override: Option<u16>,
    require_empty_db: bool,
) -> Result<(), String> {
    let cfg = load_config();
    let port = port_override.unwrap_or_else(|| server_port(&cfg));
    let pg_dsn = dsn(&cfg);
    let cfg_file = crate::config::config_path();
    let cfg_state = if cfg_file.is_file() {
        "present"
    } else {
        "not found"
    };
    let db_target = crate::config::redact_dsn_target(&pg_dsn);

    println!(
        "[memnos] config: {} ({})",
        cfg_file.display(),
        cfg_state
    );
    println!("[memnos] target db: {}", db_target);

    let pool_max = env::var("MEMNOS_POOL_MAX")
        .ok()
        .and_then(|s| s.trim().parse::<usize>().ok())
        .unwrap_or(10)
        .clamp(2, 64);
    let pool = build_pool(&pg_dsn, pool_max)?;
    let must_be_empty = require_empty_db
        || matches!(
            env::var("MEMNOS_REQUIRE_EMPTY_DB")
                .unwrap_or_default()
                .trim()
                .to_ascii_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on"
        );
    if must_be_empty {
        let probe_conn = pool
            .get()
            .await
            .map_err(|e| format!("database connection failed ({}): {}", db_target, e))?;
        let has_existing: bool = probe_conn
            .query_one(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname IN ('memnos_control', 'tenant_memnos'))",
                &[],
            )
            .await
            .map(|r| r.get(0))
            .map_err(|e| format!("database schema probe failed ({}): {}", db_target, e))?;
        if has_existing {
            return Err(format!(
                "refusing to start with --require-empty-db: target database '{}' already contains existing memnos schemas — point MEMNOS_DSN at a fresh database or omit --require-empty-db",
                db_target
            ));
        }
    }

    Control::init(&pool)
        .await
        .map_err(|e| format!("Control::init failed: {}", e))?;

    let raw_openai_key = env::var("OPENAI_API_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| cfg.openai.clone());
    let openai_key =
        crate::vault::Vault::resolve_provider_key_from_pool(&pool, raw_openai_key, true).await;

    let embedder = Arc::new(Embedder::from_env(openai_key.clone(), None));
    let extractor = Arc::new(Extractor::from_env(openai_key));
    let store = Arc::new(BrainStore::new(
        pool.clone(),
        None,
        embedder.clone(),
        extractor.clone(),
    )?);

    let explicit_choice = crate::config::explicit_embed_choice(&cfg);
    let existing_lineage = store.has_existing_embed_lineage().await?;
    let warm_proven = Arc::new(AtomicBool::new(false));
    let embed_configured = Arc::new(AtomicBool::new(false));
    let setup_reason = Arc::new(Mutex::new(String::new()));

    if embedder.is_openai() || explicit_choice.as_deref() == Some("local") || existing_lineage {
        store.init_schema().await?;
        let active_model = embedder.model_name();
        let (emb_spec, rerank_spec) = crate::rerank::specs_for_embed_model(&active_model);
        let local_ready = embedder.is_openai()
            || (crate::rerank::is_model_cached(emb_spec)
                && (!crate::rerank::rerank_enabled()
                    || crate::rerank::is_model_cached(rerank_spec)));
        if !embedder.is_openai() && !local_ready && crate::rerank::offline_models_only() {
            return Err(format!(
                "local neural models for '{}' are not cached locally and MEMNOS_OFFLINE_MODELS=1 — run `memnos models pull --yes` while online or configure OPENAI_API_KEY",
                active_model
            ));
        }
        if local_ready {
            embed_configured.store(true, Ordering::Release);
            let warm_store = store.clone();
            let warm_flag = warm_proven.clone();
            tokio::spawn(async move {
                let _ = warm_store.warm_indexes().await;
                warm_flag.store(true, Ordering::Relaxed);
            });
            if embedder.is_openai() {
                crate::rerank::spawn_background_prewarm_opts(&active_model, false);
            } else {
                crate::rerank::spawn_background_prewarm_opts(&active_model, true);
            }
        } else if let Ok(mut r) = setup_reason.lock() {
            *r = "memnos is not set up yet: local neural models (~930 MB) are not downloaded — run `memnos setup embeddings` or `memnos models pull --yes`".to_string();
        }
    } else if let Ok(mut r) = setup_reason.lock() {
        *r = "memnos is not set up yet, run `memnos setup embeddings`".to_string();
    }

    let channel_notify = Arc::new(Notify::new());
    spawn_channel_listener(pg_dsn.clone(), channel_notify.clone());
    let (event_tx, _) = tokio::sync::broadcast::channel::<Value>(512);
    {
        let cn = channel_notify.clone();
        let tx = event_tx.clone();
        tokio::spawn(async move {
            loop {
                cn.notified().await;
                let _ = tx.send(json!({
                    "type": "refresh",
                    "ts": chrono::Utc::now().to_rfc3339(),
                }));
            }
        });
    }

    // Background webhook pusher (every 5s)
    let wh_pool = pool.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let _ = Control::deliver_pending(&wh_pool, 50, 10).await;
            let _ = Control::deliver_pending_sessions(&wh_pool, 50, 10).await;
        }
    });

    // Optional parent gateway watcher (`MEMNOS_GATEWAY_PID`)
    let (gw_dead_tx, mut gw_dead_rx) = tokio::sync::mpsc::channel::<()>(1);
    if let Ok(gw_pid_str) = env::var("MEMNOS_GATEWAY_PID") {
        if let Ok(gw_pid) = gw_pid_str.parse::<u32>() {
            tokio::spawn(async move {
                loop {
                    tokio::time::sleep(Duration::from_secs(5)).await;
                    if !is_local_pid_alive(gw_pid) {
                        let _ = gw_dead_tx.send(()).await;
                        return;
                    }
                }
            });
        }
    }

    let state = AppState {
        port,
        pool,
        store,
        embedder,
        extractor,
        channel_notify,
        event_tx,
        warm_proven,
        embed_configured,
        setup_reason,
        ns_cache: Arc::new(Mutex::new(None)),
        mcp_sessions: Arc::new(Mutex::new(HashMap::new())),
        mcp_semaphore: Arc::new(Semaphore::new(24)),
        spawner: crate::spawner::HeadlessSpawner::new(),
        copilot: crate::copilot::CopilotEngine::new(),
        started_at: Instant::now(),
    };

    // Best-effort backfill of local config tokens into `token_enc` on boot
    for maybe_tok in [cfg.admin_token.clone(), Some(crate::config::session_token(&cfg))] {
        if let Some(t) = maybe_tok.map(|s| s.trim().to_string()).filter(|s| !s.is_empty()) {
            let _ = Control::authenticate(&state.pool, &t).await;
        }
    }

    let cors_routes = Router::new()
        .route("/", get(root_status_handler))
        .route("/healthz", get(healthz_handler))
        .route("/health", get(healthz_handler))
        .route("/readyz", get(readyz_handler))
        .route("/metrics", get(metrics_handler))
        .route("/metrics/prometheus", get(metrics_prometheus_handler))
        .route("/workspace/ws", get(workspace_ws_handler))
        .route("/workspace/events", get(workspace_events_handler))
        .route("/workspace/api/*sub", any(workspace_api_handler))
        .route("/admin/api/*sub", any(admin_api_handler))
        .route("/secret/resolve", post(secret_resolve_handler))
        .route("/bindings", get(user_routes_handler).post(user_routes_handler))
        .route("/bindings/recap", get(user_routes_handler))
        .route("/bindings/*rest", delete(user_routes_handler))
        .route("/hosts", get(user_routes_handler).post(user_routes_handler))
        .route("/nudges", get(user_routes_handler))
        .fallback(any(api_post_handler))
        .layer(CorsLayer::permissive());

    let app = Router::new()
        .route("/mcp", any(mcp_http_handler))
        .merge(cors_routes)
        .with_state(state);

    let bind_host = env::var("MEMNOS_BIND").unwrap_or_else(|_| "127.0.0.1".to_string());
    let listener = tokio::net::TcpListener::bind((bind_host.as_str(), port))
        .await
        .map_err(|e| format!("failed to bind {}:{}: {}", bind_host, port, e))?;

    println!(
        "[memnos] server v{} listening on http://{}:{} (db={}, config={})",
        VERSION,
        bind_host,
        port,
        db_target,
        cfg_file.display()
    );

    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = gw_dead_rx.recv() => {
                    println!("[memnos] parent gateway exited — shutting down internal backend.");
                }
            }
        })
        .await
        .map_err(|e| format!("server error: {}", e))
}
