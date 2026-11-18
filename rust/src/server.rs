//! `memnos serve` — Unified Native Rust HTTP Server (`server.rs`).
//!
//! Serves 100% of the MEMNOS REST API, the zero-build `/admin` web console (embedded directly
//! inside the single binary), the Streamable-HTTP `/mcp` JSON-RPC 2.0 mount, and the
//! `LISTEN memnos_channel` `/channel/wait` long-polling bus.

use crate::config::{dsn, load_config, server_port, VERSION};
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
    extract::{Path as AxumPath, Query, State},
    http::{header, HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{any, delete, get, post},
    Json, Router,
};
use deadpool_postgres::Pool;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::env;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::{Notify, Semaphore};

const UI_INDEX_HTML: &[u8] = include_bytes!("../../ui/index.html");
const UI_APP_CSS: &[u8] = include_bytes!("../../ui/app.css");
const UI_APP_JS: &[u8] = include_bytes!("../../ui/app.js");
const UI_CLI_REF_JSON: &[u8] = include_bytes!("../../ui/cli-reference.json");

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
    pub warm_proven: Arc<AtomicBool>,
    pub ns_cache: Arc<Mutex<Option<(Instant, Vec<Value>)>>>,
    pub mcp_sessions: Arc<Mutex<HashMap<String, Arc<Mutex<String>>>>>,
    pub mcp_semaphore: Arc<Semaphore>,
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

async fn healthz_handler() -> Response {
    json_resp(
        StatusCode::OK,
        json!({
            "ok": true,
            "version": VERSION,
            "engine": "rust"
        }),
    )
}

async fn readyz_handler(State(st): State<AppState>) -> Response {
    let db_ok = match st.pool.get().await {
        Ok(c) => c.query_one("SELECT 1", &[]).await.is_ok(),
        Err(_) => false,
    };
    let warm = st.warm_proven.load(Ordering::Relaxed);
    let ready = db_ok && warm;
    let code = if ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    json_resp(
        code,
        json!({
            "ready": ready,
            "db": db_ok,
            "warm": warm,
            "version": VERSION,
            "engine": "rust"
        }),
    )
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

fn serve_ui_asset(file_name: &str) -> Response {
    let safe = std::path::Path::new(file_name)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    let (canonical, ctype, embedded): (&str, &str, &[u8]) = match safe {
        "" | "index.html" => ("index.html", "text/html; charset=utf-8", UI_INDEX_HTML),
        "app.css" => ("app.css", "text/css; charset=utf-8", UI_APP_CSS),
        "app.js" => ("app.js", "application/javascript; charset=utf-8", UI_APP_JS),
        "cli-reference.json" => (
            "cli-reference.json",
            "application/json; charset=utf-8",
            UI_CLI_REF_JSON,
        ),
        _ => return json_resp(StatusCode::NOT_FOUND, json!({ "error": "not found" })),
    };
    if let Ok(ui_dir) = env::var("MEMNOS_UI_DIR") {
        let disk_path = PathBuf::from(ui_dir).join(canonical);
        if disk_path.is_file() {
            if let Ok(bytes) = std::fs::read(&disk_path) {
                return (StatusCode::OK, [(header::CONTENT_TYPE, ctype)], bytes).into_response();
            }
        }
    }
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, ctype)],
        embedded.to_vec(),
    )
        .into_response()
}

async fn admin_index_handler() -> Response {
    serve_ui_asset("index.html")
}

async fn admin_static_handler(AxumPath(file): AxumPath<String>) -> Response {
    serve_ui_asset(&file)
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
            json!({ "ok": true, "transport": "streamable-http", "engine": "rust" }),
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
            Ok(p) => json_resp(StatusCode::OK, json!({ "principals": p })),
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
                "vault_unlocked": Vault::available(),
                "engine": "rust"
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

    let principal_name = Control::principal_info(&st.pool, pid)
        .await
        .ok()
        .flatten()
        .and_then(|v| v.get("name").and_then(|s| s.as_str()).map(|s| s.to_string()));

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

    let scope_wide = matches!(
        req.get("scope").and_then(|v| v.as_str()),
        Some("all" | "wide")
    );
    let ns_optional = (scope_wide
        && matches!(
            path,
            "/recall" | "/recall_v2" | "/memory/search" | "/memory/context"
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

    if !(scope_wide && matches!(path, "/recall" | "/recall_v2" | "/memory/search" | "/memory/context"))
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
        "/recall" | "/recall_v2" | "/memory/search" | "/memory/context" => {
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
            let _ = Control::audit(
                &st.pool,
                Some(pid),
                action,
                Some(&ns),
                code.is_success(),
                None,
                Some(t0.elapsed().as_millis() as i32),
                None,
                Some(code.as_u16() as i32),
            )
            .await;
            json_resp(code, payload)
        }
        Err(resp) => resp,
    }
}

pub async fn run_server(port_override: Option<u16>) -> Result<(), String> {
    let cfg = load_config();
    let port = port_override.unwrap_or_else(|| server_port(&cfg));
    let pg_dsn = dsn(&cfg);

    let pool = build_pool(&pg_dsn, 24)?;
    Control::init(&pool)
        .await
        .map_err(|e| format!("Control::init failed: {}", e))?;

    let openai_key = env::var("OPENAI_API_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| cfg.openai.clone());

    let embedder = Arc::new(Embedder::from_env(openai_key.clone(), None));
    let extractor = Arc::new(Extractor::from_env(openai_key));
    let store = Arc::new(BrainStore::new(
        pool.clone(),
        None,
        embedder.clone(),
        extractor.clone(),
    )?);

    store.init_schema().await?;

    let warm_proven = Arc::new(AtomicBool::new(false));
    let warm_store = store.clone();
    let warm_flag = warm_proven.clone();
    tokio::spawn(async move {
        let _ = warm_store.warm_indexes().await;
        warm_flag.store(true, Ordering::Relaxed);
    });

    let channel_notify = Arc::new(Notify::new());
    spawn_channel_listener(pg_dsn.clone(), channel_notify.clone());

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
        warm_proven,
        ns_cache: Arc::new(Mutex::new(None)),
        mcp_sessions: Arc::new(Mutex::new(HashMap::new())),
        mcp_semaphore: Arc::new(Semaphore::new(24)),
    };

    let app = Router::new()
        .route("/healthz", get(healthz_handler))
        .route("/readyz", get(readyz_handler))
        .route("/metrics", get(metrics_handler))
        .route("/metrics/prometheus", get(metrics_prometheus_handler))
        .route("/admin", get(admin_index_handler))
        .route("/admin/", get(admin_index_handler))
        .route("/admin/api/*sub", any(admin_api_handler))
        .route("/admin/*file", get(admin_static_handler))
        .route("/mcp", any(mcp_http_handler))
        .route("/secret/resolve", post(secret_resolve_handler))
        .route("/bindings", get(user_routes_handler).post(user_routes_handler))
        .route("/bindings/recap", get(user_routes_handler))
        .route("/bindings/*rest", delete(user_routes_handler))
        .route("/hosts", get(user_routes_handler).post(user_routes_handler))
        .route("/nudges", get(user_routes_handler))
        .fallback(any(api_post_handler))
        .with_state(state);

    let bind_host = env::var("MEMNOS_BIND").unwrap_or_else(|_| "127.0.0.1".to_string());
    let listener = tokio::net::TcpListener::bind((bind_host.as_str(), port))
        .await
        .map_err(|e| format!("failed to bind {}:{}: {}", bind_host, port, e))?;

    println!(
        "[memnos] Rust engine v{} listening on http://{}:{}",
        VERSION, bind_host, port
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
