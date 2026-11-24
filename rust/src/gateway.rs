//! `memnos gateway` — Zero-downtime blue-green upgrade front door (`gateway.rs`).
//!
//! Holds the public port (`:8900` by default) permanently, spawns internal `memnos serve`
//! backends on ephemeral loopback ports, gates swaps on `/readyz`, drains in-flight requests,
//! rolls back if the new backend dies mid-drain, and runs a self-healing watchdog.

use crate::config::config_dir as memnos_dir;
use axum::{
    body::{Body, Bytes},
    extract::{
        ws::{Message as AxumWsMsg, WebSocketUpgrade},
        State,
    },
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{any, get},
    Json, Router,
};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message as TungMsg;
use rand::RngCore;
use reqwest::Client;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::net::TcpListener as StdTcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const CONTROL_PREFIX: &str = "/__gateway__/";

fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn env_f64(name: &str, default: f64) -> f64 {
    env::var(name)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(default)
}

fn env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(default)
}

pub fn pick_free_port() -> Result<u16, String> {
    let listener = StdTcpListener::bind(("127.0.0.1", 0))
        .map_err(|e| format!("failed to pick ephemeral port: {}", e))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("local_addr failed: {}", e))?
        .port();
    drop(listener);
    Ok(port)
}

struct BackendHandle {
    id: u64,
    port: u16,
    pid: u32,
    child: Child,
}

impl BackendHandle {
    fn try_wait(&mut self) -> Option<i32> {
        match self.child.try_wait() {
            Ok(Some(status)) => Some(status.code().unwrap_or(-1)),
            Ok(None) => None,
            Err(_) => Some(-1),
        }
    }

    fn kill_graceful(&mut self, grace_secs: f64) {
        if self.try_wait().is_some() {
            return;
        }
        #[cfg(unix)]
        {
            let _ = Command::new("kill")
                .args(["-TERM", &self.pid.to_string()])
                .status();
        }
        #[cfg(windows)]
        {
            let _ = self.child.kill();
        }
        let deadline = Instant::now() + Duration::from_secs_f64(grace_secs.max(0.5));
        while Instant::now() < deadline {
            if self.try_wait().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct GatewayInner {
    next_id: u64,
    current: Option<BackendHandle>,
    inflight: HashMap<u16, usize>,
    upgrade_state: Value,
    heal_count: u64,
    consecutive_heal_failures: usize,
    last_heal_at: Option<f64>,
}

#[derive(Clone)]
struct GatewayState {
    public_port: u16,
    control_token: String,
    http: Client,
    inner: Arc<Mutex<GatewayInner>>,
}

fn spawn_backend(port: u16, id: u64) -> Result<BackendHandle, String> {
    let raw_exe = env::current_exe().map_err(|e| format!("current_exe failed: {}", e))?;
    let raw_str = raw_exe.to_string_lossy();
    let exe = if let Some(clean) = raw_str.strip_suffix(" (deleted)") {
        std::path::PathBuf::from(clean)
    } else {
        raw_exe
    };
    let mut cmd = Command::new(exe);
    cmd.args(["serve", "--port", &port.to_string()])
        .env("MEMNOS_PORT", port.to_string())
        .env("MEMNOS_GATEWAY_PID", std::process::id().to_string())
        .stdin(Stdio::null());
    let child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn backend on port {}: {}", port, e))?;
    let pid = child.id();
    Ok(BackendHandle {
        id,
        port,
        pid,
        child,
    })
}

async fn wait_ready(http: &Client, backend: &mut BackendHandle, timeout_secs: f64) -> bool {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs);
    let url = format!("http://127.0.0.1:{}/readyz", backend.port);
    while Instant::now() < deadline {
        if backend.try_wait().is_some() {
            return false;
        }
        if let Ok(resp) = http
            .get(&url)
            .timeout(Duration::from_secs(2))
            .send()
            .await
        {
            if resp.status().is_success() {
                if let Ok(v) = resp.json::<Value>().await {
                    if v.get("ready").and_then(|r| r.as_bool()) == Some(true) {
                        return true;
                    }
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    false
}

async fn backend_healthy(http: &Client, port: u16, timeout_secs: f64) -> bool {
    let url = format!("http://127.0.0.1:{}/healthz", port);
    match http
        .get(&url)
        .timeout(Duration::from_secs_f64(timeout_secs))
        .send()
        .await
    {
        Ok(r) if r.status().is_success() => r
            .json::<Value>()
            .await
            .ok()
            .and_then(|v| v.get("ok").and_then(|b| b.as_bool()))
            .unwrap_or(false),
        _ => false,
    }
}

fn write_gateway_state_file(port: u16, token: &str) {
    let dir = memnos_dir();
    let _ = fs::create_dir_all(&dir);
    let path = dir.join("gateway_state.json");
    let tmp = dir.join(format!("gateway_state.json.tmp{}", std::process::id()));
    let payload = json!({
        "port": port,
        "control_token": token,
        "gateway_pid": std::process::id(),
        "started_at": now_epoch(),
    });
    if let Ok(raw) = serde_json::to_string(&payload) {
        if fs::write(&tmp, raw).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
            }
            let _ = fs::rename(&tmp, &path);
        }
    }
}

fn clear_gateway_state_file() {
    let _ = fs::remove_file(memnos_dir().join("gateway_state.json"));
}

fn status_snapshot(st: &GatewayState) -> Value {
    let g = st.inner.lock().unwrap();
    let inflight_map: serde_json::Map<String, Value> = g
        .inflight
        .iter()
        .map(|(k, v)| (k.to_string(), json!(v)))
        .collect();
    json!({
        "public_port": st.public_port,
        "current_backend_port": g.current.as_ref().map(|b| b.port),
        "current_backend_pid": g.current.as_ref().map(|b| b.pid),
        "inflight": inflight_map,
        "upgrade": g.upgrade_state,
        "watchdog": {
            "heal_count": g.heal_count,
            "consecutive_heal_failures": g.consecutive_heal_failures,
            "last_heal_at": g.last_heal_at,
        },
        "gateway_pid": std::process::id(),
    })
}

fn constant_time_eq(a: &str, b: &str) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.bytes().zip(b.bytes()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn spawn_rolling_upgrade(st: GatewayState) -> bool {
    let target_id = {
        let mut g = st.inner.lock().unwrap();
        if g.upgrade_state.get("status").and_then(|v| v.as_str()) == Some("running") {
            return false;
        }
        g.upgrade_state = json!({
            "status": "running",
            "phase": "spawning",
            "started_at": now_epoch(),
        });
        g.current.as_ref().map(|b| b.id)
    };

    tokio::spawn(async move {
        let ready_timeout = env_f64("MEMNOS_GATEWAY_READY_TIMEOUT_S", 300.0);
        let drain_timeout = env_f64("MEMNOS_GATEWAY_DRAIN_TIMEOUT_S", 30.0);
        let kill_grace = env_f64("MEMNOS_GATEWAY_KILL_GRACE_S", 10.0);

        let new_port = match pick_free_port() {
            Ok(p) => p,
            Err(e) => {
                let mut g = st.inner.lock().unwrap();
                g.upgrade_state = json!({
                    "status": "failed",
                    "phase": "spawn_failed",
                    "error": e,
                    "finished_at": now_epoch(),
                });
                return;
            }
        };

        let next_id = {
            let mut g = st.inner.lock().unwrap();
            g.next_id += 1;
            g.next_id
        };

        let mut new_backend = match spawn_backend(new_port, next_id) {
            Ok(b) => b,
            Err(e) => {
                let mut g = st.inner.lock().unwrap();
                g.upgrade_state = json!({
                    "status": "failed",
                    "phase": "spawn_failed",
                    "error": e,
                    "finished_at": now_epoch(),
                });
                return;
            }
        };

        {
            let mut g = st.inner.lock().unwrap();
            g.upgrade_state = json!({
                "status": "running",
                "phase": "prewarm",
                "new_port": new_backend.port,
                "new_pid": new_backend.pid,
                "started_at": g.upgrade_state.get("started_at").cloned().unwrap_or(json!(now_epoch())),
            });
        }

        if !wait_ready(&st.http, &mut new_backend, ready_timeout).await {
            let dead_pid = new_backend.pid;
            tokio::task::spawn_blocking(move || new_backend.kill_graceful(kill_grace))
                .await
                .ok();
            let mut g = st.inner.lock().unwrap();
            g.upgrade_state = json!({
                "status": "failed",
                "phase": "prewarm_failed",
                "error": format!("new backend (pid {}) never became ready within {:.0}s", dead_pid, ready_timeout),
                "finished_at": now_epoch(),
            });
            return;
        }

        let swap_res = {
            let mut g = st.inner.lock().unwrap();
            if g.current.as_ref().map(|b| b.id) != target_id {
                Err(new_backend)
            } else {
                let old = g.current.replace(new_backend);
                g.upgrade_state = json!({
                    "status": "running",
                    "phase": "draining",
                    "old_port": old.as_ref().map(|b| b.port),
                    "old_pid": old.as_ref().map(|b| b.pid),
                    "swapped_at": now_epoch(),
                });
                Ok(old)
            }
        };
        let mut old_backend_opt = match swap_res {
            Ok(old) => old,
            Err(mut stale_new) => {
                tokio::task::spawn_blocking(move || stale_new.kill_graceful(kill_grace))
                    .await
                    .ok();
                let mut g2 = st.inner.lock().unwrap();
                g2.upgrade_state = json!({
                    "status": "failed",
                    "phase": "superseded",
                    "error": "backend was already replaced during prewarm",
                    "finished_at": now_epoch(),
                });
                return;
            }
        };

        let mut drained = true;
        let mut new_died = false;

        if let Some(ref old_b) = old_backend_opt {
            tokio::time::sleep(Duration::from_millis(250)).await;
            let deadline = Instant::now() + Duration::from_secs_f64(drain_timeout);
            let mut consecutive_zero = 0usize;
            drained = false;
            while Instant::now() < deadline {
                let (inflight_n, died) = {
                    let mut g = st.inner.lock().unwrap();
                    let died = g
                        .current
                        .as_mut()
                        .map(|b| b.try_wait().is_some())
                        .unwrap_or(true);
                    let n = g.inflight.get(&old_b.port).copied().unwrap_or(0);
                    (n, died)
                };
                if died {
                    new_died = true;
                    break;
                }
                if inflight_n == 0 {
                    consecutive_zero += 1;
                    if consecutive_zero >= 2 {
                        drained = true;
                        break;
                    }
                } else {
                    consecutive_zero = 0;
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }

        // Final authoritative post-flip liveness recheck before stopping old backend.
        {
            let mut g = st.inner.lock().unwrap();
            if g.current
                .as_mut()
                .map(|b| b.try_wait().is_some())
                .unwrap_or(true)
            {
                new_died = true;
            }
            if new_died {
                if let Some(mut old_b) = old_backend_opt.take() {
                    if old_b.try_wait().is_none() {
                        let old_port = old_b.port;
                        let old_pid = old_b.pid;
                        g.current = Some(old_b);
                        g.upgrade_state = json!({
                            "status": "failed",
                            "phase": "new_backend_died_post_flip",
                            "error": format!("new backend died during drain; rolled back to live old backend pid {} (port {})", old_pid, old_port),
                            "finished_at": now_epoch(),
                        });
                        return;
                    }
                }
                g.current = None;
                g.upgrade_state = json!({
                    "status": "failed",
                    "phase": "new_backend_died_post_flip",
                    "error": "new backend died post-flip and old backend is also dead",
                    "finished_at": now_epoch(),
                });
                return;
            }
        }

        if let Some(mut old_b) = old_backend_opt {
            tokio::task::spawn_blocking(move || {
                old_b.kill_graceful(kill_grace);
            })
            .await
            .ok();
        }

        let mut g = st.inner.lock().unwrap();
        g.upgrade_state = json!({
            "status": "done",
            "phase": "complete",
            "drained": drained,
            "finished_at": now_epoch(),
        });
    });

    true
}

async fn gateway_ws_handler(
    State(st): State<GatewayState>,
    uri: Uri,
    ws: WebSocketUpgrade,
) -> Response {
    let port_opt = {
        let g = st.inner.lock().unwrap();
        g.current.as_ref().map(|b| b.port)
    };
    let Some(port) = port_opt else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "memnos gateway: no backend ready yet"})),
        )
            .into_response();
    };
    let pq = uri
        .path_and_query()
        .map(|x| x.as_str())
        .unwrap_or("/workspace/ws")
        .to_string();
    let backend_ws_url = format!("ws://127.0.0.1:{}{}", port, pq);

    ws.on_upgrade(move |client_ws| async move {
        let Ok((backend_ws, _)) = tokio_tungstenite::connect_async(&backend_ws_url).await else {
            return;
        };
        let (mut c_tx, mut c_rx) = client_ws.split();
        let (mut b_tx, mut b_rx) = backend_ws.split();

        let client_to_backend = async {
            while let Some(Ok(msg)) = c_rx.next().await {
                let out = match msg {
                    AxumWsMsg::Text(t) => TungMsg::Text(t),
                    AxumWsMsg::Binary(b) => TungMsg::Binary(b),
                    AxumWsMsg::Ping(p) => TungMsg::Ping(p),
                    AxumWsMsg::Pong(p) => TungMsg::Pong(p),
                    AxumWsMsg::Close(_) => {
                        let _ = b_tx.close().await;
                        break;
                    }
                };
                if b_tx.send(out).await.is_err() {
                    break;
                }
            }
        };

        let backend_to_client = async {
            while let Some(Ok(msg)) = b_rx.next().await {
                let out = match msg {
                    TungMsg::Text(t) => AxumWsMsg::Text(t),
                    TungMsg::Binary(b) => AxumWsMsg::Binary(b),
                    TungMsg::Ping(p) => AxumWsMsg::Ping(p),
                    TungMsg::Pong(p) => AxumWsMsg::Pong(p),
                    TungMsg::Close(_) => {
                        let _ = c_tx.close().await;
                        break;
                    }
                    TungMsg::Frame(_) => continue,
                };
                if c_tx.send(out).await.is_err() {
                    break;
                }
            }
        };

        tokio::select! {
            _ = client_to_backend => {}
            _ = backend_to_client => {}
        }
    })
}

async fn gateway_handler(
    State(st): State<GatewayState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let path = uri.path();
    let path_and_q = uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or(path);

    if path.starts_with(CONTROL_PREFIX) {
        let auth = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        let tok = auth
            .strip_prefix("Bearer ")
            .or_else(|| auth.strip_prefix("bearer "))
            .map(|s| s.trim())
            .unwrap_or("");
        if tok.is_empty() || !constant_time_eq(tok, &st.control_token) {
            return (StatusCode::UNAUTHORIZED, Json(json!({"error": "unauthorized"})))
                .into_response();
        }
        if path == "/__gateway__/status" && method == Method::GET {
            return (StatusCode::OK, Json(status_snapshot(&st))).into_response();
        }
        if path == "/__gateway__/upgrade" && method == Method::POST {
            if spawn_rolling_upgrade(st.clone()) {
                return (StatusCode::ACCEPTED, Json(json!({"status": "started"}))).into_response();
            }
            let mut snap = status_snapshot(&st);
            if let Some(obj) = snap.as_object_mut() {
                obj.insert("status".to_string(), json!("already running"));
            }
            return (StatusCode::ACCEPTED, Json(snap)).into_response();
        }
        return (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "unknown gateway control route"})),
        )
            .into_response();
    }

    let port = {
        let mut g = st.inner.lock().unwrap();
        let Some(p) = g.current.as_ref().map(|b| b.port) else {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": "memnos gateway: no backend ready yet (starting up)"})),
            )
                .into_response();
        };
        *g.inflight.entry(p).or_insert(0) += 1;
        p
    };

    let target_url = format!("http://127.0.0.1:{}{}", port, path_and_q);
    let mut req_builder = st.http.request(method, &target_url).body(body.to_vec());
    if path == "/workspace/events" {
        req_builder = req_builder.timeout(Duration::from_secs(3600));
    }
    for (k, v) in &headers {
        let name = k.as_str().to_ascii_lowercase();
        if matches!(name.as_str(), "host" | "content-length" | "connection") {
            continue;
        }
        req_builder = req_builder.header(k, v);
    }

    let res = req_builder.send().await;
    {
        let mut g = st.inner.lock().unwrap();
        if let Some(cnt) = g.inflight.get_mut(&port) {
            *cnt = cnt.saturating_sub(1);
        }
    }

    match res {
        Ok(r) => {
            let status = r.status();
            let mut out_headers = HeaderMap::new();
            for (k, v) in r.headers() {
                let name = k.as_str().to_ascii_lowercase();
                if matches!(
                    name.as_str(),
                    "content-length" | "connection" | "transfer-encoding"
                ) {
                    continue;
                }
                out_headers.insert(k.clone(), v.clone());
            }
            let is_sse = out_headers
                .get(axum::http::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .map(|s| s.starts_with("text/event-stream"))
                .unwrap_or(false);
            let body_out = if is_sse {
                Body::from_stream(r.bytes_stream())
            } else {
                Body::from(r.bytes().await.unwrap_or_default())
            };
            let mut resp = Response::new(body_out);
            *resp.status_mut() = status;
            *resp.headers_mut() = out_headers;
            resp
        }
        Err(e) => {
            if path == "/healthz" {
                let heal = {
                    let g = st.inner.lock().unwrap();
                    json!({
                        "heal_count": g.heal_count,
                        "consecutive_heal_failures": g.consecutive_heal_failures,
                        "last_heal_at": g.last_heal_at,
                    })
                };
                return (
                    StatusCode::SERVICE_UNAVAILABLE,
                    Json(json!({
                        "ok": false,
                        "error": format!("memnos gateway: backend unreachable ({}) — watchdog is monitoring", e),
                        "watchdog": heal,
                    })),
                )
                    .into_response();
            }
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({"error": format!("memnos gateway: backend unreachable ({})", e)})),
            )
                .into_response()
        }
    }
}

pub async fn run_gateway(public_port: u16) -> Result<(), String> {
    let mut token_bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut token_bytes);
    let control_token = hex::encode(token_bytes);

    let http = Client::builder()
        .timeout(Duration::from_secs(120))
        .build()
        .map_err(|e| format!("HTTP client init failed: {}", e))?;

    let state = GatewayState {
        public_port,
        control_token: control_token.clone(),
        http: http.clone(),
        inner: Arc::new(Mutex::new(GatewayInner {
            next_id: 1,
            current: None,
            inflight: HashMap::new(),
            upgrade_state: json!({"status": "idle"}),
            heal_count: 0,
            consecutive_heal_failures: 0,
            last_heal_at: None,
        })),
    };

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", public_port))
        .await
        .map_err(|e| format!("failed to bind 127.0.0.1:{}: {}", public_port, e))?;

    println!(
        "[memnos-gateway] zero-downtime front door listening on http://127.0.0.1:{}",
        public_port
    );
    write_gateway_state_file(public_port, &control_token);

    let app = Router::new()
        .route("/workspace/ws", get(gateway_ws_handler))
        .fallback(any(gateway_handler))
        .with_state(state.clone());

    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    let serve_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = shutdown_rx.await;
            })
            .await;
    });

    // Boot initial backend
    let init_port = pick_free_port()?;
    let mut init_backend = spawn_backend(init_port, 1)?;
    let ready_timeout = env_f64("MEMNOS_GATEWAY_READY_TIMEOUT_S", 300.0);
    if !wait_ready(&http, &mut init_backend, ready_timeout).await {
        let kill_grace = env_f64("MEMNOS_GATEWAY_KILL_GRACE_S", 10.0);
        tokio::task::spawn_blocking(move || init_backend.kill_graceful(kill_grace))
            .await
            .ok();
        clear_gateway_state_file();
        let _ = shutdown_tx.send(());
        return Err(format!(
            "initial backend on internal port {} never became ready within {:.0}s",
            init_port, ready_timeout
        ));
    }
    println!(
        "[memnos-gateway] initial backend ready — pid {}, internal port {}. Serving.",
        init_backend.pid, init_backend.port
    );
    {
        let mut g = state.inner.lock().unwrap();
        g.current = Some(init_backend);
    }

    // Spawn watchdog
    let wd_state = state.clone();
    let (wd_giveup_tx, mut wd_giveup_rx) = tokio::sync::mpsc::channel::<()>(1);
    tokio::spawn(async move {
        let interval = env_f64("MEMNOS_GATEWAY_WATCHDOG_INTERVAL_S", 10.0);
        let hc_timeout = env_f64("MEMNOS_GATEWAY_WATCHDOG_HEALTHCHECK_TIMEOUT_S", 5.0);
        let miss_threshold = env_usize("MEMNOS_GATEWAY_WATCHDOG_MISS_THRESHOLD", 3);
        let max_heals = env_usize("MEMNOS_GATEWAY_WATCHDOG_MAX_HEAL_ATTEMPTS", 3);
        let backoff = env_f64("MEMNOS_GATEWAY_WATCHDOG_HEAL_BACKOFF_S", 15.0);
        let kill_grace = env_f64("MEMNOS_GATEWAY_KILL_GRACE_S", 10.0);

        let mut consecutive_misses = 0usize;
        loop {
            tokio::time::sleep(Duration::from_secs_f64(interval)).await;
            let (upgrade_running, target_opt) = {
                let mut g = wd_state.inner.lock().unwrap();
                let running =
                    g.upgrade_state.get("status").and_then(|v| v.as_str()) == Some("running");
                let target = g
                    .current
                    .as_mut()
                    .map(|b| (b.id, b.port, b.try_wait().is_some()));
                (running, target)
            };
            if upgrade_running {
                continue;
            }
            let Some((orig_id, port, exited)) = target_opt else {
                continue;
            };
            if exited {
                consecutive_misses = miss_threshold;
            } else if backend_healthy(&wd_state.http, port, hc_timeout).await {
                consecutive_misses = 0;
                continue;
            } else {
                consecutive_misses += 1;
            }

            if consecutive_misses < miss_threshold {
                continue;
            }
            consecutive_misses = 0;

            let Ok(new_port) = pick_free_port() else {
                continue;
            };
            let next_id = {
                let mut g = wd_state.inner.lock().unwrap();
                g.next_id += 1;
                g.next_id
            };
            let Ok(mut replacement) = spawn_backend(new_port, next_id) else {
                continue;
            };
            if wait_ready(&wd_state.http, &mut replacement, ready_timeout).await {
                let old_to_kill = {
                    let mut g = wd_state.inner.lock().unwrap();
                    let up_running =
                        g.upgrade_state.get("status").and_then(|v| v.as_str()) == Some("running");
                    if up_running || g.current.as_ref().map(|b| b.id) != Some(orig_id) {
                        None
                    } else {
                        g.heal_count += 1;
                        g.consecutive_heal_failures = 0;
                        g.last_heal_at = Some(now_epoch());
                        g.current.replace(replacement)
                    }
                };
                if let Some(mut old_b) = old_to_kill {
                    tokio::task::spawn_blocking(move || old_b.kill_graceful(kill_grace))
                        .await
                        .ok();
                    continue;
                }
            } else {
                tokio::task::spawn_blocking(move || replacement.kill_graceful(kill_grace))
                    .await
                    .ok();
                let failures = {
                    let mut g = wd_state.inner.lock().unwrap();
                    g.consecutive_heal_failures += 1;
                    g.consecutive_heal_failures
                };
                if failures >= max_heals {
                    let _ = wd_giveup_tx.send(()).await;
                    return;
                }
                tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
            }
        }
    });

    let mut gave_up = false;
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {
            println!("[memnos-gateway] received shutdown signal ...");
        }
        _ = wd_giveup_rx.recv() => {
            gave_up = true;
            println!("[memnos-gateway] watchdog gave up after repeated heal failures; freeing port.");
        }
    }

    let _ = shutdown_tx.send(());
    let _ = serve_handle.await;

    let old_opt = state.inner.lock().unwrap().current.take();
    if let Some(mut b) = old_opt {
        let kill_grace = env_f64("MEMNOS_GATEWAY_KILL_GRACE_S", 10.0);
        tokio::task::spawn_blocking(move || b.kill_graceful(kill_grace))
            .await
            .ok();
    }
    clear_gateway_state_file();
    if gave_up {
        Err("watchdog gave up after repeated backend heal failures".to_string())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pick_free_port_and_constant_time_eq() {
        let p = pick_free_port().expect("ephemeral port");
        assert!(p > 0);
        assert!(constant_time_eq("abc123", "abc123"));
        assert!(!constant_time_eq("abc123", "abc124"));
        assert!(!constant_time_eq("abc", "abcd"));
    }
}
