//! `memnos proxy` — Transparent OpenAI & Anthropic LLM-API capture tee (`proxy.rs`).
//!
//! Relays requests verbatim (including streaming SSE chunks with zero buffering latency)
//! and captures terminal human user + assistant exchanges asynchronously via `/remember`.

use crate::config::{config_path, load_config, server_url, session_token};
use axum::{
    body::{Body, Bytes},
    extract::State,
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{any, get},
    Json, Router,
};
use futures_util::StreamExt;
use reqwest::Client;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::env;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::mpsc;

const MAX_BODY: usize = 20 * 1024 * 1024;

#[derive(Clone)]
pub struct ProxyConfig {
    pub port: u16,
    pub server_url: String,
    pub token: String,
    pub namespace: String,
    pub openai_upstream: String,
    pub anthropic_upstream: String,
    pub capture: bool,
    pub model_denylist: Vec<String>,
    pub min_max_tokens: i64,
}

impl ProxyConfig {
    pub fn load(port_override: Option<u16>) -> Self {
        let cfg = load_config();
        let raw_cfg: Value = std::fs::read_to_string(config_path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_else(|| json!({}));
        let default_user = env::var("USER")
            .or_else(|_| env::var("USERNAME"))
            .unwrap_or_else(|_| "me".to_string());
        let default_ns = format!(
            "user:{}",
            default_user.split_whitespace().next().unwrap_or("me")
        );

        let mut port: u16 = 8910;
        let mut openai_upstream = "https://api.openai.com".to_string();
        let mut anthropic_upstream = "https://api.anthropic.com".to_string();
        let mut capture = true;
        let mut model_denylist = vec!["*haiku*".to_string()];
        let mut min_max_tokens: i64 = 512;

        if let Some(p) = raw_cfg.get("proxy").and_then(|v| v.as_object()) {
            if let Some(v) = p.get("port").and_then(|x| x.as_u64()) {
                port = v as u16;
            }
            if let Some(v) = p.get("capture").and_then(|x| x.as_bool()) {
                capture = v;
            }
            if let Some(v) = p.get("capture_min_max_tokens").and_then(|x| x.as_i64()) {
                min_max_tokens = v;
            }
            if let Some(arr) = p.get("capture_model_denylist").and_then(|x| x.as_array()) {
                model_denylist = arr
                    .iter()
                    .filter_map(|x| x.as_str().map(|s| s.to_string()))
                    .collect();
            }
            if let Some(up) = p.get("upstreams").and_then(|x| x.as_object()) {
                if let Some(o) = up.get("openai").and_then(|x| x.as_str()) {
                    openai_upstream = o.to_string();
                }
                if let Some(a) = up.get("anthropic").and_then(|x| x.as_str()) {
                    anthropic_upstream = a.to_string();
                }
            }
        }

        if let Ok(ep) = env::var("MEMNOS_PROXY_PORT") {
            if let Ok(v) = ep.parse::<u16>() {
                port = v;
            }
        }
        if let Some(po) = port_override {
            port = po;
        }

        let srv_url = server_url(&cfg);
        let token = env::var("MEMNOS_TOKEN").unwrap_or_else(|_| {
            cfg.proxy_token
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| session_token(&cfg))
        });
        let namespace = env::var("MEMNOS_NS").unwrap_or_else(|_| {
            raw_cfg
                .get("proxy_namespace")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .filter(|s| !s.is_empty())
                .unwrap_or(default_ns)
        });

        Self {
            port,
            server_url: srv_url,
            token,
            namespace,
            openai_upstream,
            anthropic_upstream,
            capture,
            model_denylist,
            min_max_tokens,
        }
    }
}

#[derive(Debug, Clone)]
pub struct RespSummary {
    pub text: String,
    pub terminal: bool,
    pub has_tools: bool,
}

struct CaptureJob {
    req: Value,
    summary: RespSummary,
    fmt: String,
    namespace: String,
}

struct ProxyStats {
    captured: AtomicU64,
    skipped: AtomicU64,
    errors: AtomicU64,
    relay_errors: AtomicU64,
    last_error: Mutex<Option<String>>,
}

impl Default for ProxyStats {
    fn default() -> Self {
        Self {
            captured: AtomicU64::new(0),
            skipped: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            relay_errors: AtomicU64::new(0),
            last_error: Mutex::new(None),
        }
    }
}

#[derive(Clone)]
struct ProxyState {
    cfg: ProxyConfig,
    http: Client,
    tx: mpsc::Sender<CaptureJob>,
    stats: Arc<ProxyStats>,
}

fn wildcard_match(pat: &str, text: &str) -> bool {
    let p = pat.to_lowercase();
    let t = text.to_lowercase();
    if p == "*" {
        return true;
    }
    let parts: Vec<&str> = p.split('*').collect();
    if parts.len() == 1 {
        return p == t;
    }
    let mut pos = 0usize;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        if i == 0 {
            if !t.starts_with(part) {
                return false;
            }
            pos = part.len();
        } else if i == parts.len() - 1 {
            if !t[pos..].ends_with(part) {
                return false;
            }
            pos = t.len();
        } else if let Some(idx) = t[pos..].find(part) {
            pos += idx + part.len();
        } else {
            return false;
        }
    }
    true
}

pub fn block_text(content: &Value) -> Option<String> {
    if let Some(s) = content.as_str() {
        return Some(s.to_string());
    }
    if let Some(arr) = content.as_array() {
        let mut out = Vec::with_capacity(arr.len());
        for b in arr {
            let obj = b.as_object()?;
            if obj.get("type").and_then(|v| v.as_str()) != Some("text") {
                return None;
            }
            out.push(
                obj.get("text")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            );
        }
        return Some(out.join("\n"));
    }
    None
}

pub fn trailing_user_text(req: &Value) -> Option<String> {
    let msgs = req.get("messages")?.as_array()?;
    let last = msgs.last()?;
    if last.get("role").and_then(|v| v.as_str()) != Some("user") {
        return None;
    }
    block_text(last.get("content")?)
}

fn conv_key(req: &Value, fmt: &str) -> String {
    let mut sysp = String::new();
    if fmt == "anthropic" {
        if let Some(s) = req.get("system") {
            sysp = s
                .as_str()
                .map(|x| x.to_string())
                .unwrap_or_else(|| s.to_string().chars().take(2000).collect());
        }
    } else if let Some(msgs) = req.get("messages").and_then(|v| v.as_array()) {
        for m in msgs {
            if matches!(
                m.get("role").and_then(|v| v.as_str()),
                Some("system") | Some("developer")
            ) {
                sysp = m
                    .get("content")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
                    .unwrap_or_default();
                break;
            }
        }
    }
    let mut first = String::new();
    if let Some(msgs) = req.get("messages").and_then(|v| v.as_array()) {
        for m in msgs {
            if m.get("role").and_then(|v| v.as_str()) == Some("user") {
                if let Some(c) = m.get("content") {
                    first = block_text(c)
                        .unwrap_or_else(|| c.to_string().chars().take(500).collect());
                }
                break;
            }
        }
    }
    let mut hasher = Sha256::new();
    hasher.update(sysp.as_bytes());
    hasher.update(b"\x00");
    hasher.update(first.as_bytes());
    let hex_digest = hex::encode(hasher.finalize());
    hex_digest[..16].to_string()
}

pub fn parse_response(body: &[u8], fmt: &str, streamed: bool) -> RespSummary {
    if streamed {
        let raw_str = String::from_utf8_lossy(body);
        let mut text = String::new();
        let mut finish: Option<String> = None;
        let mut has_tools = false;
        for line in raw_str.lines() {
            let trimmed = line.trim();
            if let Some(rest) = trimmed.strip_prefix("data:") {
                let data = rest.trim();
                if data.is_empty() || data == "[DONE]" {
                    continue;
                }
                let Ok(ev) = serde_json::from_str::<Value>(data) else {
                    continue;
                };
                if fmt == "openai" {
                    if let Some(choices) = ev.get("choices").and_then(|v| v.as_array()) {
                        for ch in choices {
                            if let Some(delta) = ch.get("delta") {
                                if let Some(c) = delta.get("content").and_then(|v| v.as_str()) {
                                    text.push_str(c);
                                }
                                if delta.get("tool_calls").is_some() {
                                    has_tools = true;
                                }
                            }
                            if let Some(fr) = ch.get("finish_reason").and_then(|v| v.as_str()) {
                                finish = Some(fr.to_string());
                            }
                        }
                    }
                } else {
                    match ev.get("type").and_then(|v| v.as_str()) {
                        Some("content_block_delta") => {
                            if ev
                                .get("delta")
                                .and_then(|d| d.get("type"))
                                .and_then(|v| v.as_str())
                                == Some("text_delta")
                            {
                                if let Some(t) = ev
                                    .get("delta")
                                    .and_then(|d| d.get("text"))
                                    .and_then(|v| v.as_str())
                                {
                                    text.push_str(t);
                                }
                            }
                        }
                        Some("content_block_start") => {
                            if ev
                                .get("content_block")
                                .and_then(|b| b.get("type"))
                                .and_then(|v| v.as_str())
                                == Some("tool_use")
                            {
                                has_tools = true;
                            }
                        }
                        Some("message_delta") => {
                            if let Some(sr) = ev
                                .get("delta")
                                .and_then(|d| d.get("stop_reason"))
                                .and_then(|v| v.as_str())
                            {
                                finish = Some(sr.to_string());
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        let terminal = if fmt == "openai" {
            finish.as_deref() == Some("stop")
        } else {
            finish.as_deref() == Some("end_turn")
        };
        return RespSummary {
            text,
            terminal,
            has_tools,
        };
    }

    let Ok(d) = serde_json::from_slice::<Value>(body) else {
        return RespSummary {
            text: String::new(),
            terminal: false,
            has_tools: false,
        };
    };
    if fmt == "openai" {
        let ch = d
            .get("choices")
            .and_then(|v| v.as_array())
            .and_then(|a| a.first())
            .cloned()
            .unwrap_or(Value::Null);
        let msg = ch.get("message").cloned().unwrap_or(Value::Null);
        let text = msg
            .get("content")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let terminal = ch.get("finish_reason").and_then(|v| v.as_str()) == Some("stop");
        let has_tools = msg
            .get("tool_calls")
            .and_then(|v| v.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false);
        RespSummary {
            text,
            terminal,
            has_tools,
        }
    } else {
        let mut text = Vec::new();
        let mut has_tools = false;
        if let Some(arr) = d.get("content").and_then(|v| v.as_array()) {
            for b in arr {
                match b.get("type").and_then(|v| v.as_str()) {
                    Some("text") => {
                        if let Some(t) = b.get("text").and_then(|v| v.as_str()) {
                            text.push(t.to_string());
                        }
                    }
                    Some("tool_use") => has_tools = true,
                    _ => {}
                }
            }
        }
        RespSummary {
            text: text.join("\n"),
            terminal: d.get("stop_reason").and_then(|v| v.as_str()) == Some("end_turn"),
            has_tools,
        }
    }
}

pub fn should_capture(cfg: &ProxyConfig, req: &Value, summary: &RespSummary) -> (bool, &'static str) {
    if !cfg.capture {
        return (false, "capture disabled");
    }
    let model = req.get("model").and_then(|v| v.as_str()).unwrap_or("");
    if cfg.model_denylist.iter().any(|p| wildcard_match(p, model)) {
        return (false, "model denylisted");
    }
    let mt = req
        .get("max_tokens")
        .or_else(|| req.get("max_completion_tokens"))
        .and_then(|v| v.as_i64());
    if let Some(m) = mt {
        if m < cfg.min_max_tokens {
            return (false, "max_tokens below threshold");
        }
    }
    if !summary.terminal || summary.has_tools {
        return (false, "non-terminal response");
    }
    let Some(user_text) = trailing_user_text(req) else {
        return (false, "last message not human text");
    };
    let ut = user_text.trim();
    if ut.len() < 15 || ut.split_whitespace().count() < 3 {
        return (false, "user text too short");
    }
    if summary.text.trim().len() < 30 {
        return (false, "assistant text too short");
    }
    (true, "ok")
}

async fn healthz_handler(State(st): State<ProxyState>) -> impl IntoResponse {
    let last_err = st.stats.last_error.lock().ok().and_then(|g| g.clone());
    Json(json!({
        "ok": true,
        "capture": st.cfg.capture,
        "stats": {
            "captured": st.stats.captured.load(Ordering::Relaxed),
            "skipped": st.stats.skipped.load(Ordering::Relaxed),
            "errors": st.stats.errors.load(Ordering::Relaxed),
            "relay_errors": st.stats.relay_errors.load(Ordering::Relaxed),
            "last_error": last_err,
        }
    }))
}

async fn proxy_handler(
    State(st): State<ProxyState>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if method != Method::POST {
        return (StatusCode::NOT_FOUND, "").into_response();
    }
    if body.len() > MAX_BODY {
        return (StatusCode::PAYLOAD_TOO_LARGE, "").into_response();
    }
    let path_and_q = uri
        .path_and_query()
        .map(|pq| pq.as_str())
        .unwrap_or_else(|| uri.path());
    let path = uri.path();

    let fmt = if path.starts_with("/v1/messages") {
        "anthropic"
    } else if path.starts_with("/v1/chat/completions")
        || path.contains("/chat/")
        || path.starts_with("/v1/")
    {
        "openai"
    } else {
        return (StatusCode::NOT_FOUND, "").into_response();
    };

    let ns = headers
        .get("x-memnos-namespace")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .unwrap_or_else(|| st.cfg.namespace.clone());

    let upstream = if fmt == "anthropic" {
        st.cfg.anthropic_upstream.trim_end_matches('/')
    } else {
        st.cfg.openai_upstream.trim_end_matches('/')
    };
    let target_url = format!("{}{}", upstream, path_and_q);

    let req_json: Value = serde_json::from_slice(&body).unwrap_or_else(|_| json!({}));
    let streamed = req_json
        .get("stream")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let capture_eligible =
        path.starts_with("/v1/messages") || path.starts_with("/v1/chat/completions");

    let mut req_builder = st.http.post(&target_url).body(body.to_vec());
    for (k, v) in &headers {
        let name = k.as_str().to_ascii_lowercase();
        if matches!(
            name.as_str(),
            "host" | "content-length" | "connection" | "accept-encoding" | "x-memnos-namespace"
        ) {
            continue;
        }
        req_builder = req_builder.header(k, v);
    }

    let upstream_resp = match req_builder.send().await {
        Ok(r) => r,
        Err(e) => {
            let (etype, code) = if e.is_timeout() {
                ("upstream_timeout", StatusCode::BAD_GATEWAY)
            } else if e.is_connect() {
                ("upstream_unreachable", StatusCode::BAD_GATEWAY)
            } else {
                ("proxy_error", StatusCode::INTERNAL_SERVER_ERROR)
            };
            let msg = format!("network error contacting {}: {}", upstream, e);
            st.stats.relay_errors.fetch_add(1, Ordering::Relaxed);
            if let Ok(mut g) = st.stats.last_error.lock() {
                *g = Some(format!("{}: {}", etype, msg));
            }
            return (
                code,
                Json(json!({
                    "error": {
                        "type": etype,
                        "source": "memnos-proxy",
                        "message": msg
                    }
                })),
            )
                .into_response();
        }
    };

    let status = upstream_resp.status();
    let mut resp_headers = HeaderMap::new();
    for (k, v) in upstream_resp.headers() {
        let name = k.as_str().to_ascii_lowercase();
        if matches!(
            name.as_str(),
            "content-length" | "connection" | "transfer-encoding"
        ) {
            continue;
        }
        resp_headers.insert(k.clone(), v.clone());
    }

    let status_ok = status.is_success();
    let fmt_owned = fmt.to_string();

    if streamed {
        let byte_stream = upstream_resp.bytes_stream();
        let tx = st.tx.clone();
        let acc = Arc::new(Mutex::new(Vec::<u8>::new()));
        let acc_clone = acc.clone();

        let mapped = byte_stream.map(move |chunk_res| match chunk_res {
            Ok(bytes) => {
                if capture_eligible && status_ok {
                    if let Ok(mut b) = acc_clone.lock() {
                        if b.len() < MAX_BODY {
                            b.extend_from_slice(&bytes);
                        }
                    }
                }
                Ok::<Bytes, std::io::Error>(bytes)
            }
            Err(e) => Err(std::io::Error::new(std::io::ErrorKind::Other, e.to_string())),
        });

        let final_stream = futures_util::stream::unfold(
            (Box::pin(mapped), Some((tx, req_json, fmt_owned, ns, acc))),
            move |(mut inner, mut finish_ctx)| async move {
                match inner.next().await {
                    Some(item) => Some((item, (inner, finish_ctx))),
                    None => {
                        if capture_eligible && status_ok {
                            if let Some((tx, req_j, fmt_s, ns_s, buf_arc)) = finish_ctx.take() {
                                if let Ok(buf) = buf_arc.lock() {
                                    let summary = parse_response(&buf, &fmt_s, true);
                                    let _ = tx.try_send(CaptureJob {
                                        req: req_j,
                                        summary,
                                        fmt: fmt_s,
                                        namespace: ns_s,
                                    });
                                }
                            }
                        }
                        None
                    }
                }
            },
        );

        let mut resp = Response::new(Body::from_stream(final_stream));
        *resp.status_mut() = status;
        *resp.headers_mut() = resp_headers;
        resp
    } else {
        let resp_bytes = upstream_resp.bytes().await.unwrap_or_default();
        if capture_eligible && status_ok {
            let summary = parse_response(&resp_bytes, &fmt_owned, false);
            let _ = st.tx.try_send(CaptureJob {
                req: req_json,
                summary,
                fmt: fmt_owned,
                namespace: ns,
            });
        }
        let mut resp = Response::new(Body::from(resp_bytes));
        *resp.status_mut() = status;
        *resp.headers_mut() = resp_headers;
        resp
    }
}

pub async fn run_proxy(port_override: Option<u16>) -> Result<(), String> {
    let cfg = ProxyConfig::load(port_override);
    let port = cfg.port;
    let http = Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| format!("failed to build HTTP client: {}", e))?;

    let (tx, mut rx) = mpsc::channel::<CaptureJob>(256);
    let stats = Arc::new(ProxyStats::default());

    let worker_cfg = cfg.clone();
    let worker_stats = stats.clone();
    let worker_http = Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())?;

    tokio::spawn(async move {
        let mut lru = VecDeque::<String>::with_capacity(4096);
        while let Some(job) = rx.recv().await {
            let (ok, _) = should_capture(&worker_cfg, &job.req, &job.summary);
            if !ok {
                worker_stats.skipped.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            let sid = conv_key(&job.req, &job.fmt);
            let user_turn = trailing_user_text(&job.req).unwrap_or_default();
            let turns = [
                (user_turn.trim().to_string(), "user"),
                (job.summary.text.trim().to_string(), "assistant"),
            ];
            for (text, speaker) in turns {
                if text.is_empty() {
                    continue;
                }
                let norm = text.split_whitespace().collect::<Vec<_>>().join(" ");
                let mut h = Sha256::new();
                h.update(format!("{}|{}|{}", job.namespace, speaker, norm).as_bytes());
                let key = hex::encode(h.finalize());
                if lru.contains(&key) {
                    continue;
                }
                lru.push_back(key);
                if lru.len() > 4096 {
                    lru.pop_front();
                }
                let clipped: String = text.chars().take(8000).collect();
                let mut req = worker_http
                    .post(format!(
                        "{}/remember",
                        worker_cfg.server_url.trim_end_matches('/')
                    ))
                    .json(&json!({
                        "namespace": job.namespace,
                        "text": clipped,
                        "speaker": speaker,
                        "session_id": sid,
                        "async": true
                    }));
                if !worker_cfg.token.is_empty() {
                    req = req.header("Authorization", format!("Bearer {}", worker_cfg.token));
                }
                match req.send().await {
                    Ok(r) if r.status().is_success() => {
                        worker_stats.captured.fetch_add(1, Ordering::Relaxed);
                    }
                    Ok(r) => {
                        worker_stats.errors.fetch_add(1, Ordering::Relaxed);
                        if let Ok(mut g) = worker_stats.last_error.lock() {
                            *g = Some(format!("capture[{}]: HTTP {}", speaker, r.status()));
                        }
                    }
                    Err(e) => {
                        worker_stats.errors.fetch_add(1, Ordering::Relaxed);
                        if let Ok(mut g) = worker_stats.last_error.lock() {
                            *g = Some(format!("capture[{}]: {}", speaker, e));
                        }
                    }
                }
            }
        }
    });

    println!("[memnos-proxy] capture proxy on http://127.0.0.1:{}", port);
    println!(
        "  upstreams: openai -> {}   anthropic -> {}",
        cfg.openai_upstream, cfg.anthropic_upstream
    );
    println!(
        "  capture:   {}",
        if cfg.capture {
            format!("ON -> namespace {}", cfg.namespace)
        } else {
            "OFF (relay only)".to_string()
        }
    );

    let state = ProxyState {
        cfg,
        http,
        tx,
        stats,
    };
    let app = Router::new()
        .route("/healthz", get(healthz_handler))
        .fallback(any(proxy_handler))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .map_err(|e| format!("failed to bind 127.0.0.1:{}: {}", port, e))?;
    axum::serve(listener, app)
        .await
        .map_err(|e| format!("proxy server error: {}", e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_capture_policy_and_sse_parsing() {
        let cfg = ProxyConfig {
            port: 8910,
            server_url: "http://127.0.0.1:8900".to_string(),
            token: String::new(),
            namespace: "proj:memnos".to_string(),
            openai_upstream: "https://api.openai.com".to_string(),
            anthropic_upstream: "https://api.anthropic.com".to_string(),
            capture: true,
            model_denylist: vec!["*haiku*".to_string()],
            min_max_tokens: 512,
        };
        let sse = b"data: {\"choices\":[{\"delta\":{\"content\":\"We migrated payments-sync \"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"to Postgres with zero downtime.\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n";
        let sum = parse_response(sse, "openai", true);
        assert!(sum.terminal);
        assert!(!sum.has_tools);
        assert_eq!(
            sum.text,
            "We migrated payments-sync to Postgres with zero downtime."
        );

        let req = json!({
            "model": "gpt-4o",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "How did we migrate payments-sync?"}]
        });
        assert_eq!(should_capture(&cfg, &req, &sum), (true, "ok"));

        let haiku_req = json!({
            "model": "claude-3-5-haiku-latest",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "How did we migrate payments-sync?"}]
        });
        assert!(!should_capture(&cfg, &haiku_req, &sum).0);
    }
}
