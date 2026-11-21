use crate::config::config_dir;
use crate::control::{slug, Control, PublishOpts, SendOpts, SessionOpts, Webhook};
use deadpool_postgres::Pool;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::{broadcast, oneshot, Notify};

#[derive(Clone, Debug, Serialize)]
pub struct HarnessBinary {
    pub key: String,
    pub label: String,
    pub binary_name: String,
    pub installed: bool,
    #[serde(skip_serializing)]
    pub resolved_path: Option<PathBuf>,
}

const HARNESS_SPECS: &[(&str, &str, &str)] = &[
    ("claude-code", "Claude Code", "claude"),
    ("antigravity", "Antigravity", "agy"),
    ("codex", "Codex", "codex"),
    ("gemini", "Gemini", "gemini"),
    ("cursor", "Cursor", "cursor-agent"),
];

const SAFE_OS_ENV_ALLOWLIST: &[&str] = &[
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "USER",
    "USERNAME",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
];

const API_KEY_ENV_VARS: &[&str] = &[
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CURSOR_API_KEY",
];

fn search_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Some(home) = dirs_home() {
        dirs.push(home.join(".local").join("bin"));
        dirs.push(home.join(".cargo").join("bin"));
        dirs.push(home.join(".npm-global").join("bin"));
    }
    #[cfg(unix)]
    {
        dirs.push(PathBuf::from("/opt/homebrew/bin"));
        dirs.push(PathBuf::from("/usr/local/bin"));
        dirs.push(PathBuf::from("/usr/bin"));
    }
    if let Some(path_os) = env::var_os("PATH") {
        for p in env::split_paths(&path_os) {
            if !dirs.contains(&p) {
                dirs.push(p);
            }
        }
    }
    dirs
}

fn dirs_home() -> Option<PathBuf> {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

fn is_executable_file(p: &Path) -> bool {
    let Ok(meta) = std::fs::metadata(p) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        (meta.permissions().mode() & 0o111) != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn find_executable(bin_name: &str) -> Option<PathBuf> {
    #[cfg(windows)]
    let candidates = [
        format!("{}.exe", bin_name),
        format!("{}.cmd", bin_name),
        bin_name.to_string(),
    ];
    #[cfg(not(windows))]
    let candidates = [bin_name.to_string()];

    for dir in search_dirs() {
        for c in &candidates {
            let full = dir.join(c);
            if is_executable_file(&full) {
                return Some(full);
            }
        }
    }
    None
}

pub fn discover_harnesses() -> Vec<HarnessBinary> {
    HARNESS_SPECS
        .iter()
        .map(|(key, label, bin)| {
            let found = find_executable(bin);
            HarnessBinary {
                key: (*key).to_string(),
                label: (*label).to_string(),
                binary_name: (*bin).to_string(),
                installed: found.is_some(),
                resolved_path: found,
            }
        })
        .collect()
}

pub fn normalize_harness_key(raw: &str) -> &'static str {
    let l = raw.trim().to_ascii_lowercase();
    if l.contains("claude") {
        "claude-code"
    } else if l.contains("antigravity") || l == "agy" {
        "antigravity"
    } else if l.contains("codex") {
        "codex"
    } else if l.contains("gemini") {
        "gemini"
    } else if l.contains("cursor") {
        "cursor"
    } else {
        ""
    }
}

fn sanitize_session_id(raw: &str) -> Option<String> {
    let t = raw.trim();
    if t.is_empty() || t.len() > 128 || t.starts_with('-') {
        return None;
    }
    if t.chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        Some(t.to_string())
    } else {
        None
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct LaunchRequest {
    pub namespace: String,
    pub harness: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub handle: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub channel: String,
    #[serde(default)]
    pub cwd: String,
    #[serde(default)]
    pub prompt: String,
    #[serde(default = "default_true")]
    pub subscription_only: bool,
    #[serde(default)]
    pub skip_permissions: bool,
    #[serde(default)]
    pub isolate_worktree: bool,
    #[serde(default = "default_true")]
    pub auto_failover: bool,
    #[serde(default)]
    pub failover_depth: u8,
    #[serde(default)]
    pub resume_session_id: String,
    #[serde(default)]
    pub reply_to_session: String,
    #[serde(default)]
    pub is_dm_turn: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Clone, Debug, Serialize)]
pub struct WorkerRecord {
    pub run_id: u64,
    pub namespace: String,
    pub handle: String,
    pub harness: String,
    pub model: String,
    pub role: String,
    pub channel: String,
    pub cwd: String,
    pub worktree_path: Option<String>,
    pub worktree_branch: Option<String>,
    pub cli_session_id: Option<String>,
    pub subscription_only: bool,
    pub skip_permissions: bool,
    pub auto_failover: bool,
    pub status: String, // starting | running | completed | failed | quota_paused | stopped | timed_out
    pub pid: Option<u32>,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub last_line: String,
    pub log_lines: Vec<String>,
    pub exit_code: Option<i32>,
    pub failover_to: Option<String>,
}

#[derive(Clone, Debug)]
struct QueuedDm {
    from_session: String,
    content: String,
}

pub struct HeadlessSpawner {
    next_id: AtomicU64,
    max_concurrent: usize,
    timeout_secs: u64,
    reaped_stale_db: AtomicBool,
    runs: Mutex<Vec<WorkerRecord>>,
    kill_senders: Mutex<HashMap<u64, oneshot::Sender<()>>>,
    queued_dms: Mutex<HashMap<String, Vec<QueuedDm>>>,
}

impl HeadlessSpawner {
    pub fn new() -> Arc<Self> {
        let cap = env::var("MEMNOS_MAX_HEADLESS")
            .ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(4)
            .clamp(1, 16);
        let timeout_secs = env::var("MEMNOS_HEADLESS_TIMEOUT_SECS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(1800)
            .clamp(30, 14_400);
        Arc::new(Self {
            next_id: AtomicU64::new(1),
            max_concurrent: cap,
            timeout_secs,
            reaped_stale_db: AtomicBool::new(false),
            runs: Mutex::new(Vec::new()),
            kill_senders: Mutex::new(HashMap::new()),
            queued_dms: Mutex::new(HashMap::new()),
        })
    }

    pub fn status_snapshot(&self, allowed_ns: Option<&[String]>, is_admin: bool) -> Value {
        let harnesses = discover_harnesses();
        let runs = self.runs.lock().unwrap();
        let filtered: Vec<WorkerRecord> = runs
            .iter()
            .filter(|r| {
                if is_admin {
                    true
                } else if let Some(list) = allowed_ns {
                    list.iter().any(|n| n == &r.namespace)
                } else {
                    false
                }
            })
            .cloned()
            .collect();
        let running_count = filtered
            .iter()
            .filter(|r| r.status == "running" || r.status == "starting")
            .count();
        json!({
            "harnesses": harnesses,
            "max_concurrent": self.max_concurrent,
            "running_count": running_count,
            "runs": filtered,
        })
    }

    pub fn stop_worker(
        &self,
        run_id: Option<u64>,
        handle: Option<&str>,
        namespace: Option<&str>,
    ) -> Result<Value, String> {
        let ns_filter = namespace.map(str::trim).filter(|s| !s.is_empty() && *s != "*");
        let (target_id, dm_key) = {
            let runs = self.runs.lock().unwrap();
            let found = if let Some(id) = run_id {
                runs.iter().find(|r| {
                    r.run_id == id
                        && (r.status == "running" || r.status == "starting")
                        && ns_filter.map(|n| r.namespace == n).unwrap_or(true)
                })
            } else if let Some(h) = handle.map(|s| s.trim().trim_start_matches('@')) {
                runs.iter().find(|r| {
                    (r.status == "running" || r.status == "starting")
                        && r.handle == h
                        && ns_filter.map(|n| r.namespace == n).unwrap_or(true)
                })
            } else {
                None
            };
            match found {
                Some(r) => (Some(r.run_id), Some(format!("{}::{}", r.namespace, r.handle))),
                None => (None, None),
            }
        };
        if let Some(k) = dm_key {
            self.queued_dms.lock().unwrap().remove(&k);
        }
        let Some(id) = target_id else {
            return Err("no matching running headless worker found".to_string());
        };
        let tx_opt = self.kill_senders.lock().unwrap().remove(&id);
        if let Some(tx) = tx_opt {
            let _ = tx.send(());
            Ok(json!({ "ok": true, "run_id": id, "stopped": true }))
        } else {
            Err(format!("worker #{} is no longer running", id))
        }
    }

    /// Route a direct `@inbox` message sent from the UI to a headless worker:
    /// - If the worker is currently running, queue the DM so it answers right as its active step completes.
    /// - If the worker has already auto-closed (`offline`/`completed`), wake/resume it headlessly (`--resume <cli_session_id>`)!
    pub async fn deliver_dm(
        self: &Arc<Self>,
        pool: &Pool,
        port: u16,
        caller_principal_id: i64,
        is_admin: bool,
        event_tx: broadcast::Sender<Value>,
        channel_notify: Arc<Notify>,
        namespace: &str,
        to_session: &str,
        from_session: &str,
        content: &str,
    ) -> Option<&'static str> {
        if !is_admin {
            return None;
        }
        let clean_to = to_session.trim().trim_start_matches('@');
        let clean_from = from_session.trim().trim_start_matches('@');
        if clean_to.is_empty() || content.trim().is_empty() {
            return None;
        }

        // 1. Check if this handle is currently running in the spawner
        let is_running_now = {
            let mut runs = self.runs.lock().unwrap();
            if let Some(r) = runs.iter_mut().find(|r| {
                r.namespace == namespace
                    && r.handle == clean_to
                    && (r.status == "running" || r.status == "starting")
            }) {
                let ts_hms = chrono::Utc::now().format("%H:%M:%S").to_string();
                push_log_line(
                    &mut r.log_lines,
                    format!("[{}] 📩 DM from @{}: {}", ts_hms, clean_from, content.chars().take(90).collect::<String>()),
                );
                true
            } else {
                false
            }
        };

        if is_running_now {
            let key = format!("{}::{}", namespace, clean_to);
            self.queued_dms
                .lock()
                .unwrap()
                .entry(key)
                .or_default()
                .push(QueuedDm {
                    from_session: clean_from.to_string(),
                    content: content.trim().to_string(),
                });
            return Some("queued_for_running_worker");
        }

        // 2. Otherwise inspect `agent_sessions` to see if `to_session` is a headless handle we can wake/resume!
        let Ok(c) = pool.get().await else {
            return None;
        };
        let Ok(Some(row)) = c
            .query_opt(
                "SELECT COALESCE(harness, '') AS harness, COALESCE(model, '') AS model,
                        COALESCE(role, '') AS role, COALESCE(cwd, '') AS cwd, metadata
                 FROM memnos_control.agent_sessions
                 WHERE namespace = $1 AND name = $2",
                &[&namespace, &clean_to],
            )
            .await
        else {
            return None;
        };
        let meta: Value = row.get("metadata");
        if meta.get("headless").and_then(|v| v.as_bool()) != Some(true) {
            return None;
        }
        let harness: String = row.get("harness");
        if harness.is_empty() {
            return None;
        }
        let model: String = row.get("model");
        let role: String = row.get("role");
        let cwd: String = row.get("cwd");
        let sub_only = meta
            .get("subscription_only")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        let skip_perms = meta
            .get("skip_permissions")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let cli_sid = meta
            .get("cli_session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let dm_prompt = format!(
            "Direct message from @{clean_from} in MEMNOS Workspace:\n\n\"{}\"\n\nRespond directly, clearly, and concisely to @{clean_from}. (Your reply will automatically be delivered to @{clean_from} in your @{clean_to} chat thread.)",
            content.trim()
        );
        let wake_req = LaunchRequest {
            namespace: namespace.to_string(),
            harness,
            model,
            handle: clean_to.to_string(),
            role,
            channel: String::new(),
            cwd,
            prompt: dm_prompt,
            subscription_only: sub_only,
            skip_permissions: skip_perms,
            isolate_worktree: false,
            auto_failover: true,
            failover_depth: 0,
            resume_session_id: cli_sid,
            reply_to_session: clean_from.to_string(),
            is_dm_turn: true,
        };
        if self
            .launch(
                pool.clone(),
                port,
                caller_principal_id,
                event_tx,
                channel_notify,
                wake_req,
            )
            .await
            .is_ok()
        {
            Some("woke_headless_worker")
        } else {
            None
        }
    }

    fn take_queued_dms(&self, ns: &str, handle: &str) -> Vec<QueuedDm> {
        let key = format!("{}::{}", ns, handle);
        self.queued_dms.lock().unwrap().remove(&key).unwrap_or_default()
    }

    fn rollback_slot(&self, run_id: u64) {
        let mut runs = self.runs.lock().unwrap();
        runs.retain(|r| r.run_id != run_id);
    }

    pub fn launch<'a>(
        self: &'a Arc<Self>,
        pool: Pool,
        port: u16,
        caller_principal_id: i64,
        event_tx: broadcast::Sender<Value>,
        channel_notify: Arc<Notify>,
        req: LaunchRequest,
    ) -> Pin<Box<dyn Future<Output = Result<WorkerRecord, String>> + Send + 'a>> {
        Box::pin(async move {
            if !self.reaped_stale_db.swap(true, Ordering::SeqCst) {
                if let Ok(c) = pool.get().await {
                    let _ = c
                        .execute(
                            "UPDATE memnos_control.agent_sessions
                             SET status = 'offline', run_state = 'stopped', expires_at = now()
                             WHERE tty = 'headless' AND status <> 'offline'",
                            &[],
                        )
                        .await;
                }
            }

            let ns = req.namespace.trim().to_string();
            if ns.is_empty() || ns == "*" {
                return Err(
                    "choose a concrete target namespace (not '*') to launch a worker".to_string(),
                );
            }

            let h_key = normalize_harness_key(&req.harness);
            if h_key.is_empty() {
                return Err(format!("unsupported harness '{}'", req.harness));
            }
            let discovered = discover_harnesses();
            let h_bin = discovered
                .iter()
                .find(|h| h.key == h_key)
                .cloned()
                .ok_or_else(|| format!("unknown harness '{}'", h_key))?;
            let exe_path = h_bin.resolved_path.ok_or_else(|| {
                format!(
                    "harness '{}' (`{}`) is not installed in PATH on this machine",
                    h_bin.label, h_bin.binary_name
                )
            })?;

            let run_id = self.next_id.fetch_add(1, Ordering::SeqCst);
            let prefix = match h_key {
                "claude-code" => "claude",
                "antigravity" => "agy",
                "codex" => "codex",
                "gemini" => "gemini",
                "cursor" => "cursor",
                _ => "worker",
            };
            let handle = if req.handle.trim().trim_start_matches('@').is_empty() {
                let role_part = if req.role.trim().is_empty() {
                    "worker".to_string()
                } else {
                    slug(req.role.trim(), "role").unwrap_or_else(|_| "worker".to_string())
                };
                format!("{}-{}-{}", prefix, role_part, run_id)
            } else {
                slug(req.handle.trim().trim_start_matches('@'), "handle")
                    .map_err(|e| format!("{:?}", e))?
            };

            let ch_clean = if req.channel.trim().is_empty() {
                String::new()
            } else if req.channel.trim().starts_with('#') {
                req.channel.trim().to_string()
            } else {
                format!("#{}", req.channel.trim())
            };

            let started_iso = chrono::Utc::now().to_rfc3339();
            let model_trim = req.model.trim().to_string();
            let clean_resume_id = sanitize_session_id(&req.resume_session_id);

            // Atomically check concurrency cap AND reserve the slot BEFORE any `.await`
            {
                let mut runs = self.runs.lock().unwrap();
                let active = runs
                    .iter()
                    .filter(|r| r.status == "running" || r.status == "starting")
                    .count();
                if active >= self.max_concurrent {
                    return Err(format!(
                        "headless concurrency limit reached ({}/{} running) — wait for a worker to finish or stop one first",
                        active, self.max_concurrent
                    ));
                }
                runs.insert(
                    0,
                    WorkerRecord {
                        run_id,
                        namespace: ns.clone(),
                        handle: handle.clone(),
                        harness: h_key.to_string(),
                        model: model_trim.clone(),
                        role: req.role.trim().to_string(),
                        channel: ch_clean.clone(),
                        cwd: String::new(),
                        worktree_path: None,
                        worktree_branch: None,
                        cli_session_id: clean_resume_id.clone(),
                        subscription_only: req.subscription_only,
                        skip_permissions: req.skip_permissions,
                        auto_failover: req.auto_failover,
                        status: "starting".to_string(),
                        pid: None,
                        started_at: started_iso.clone(),
                        finished_at: None,
                        last_line: "Preparing isolated environment…".to_string(),
                        log_lines: vec![format!(
                            "[{}] ⚡ Launching @{} ({})",
                            chrono::Utc::now().format("%H:%M:%S"),
                            handle,
                            h_bin.label
                        )],
                        exit_code: None,
                        failover_to: None,
                    },
                );
                if runs.len() > 50 {
                    runs.truncate(50);
                }
            }

            // Guard against overwriting an already-live non-headless or running session in the same namespace
            if !req.is_dm_turn {
                if let Ok(c) = pool.get().await {
                    if let Ok(Some(_)) = c
                        .query_opt(
                            "SELECT 1 FROM memnos_control.agent_sessions
                             WHERE namespace = $1 AND name = $2 AND status <> 'offline' AND expires_at > now()",
                            &[&ns, &handle],
                        )
                        .await
                    {
                        self.rollback_slot(run_id);
                        return Err(format!(
                            "handle '@{}' is already online in namespace '{}' — pick a different handle or leave blank",
                            handle, ns
                        ));
                    }
                }
            }

            let resolved_cwd = match resolve_worker_cwd(&pool, &ns, &req.cwd).await {
                Ok(p) => p,
                Err(e) => {
                    self.rollback_slot(run_id);
                    return Err(e);
                }
            };

            let mut worktree_dir: Option<PathBuf> = None;
            let mut worktree_branch: Option<String> = None;
            let mut worktree_base_sha = String::new();

            let effective_cwd = if req.isolate_worktree {
                let base_out = Command::new("git")
                    .args(["-C", &resolved_cwd.to_string_lossy(), "rev-parse", "HEAD"])
                    .output()
                    .await
                    .map_err(|e| format!("failed to run git rev-parse: {}", e));
                let base_sha = match base_out {
                    Ok(o) if o.status.success() => {
                        String::from_utf8_lossy(&o.stdout).trim().to_string()
                    }
                    _ => {
                        self.rollback_slot(run_id);
                        return Err(format!(
                            "isolate_worktree requested, but '{}' is not a valid git repository",
                            resolved_cwd.display()
                        ));
                    }
                };
                let wt_base = config_dir().join("worktrees");
                let _ = std::fs::create_dir_all(&wt_base);
                let wt_path = wt_base.join(format!("{}-{}", handle, run_id));
                let branch_name = format!("memnos/{}-{}", handle, run_id);
                let out = Command::new("git")
                    .args([
                        "-C",
                        &resolved_cwd.to_string_lossy(),
                        "worktree",
                        "add",
                        "-b",
                        &branch_name,
                        &wt_path.to_string_lossy(),
                        "HEAD",
                    ])
                    .output()
                    .await;
                match out {
                    Ok(o) if o.status.success() => {
                        worktree_dir = Some(wt_path.clone());
                        worktree_branch = Some(branch_name);
                        worktree_base_sha = base_sha;
                        wt_path
                    }
                    Ok(o) => {
                        self.rollback_slot(run_id);
                        let stderr_s = String::from_utf8_lossy(&o.stderr).trim().to_string();
                        return Err(format!(
                            "git worktree add failed (refusing to fall back to live checkout): {}",
                            stderr_s
                        ));
                    }
                    Err(e) => {
                        self.rollback_slot(run_id);
                        return Err(format!("git worktree add error: {}", e));
                    }
                }
            } else {
                resolved_cwd.clone()
            };

            // Preserve existing channel subscriptions when waking an existing headless handle via DM
            let mut init_channels = vec!["#general".to_string()];
            if !ch_clean.is_empty() && ch_clean != "#general" {
                init_channels.push(ch_clean.clone());
            }
            if req.is_dm_turn {
                if let Ok(c) = pool.get().await {
                    if let Ok(rows) = c
                        .query(
                            "SELECT channel FROM memnos_control.channel_subscriptions WHERE namespace = $1 AND session_name = $2 AND channel LIKE '#%'",
                            &[&ns, &handle],
                        )
                        .await
                    {
                        for r in rows {
                            let ch_s: String = r.get(0);
                            if !init_channels.contains(&ch_s) {
                                init_channels.push(ch_s);
                            }
                        }
                    }
                }
            }

            // Mint ephemeral namespace-scoped worker principal & token
            let worker_principal_name = format!("headless-{}-{}", handle, run_id);
            let worker_pid = match Control::create_principal(&pool, &worker_principal_name, "agent").await {
                Ok(id) => id,
                Err(e) => {
                    cleanup_worktree_force(&resolved_cwd, worktree_dir.as_deref(), worktree_branch.as_deref()).await;
                    self.rollback_slot(run_id);
                    return Err(format!("failed to create worker principal: {:?}", e));
                }
            };
            if let Err(e) = Control::grant(&pool, worker_pid, &ns, true, true).await {
                cleanup_worker_principal(&pool, worker_pid, None).await;
                cleanup_worktree_force(&resolved_cwd, worktree_dir.as_deref(), worktree_branch.as_deref()).await;
                self.rollback_slot(run_id);
                return Err(format!("failed to grant worker namespace '{}': {:?}", ns, e));
            }
            let worker_token = match Control::mint_token(&pool, worker_pid, Some("headless-ephemeral"), Some(1)).await {
                Ok(t) => t,
                Err(e) => {
                    cleanup_worker_principal(&pool, worker_pid, None).await;
                    cleanup_worktree_force(&resolved_cwd, worktree_dir.as_deref(), worktree_branch.as_deref()).await;
                    self.rollback_slot(run_id);
                    return Err(format!("failed to mint ephemeral worker token: {:?}", e));
                }
            };

            let mut s_meta = json!({
                "headless": true,
                "subscription_only": req.subscription_only,
                "skip_permissions": req.skip_permissions,
                "auto_failover": req.auto_failover,
                "spawned_by_principal": caller_principal_id,
                "run_id": run_id,
                "display_label": format!("{} (Headless)", h_bin.label),
            });
            if let Some(ref sid) = clean_resume_id {
                s_meta["cli_session_id"] = json!(sid);
            }

            let s_opts = SessionOpts {
                harness: h_key.to_string(),
                model: model_trim.clone(),
                role: req.role.trim().to_string(),
                host: crate::nsresolve::hostname(),
                cwd: effective_cwd.to_string_lossy().to_string(),
                status: "active".to_string(),
                current_task: req.prompt.chars().take(120).collect(),
                metadata: s_meta,
                channels: init_channels,
                channel_ads: json!({}),
                ttl_seconds: 1800,
                webhook: Webhook::Unset,
                machine_id: crate::nsresolve::machine_id(),
                pid: None,
                tty: "headless".to_string(),
                run_state: "running".to_string(),
                auto_named: false,
            };
            if let Err(e) = Control::session_register(&pool, &ns, &handle, worker_pid, &s_opts).await {
                cleanup_worker_principal(&pool, worker_pid, Some(&worker_token)).await;
                cleanup_worktree_force(&resolved_cwd, worktree_dir.as_deref(), worktree_branch.as_deref()).await;
                self.rollback_slot(run_id);
                return Err(format!("failed to register headless session: {:?}", e));
            }
            channel_notify.notify_waiters();

            let mut mcp_cfg_file: Option<PathBuf> = None;
            if h_key == "claude-code" {
                let tmp_dir = config_dir().join("tmp");
                let _ = std::fs::create_dir_all(&tmp_dir);
                let cfg_path = tmp_dir.join(format!("mcp-headless-{}.json", run_id));
                let cfg_payload = json!({
                    "mcpServers": {
                        "memnos": {
                            "type": "http",
                            "url": format!("http://127.0.0.1:{}/mcp", port),
                            "headers": {
                                "Authorization": format!("Bearer {}", worker_token),
                                "X-Memnos-Namespace": ns,
                            }
                        }
                    }
                });
                let raw_cfg = serde_json::to_vec_pretty(&cfg_payload).unwrap_or_default();
                if write_private_file(&cfg_path, &raw_cfg).is_ok() {
                    mcp_cfg_file = Some(cfg_path);
                }
            }

            let task_prompt = if req.is_dm_turn && clean_resume_id.is_some() {
                req.prompt.trim().to_string()
            } else {
                build_worker_prompt(&ns, &handle, &ch_clean, &req.role, &req.prompt)
            };

            let mut cmd = Command::new(&exe_path);
            cmd.current_dir(&effective_cwd)
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
            if !req.subscription_only {
                for k in API_KEY_ENV_VARS {
                    if let Some(val) = env::var_os(k) {
                        cmd.env(k, val);
                    }
                }
            }
            cmd.env("TERM", "dumb")
                .env("NO_COLOR", "1")
                .env("MEMNOS_HEADLESS", "1")
                .env("MEMNOS_SESSION", &handle)
                .env("MEMNOS_NS", &ns)
                .env("MEMNOS_URL", format!("http://127.0.0.1:{}", port))
                .env("MEMNOS_TOKEN", &worker_token);

            match h_key {
                "claude-code" => {
                    cmd.args([
                        "-p",
                        "--output-format",
                        "stream-json",
                        "--verbose",
                        "--settings",
                        r#"{"hooks":{}}"#,
                    ]);
                    if let Some(ref sid) = clean_resume_id {
                        cmd.args(["--resume", sid]);
                    }
                    if let Some(ref mp) = mcp_cfg_file {
                        cmd.args(["--mcp-config", &mp.to_string_lossy(), "--strict-mcp-config"]);
                    }
                    if req.skip_permissions {
                        cmd.arg("--dangerously-skip-permissions");
                    }
                    if !model_trim.is_empty() {
                        cmd.args(["--model", &model_trim]);
                    }
                }
                "antigravity" => {
                    cmd.args(["-p", "--input-format", "text", "--output-format", "stream-json"]);
                    if let Some(ref sid) = clean_resume_id {
                        cmd.args(["--conversation", sid]);
                    }
                    if req.skip_permissions {
                        cmd.arg("--dangerously-skip-permissions");
                    }
                    if !model_trim.is_empty() {
                        cmd.args(["--model", &model_trim]);
                    }
                }
                "codex" => {
                    cmd.args(["exec", "--json"]);
                    if req.skip_permissions {
                        cmd.arg("--full-auto");
                    }
                    if !model_trim.is_empty() {
                        cmd.args(["--model", &model_trim]);
                    }
                    cmd.arg("-");
                }
                "gemini" => {
                    cmd.args(["-p", "", "--output-format", "stream-json"]);
                    if req.skip_permissions {
                        cmd.arg("--yolo");
                    }
                    if !model_trim.is_empty() {
                        cmd.args(["--model", &model_trim]);
                    }
                }
                "cursor" => {
                    cmd.args(["-p", "--output-format", "stream-json"]);
                    if !model_trim.is_empty() {
                        cmd.args(["--model", &model_trim]);
                    }
                }
                _ => {}
            }

            let mut child = match cmd.spawn() {
                Ok(ch) => ch,
                Err(e) => {
                    if let Some(ref mp) = mcp_cfg_file {
                        let _ = std::fs::remove_file(mp);
                    }
                    mark_session_offline(&pool, &ns, &handle, "failed", None).await;
                    cleanup_worker_principal(&pool, worker_pid, Some(&worker_token)).await;
                    cleanup_worktree_force(&resolved_cwd, worktree_dir.as_deref(), worktree_branch.as_deref()).await;
                    self.rollback_slot(run_id);
                    return Err(format!("failed to spawn {}: {}", h_bin.binary_name, e));
                }
            };

            if let Some(mut child_in) = child.stdin.take() {
                let prompt_bytes = task_prompt.into_bytes();
                tokio::spawn(async move {
                    let _ = child_in.write_all(&prompt_bytes).await;
                    let _ = child_in.shutdown().await;
                });
            }

            let child_pid = child.id();
            if let Some(p) = child_pid {
                if let Ok(c) = pool.get().await {
                    let _ = c
                        .execute(
                            "UPDATE memnos_control.agent_sessions SET pid = $3, run_state = 'running' WHERE namespace = $1 AND name = $2",
                            &[&ns, &handle, &(p as i64)],
                        )
                        .await;
                }
            }

            let init_log = format!(
                "[{}] ⚡ Started @{} ({} · cwd={})",
                chrono::Utc::now().format("%H:%M:%S"),
                handle,
                h_bin.label,
                effective_cwd.display()
            );
            let rec = WorkerRecord {
                run_id,
                namespace: ns.clone(),
                handle: handle.clone(),
                harness: h_key.to_string(),
                model: model_trim,
                role: req.role.trim().to_string(),
                channel: ch_clean.clone(),
                cwd: effective_cwd.to_string_lossy().to_string(),
                worktree_path: worktree_dir.as_ref().map(|p| p.to_string_lossy().to_string()),
                worktree_branch: worktree_branch.clone(),
                cli_session_id: clean_resume_id.clone(),
                subscription_only: req.subscription_only,
                skip_permissions: req.skip_permissions,
                auto_failover: req.auto_failover,
                status: "running".to_string(),
                pid: child_pid,
                started_at: started_iso.clone(),
                finished_at: None,
                last_line: "Worker running…".to_string(),
                log_lines: vec![init_log],
                exit_code: None,
                failover_to: None,
            };

            {
                let mut runs = self.runs.lock().unwrap();
                if let Some(slot) = runs.iter_mut().find(|r| r.run_id == run_id) {
                    *slot = rec.clone();
                }
            }

            let (kill_tx, mut kill_rx) = oneshot::channel::<()>();
            self.kill_senders.lock().unwrap().insert(run_id, kill_tx);

            let _ = event_tx.send(json!({
                "type": "activity",
                "op": "headless/spawn",
                "namespace": ns,
                "actor": handle,
                "detail": format!("{} · {}", h_bin.label, if req.is_dm_turn { "DM reply" } else if ch_clean.is_empty() { "direct task" } else { ch_clean.as_str() }),
                "ok": true,
                "status": 200,
                "latency_ms": 0,
                "ts": started_iso,
            }));

            let spawner_arc = Arc::clone(self);
            let orig_req = req.clone();
            let resolved_cwd_buf = resolved_cwd.clone();
            let watchdog_secs = self.timeout_secs;
            let run_started_utc = chrono::Utc::now();

            tokio::spawn(async move {
                let stdout_opt = child.stdout.take();
                let stderr_opt = child.stderr.take();
                let json_quota_err = Arc::new(Mutex::new(Option::<String>::None));
                let stderr_quota_cand = Arc::new(Mutex::new(Option::<String>::None));
                let captured_sid = Arc::new(Mutex::new(clean_resume_id));
                let final_reply_text = Arc::new(Mutex::new(String::new()));

                let read_stream = |out_opt: Option<tokio::process::ChildStdout>,
                                   sp: Arc<HeadlessSpawner>,
                                   rid: u64,
                                   ns_c: String,
                                   hd_c: String,
                                   ev_tx: broadcast::Sender<Value>,
                                   jq: Arc<Mutex<Option<String>>>,
                                   sid_slot: Arc<Mutex<Option<String>>>,
                                   reply_slot: Arc<Mutex<String>>| async move {
                    let Some(out) = out_opt else { return };
                    let mut reader = BufReader::new(out);
                    while let Some(line) = read_bounded_line(&mut reader, 65_536).await {
                        if let Some(q_err) = structured_json_quota_error(&line) {
                            let mut g = jq.lock().unwrap();
                            if g.is_none() {
                                *g = Some(q_err);
                            }
                        }
                        extract_session_and_reply(&line, &sid_slot, &reply_slot);
                        if let Some(s) = summarize_stream_line(&line) {
                            let ts_hms = chrono::Utc::now().format("%H:%M:%S").to_string();
                            {
                                let mut runs = sp.runs.lock().unwrap();
                                if let Some(r) = runs.iter_mut().find(|x| x.run_id == rid) {
                                    r.last_line = s.clone();
                                    if let Some(ref cur_sid) = *sid_slot.lock().unwrap() {
                                        r.cli_session_id = Some(cur_sid.clone());
                                    }
                                    push_log_line(&mut r.log_lines, format!("[{}] {}", ts_hms, s));
                                }
                            }
                            let _ = ev_tx.send(json!({
                                "type": "activity",
                                "op": "headless/step",
                                "namespace": ns_c,
                                "actor": hd_c,
                                "detail": s,
                                "ok": true,
                                "status": 200,
                                "latency_ms": 0,
                                "ts": chrono::Utc::now().to_rfc3339(),
                            }));
                        }
                    }
                };

                let read_err = |err_opt: Option<tokio::process::ChildStderr>,
                                sp: Arc<HeadlessSpawner>,
                                rid: u64,
                                sq: Arc<Mutex<Option<String>>>| async move {
                    let Some(err) = err_opt else { return };
                    let mut reader = BufReader::new(err);
                    while let Some(line) = read_bounded_line(&mut reader, 16_384).await {
                        let clean = line.trim();
                        if clean.is_empty() {
                            continue;
                        }
                        if looks_like_quota_error(clean) {
                            let mut g = sq.lock().unwrap();
                            if g.is_none() {
                                *g = Some(clean.chars().take(120).collect());
                            }
                        }
                        let short: String = clean.chars().take(120).collect();
                        let ts_hms = chrono::Utc::now().format("%H:%M:%S").to_string();
                        let mut runs = sp.runs.lock().unwrap();
                        if let Some(r) = runs.iter_mut().find(|x| x.run_id == rid) {
                            r.last_line = short.clone();
                            push_log_line(&mut r.log_lines, format!("[{}] ⚠ {}", ts_hms, short));
                        }
                    }
                };

                let mut t_out = tokio::spawn(read_stream(
                    stdout_opt,
                    Arc::clone(&spawner_arc),
                    run_id,
                    ns.clone(),
                    handle.clone(),
                    event_tx.clone(),
                    Arc::clone(&json_quota_err),
                    Arc::clone(&captured_sid),
                    Arc::clone(&final_reply_text),
                ));
                let mut t_err = tokio::spawn(read_err(
                    stderr_opt,
                    Arc::clone(&spawner_arc),
                    run_id,
                    Arc::clone(&stderr_quota_cand),
                ));

                let mut was_stopped = false;
                let mut timed_out = false;
                let exit_code = tokio::select! {
                    res = child.wait() => {
                        res.ok().and_then(|s| s.code())
                    }
                    _ = &mut kill_rx => {
                        was_stopped = true;
                        terminate_process_tree(&mut child, child_pid).await;
                        None
                    }
                    _ = tokio::time::sleep(Duration::from_secs(watchdog_secs)) => {
                        timed_out = true;
                        terminate_process_tree(&mut child, child_pid).await;
                        None
                    }
                };

                kill_process_group(child_pid);
                if tokio::time::timeout(Duration::from_secs(3), &mut t_out).await.is_err() {
                    t_out.abort();
                }
                if tokio::time::timeout(Duration::from_secs(2), &mut t_err).await.is_err() {
                    t_err.abort();
                }
                spawner_arc.kill_senders.lock().unwrap().remove(&run_id);

                if let Some(ref mp) = mcp_cfg_file {
                    let _ = std::fs::remove_file(mp);
                }

                let quota_reason = if was_stopped || timed_out {
                    None
                } else {
                    let jq = json_quota_err.lock().unwrap().clone();
                    let sq = stderr_quota_cand.lock().unwrap().clone();
                    if jq.is_some() {
                        jq
                    } else if exit_code != Some(0) {
                        sq
                    } else {
                        None
                    }
                };

                let final_status = if was_stopped {
                    "stopped"
                } else if timed_out {
                    "timed_out"
                } else if quota_reason.is_some() {
                    "quota_paused"
                } else if exit_code == Some(0) {
                    "completed"
                } else {
                    "failed"
                };

                if let Some(ref wt) = worktree_dir {
                    cleanup_worktree_if_untouched(
                        &resolved_cwd_buf,
                        wt,
                        worktree_branch.as_deref(),
                        &worktree_base_sha,
                    )
                    .await;
                }

                // Auto-deliver the headless worker's final response into the @Handle inbox thread (and #channel if needed)
                // so the user ALWAYS sees the worker's output & answers right inside MEMNOS.app!
                let reply_text = final_reply_text.lock().unwrap().trim().to_string();
                let final_sid = captured_sid.lock().unwrap().clone();

                if !was_stopped && !reply_text.is_empty() {
                    post_worker_reply_if_needed(
                        &pool,
                        &ns,
                        &handle,
                        worker_pid,
                        &orig_req.reply_to_session,
                        &ch_clean,
                        orig_req.is_dm_turn,
                        &reply_text,
                        run_started_utc,
                    )
                    .await;
                }

                let fin_iso = chrono::Utc::now().to_rfc3339();
                {
                    let ts_hms = chrono::Utc::now().format("%H:%M:%S").to_string();
                    let mut runs = spawner_arc.runs.lock().unwrap();
                    if let Some(r) = runs.iter_mut().find(|x| x.run_id == run_id) {
                        r.status = final_status.to_string();
                        r.finished_at = Some(fin_iso.clone());
                        r.exit_code = exit_code;
                        r.cli_session_id = final_sid.clone();
                        push_log_line(
                            &mut r.log_lines,
                            format!("[{}] ✓ Finished ({})", ts_hms, final_status),
                        );
                    }
                }

                let mut failover_spawned: Option<String> = None;

                if let Some(ref q_msg) = quota_reason {
                    let _ = Control::workspace_set_session_quota(
                        &pool,
                        worker_pid,
                        &ns,
                        &handle,
                        true,
                        60,
                        q_msg,
                    )
                    .await;

                    if orig_req.auto_failover && orig_req.failover_depth < 2 {
                        if let Some(alt_h) = pick_failover_harness(&orig_req.harness) {
                            let mut next_req = orig_req.clone();
                            next_req.harness = alt_h.to_string();
                            next_req.model = String::new();
                            next_req.handle = String::new();
                            next_req.resume_session_id = String::new();
                            next_req.cwd = resolved_cwd_buf.to_string_lossy().to_string();
                            next_req.failover_depth = orig_req.failover_depth + 1;
                            match spawner_arc
                                .launch(
                                    pool.clone(),
                                    port,
                                    caller_principal_id,
                                    event_tx.clone(),
                                    Arc::clone(&channel_notify),
                                    next_req,
                                )
                                .await
                            {
                                Ok(spawned) => failover_spawned = Some(spawned.handle),
                                Err(err_msg) => {
                                    let mut runs = spawner_arc.runs.lock().unwrap();
                                    if let Some(r) = runs.iter_mut().find(|x| x.run_id == run_id) {
                                        r.last_line = format!("failover skipped: {}", err_msg);
                                    }
                                }
                            }
                        }
                    }
                } else {
                    mark_session_offline(&pool, &ns, &handle, final_status, final_sid.as_deref())
                        .await;
                }

                cleanup_worker_principal(&pool, worker_pid, Some(&worker_token)).await;

                if failover_spawned.is_some() {
                    let mut runs = spawner_arc.runs.lock().unwrap();
                    if let Some(r) = runs.iter_mut().find(|x| x.run_id == run_id) {
                        r.failover_to = failover_spawned.clone();
                    }
                }

                let _ = event_tx.send(json!({
                    "type": "activity",
                    "op": format!("headless/{}", final_status),
                    "namespace": ns,
                    "actor": handle,
                    "detail": match &failover_spawned {
                        Some(fh) => format!("quota failover -> @{}", fh),
                        None => format!("exit={}", exit_code.unwrap_or(-1)),
                    },
                    "ok": final_status == "completed",
                    "status": if final_status == "completed" { 200 } else { 500 },
                    "latency_ms": 0,
                    "ts": fin_iso,
                }));
                channel_notify.notify_waiters();

                // If the user sent DMs while this turn was running, immediately wake `--resume` to answer them!
                if !was_stopped {
                    let queued = spawner_arc.take_queued_dms(&ns, &handle);
                    if !queued.is_empty() {
                        let last_from = queued
                            .last()
                            .map(|d| d.from_session.clone())
                            .unwrap_or_else(|| "owner".to_string());
                        let combined = queued
                            .iter()
                            .map(|d| format!("@{}: {}", d.from_session, d.content))
                            .collect::<Vec<_>>()
                            .join("\n\n");
                        let follow_req = LaunchRequest {
                            namespace: ns.clone(),
                            harness: orig_req.harness.clone(),
                            model: orig_req.model.clone(),
                            handle: handle.clone(),
                            role: orig_req.role.clone(),
                            channel: String::new(),
                            cwd: resolved_cwd_buf.to_string_lossy().to_string(),
                            prompt: format!(
                                "Direct message(s) received in MEMNOS Workspace while you were working:\n\n{}\n\nRespond directly and concisely to @{}.",
                                combined, last_from
                            ),
                            subscription_only: orig_req.subscription_only,
                            skip_permissions: orig_req.skip_permissions,
                            isolate_worktree: false,
                            auto_failover: orig_req.auto_failover,
                            failover_depth: 0,
                            resume_session_id: final_sid.unwrap_or_default(),
                            reply_to_session: last_from,
                            is_dm_turn: true,
                        };
                        let _ = spawner_arc
                            .launch(
                                pool,
                                port,
                                caller_principal_id,
                                event_tx,
                                channel_notify,
                                follow_req,
                            )
                            .await;
                    }
                }
            });

            Ok(rec)
        })
    }
}

fn push_log_line(lines: &mut Vec<String>, entry: String) {
    if lines.last() == Some(&entry) {
        return;
    }
    lines.push(entry);
    if lines.len() > 50 {
        let excess = lines.len() - 50;
        lines.drain(0..excess);
    }
}

async fn post_worker_reply_if_needed(
    pool: &Pool,
    ns: &str,
    handle: &str,
    worker_pid: i64,
    explicit_reply_to: &str,
    channel: &str,
    is_dm_turn: bool,
    reply_text: &str,
    run_started_utc: chrono::DateTime<chrono::Utc>,
) {
    let mut target_recipient = explicit_reply_to.trim().trim_start_matches('@').to_string();
    let mut already_sent_inbox = false;
    let mut already_sent_channel = false;

    if let Ok(c) = pool.get().await {
        // Check if there was a pending DM to this handle (e.g. from @owner) and mark it acted!
        if let Ok(Some(dm_row)) = c
            .query_opt(
                "SELECT from_session FROM memnos_control.agent_inbox
                 WHERE namespace = $1 AND to_session = $2 AND status IN ('pending', 'read') AND recalled_at IS NULL
                 ORDER BY id DESC LIMIT 1",
                &[&ns, &handle],
            )
            .await
        {
            let sender: String = dm_row.get(0);
            if target_recipient.is_empty() && !sender.is_empty() {
                target_recipient = sender;
            }
            let _ = c
                .execute(
                    "UPDATE memnos_control.agent_inbox
                     SET status = 'acted', acted_at = now(), resolution = 'replied by headless worker'
                     WHERE namespace = $1 AND to_session = $2 AND status IN ('pending', 'read') AND recalled_at IS NULL",
                    &[&ns, &handle],
                )
                .await;
        }

        if let Ok(r) = c
            .query_one(
                "SELECT count(*) FROM memnos_control.agent_inbox
                 WHERE namespace = $1 AND from_session = $2 AND created_at >= $3",
                &[&ns, &handle, &run_started_utc],
            )
            .await
        {
            let cnt: i64 = r.get(0);
            already_sent_inbox = cnt > 0;
        }

        if !channel.is_empty() {
            if let Ok(r) = c
                .query_one(
                    "SELECT count(*) FROM memnos_control.channel_messages
                     WHERE namespace = $1 AND channel = $2 AND sender_session = $3 AND created_at >= $4",
                    &[&ns, &channel, &handle, &run_started_utc],
                )
                .await
            {
                let cnt: i64 = r.get(0);
                already_sent_channel = cnt > 0;
            }
        }
    }

    if target_recipient.is_empty() {
        target_recipient = "owner".to_string();
    }

    let trimmed_reply: String = reply_text.chars().take(12_000).collect();

    if !already_sent_inbox && target_recipient != handle {
        let send_opts = SendOpts {
            subject: if is_dm_turn {
                String::new()
            } else {
                format!("Headless run summary (@{})", handle)
            },
            intent: "answer".to_string(),
            priority: "normal".to_string(),
            thread_key: String::new(),
            replaces_id: None,
            replace_pending: false,
            in_reply_to: None,
            metadata: json!({ "headless_auto_reply": true }),
        };
        let _ = Control::inbox_send(
            pool,
            ns,
            &target_recipient,
            handle,
            worker_pid,
            &trimmed_reply,
            &send_opts,
        )
        .await;
    }

    if !is_dm_turn && !channel.is_empty() && !already_sent_channel {
        let pub_opts = PublishOpts {
            intent: "message".to_string(),
            metadata: json!({ "headless_auto_summary": true }),
            description: String::new(),
            what_you_get: String::new(),
            claimable: false,
            reply_to: None,
        };
        let _ = Control::channel_publish(
            pool,
            ns,
            channel,
            handle,
            worker_pid,
            &trimmed_reply,
            &pub_opts,
        )
        .await;
    }
}

fn extract_session_and_reply(
    raw: &str,
    sid_slot: &Arc<Mutex<Option<String>>>,
    reply_slot: &Arc<Mutex<String>>,
) {
    let Ok(v) = serde_json::from_str::<Value>(raw.trim()) else {
        return;
    };
    if let Some(sid_raw) = v
        .get("session_id")
        .or_else(|| v.get("sessionId"))
        .or_else(|| v.get("conversation_id"))
        .and_then(|x| x.as_str())
    {
        if let Some(clean) = sanitize_session_id(sid_raw) {
            *sid_slot.lock().unwrap() = Some(clean);
        }
    }
    if v.get("type").and_then(|x| x.as_str()) == Some("result") {
        if let Some(res_txt) = v.get("result").and_then(|x| x.as_str()) {
            let t = res_txt.trim();
            if !t.is_empty() {
                *reply_slot.lock().unwrap() = t.to_string();
                return;
            }
        }
    }
    if let Some(arr) = v
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(|c| c.as_array())
    {
        for block in arr {
            if block.get("type").and_then(|x| x.as_str()) == Some("text") {
                if let Some(txt) = block.get("text").and_then(|x| x.as_str()) {
                    let t = txt.trim();
                    if !t.is_empty() {
                        *reply_slot.lock().unwrap() = t.to_string();
                    }
                }
            }
        }
    }
}

fn write_private_file(path: &Path, content: &[u8]) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .mode(0o600)
            .open(path)?;
        f.write_all(content)?;
        f.sync_all()
    }
    #[cfg(not(unix))]
    {
        std::fs::write(path, content)
    }
}

async fn read_bounded_line<R: tokio::io::AsyncRead + Unpin>(
    reader: &mut BufReader<R>,
    max_bytes: usize,
) -> Option<String> {
    let mut kept = Vec::with_capacity(1024);
    loop {
        let Ok(buf) = reader.fill_buf().await else {
            return if kept.is_empty() {
                None
            } else {
                Some(String::from_utf8_lossy(&kept).into_owned())
            };
        };
        if buf.is_empty() {
            return if kept.is_empty() {
                None
            } else {
                Some(String::from_utf8_lossy(&kept).into_owned())
            };
        }
        if let Some(nl) = buf.iter().position(|&b| b == b'\n') {
            let slice = &buf[..nl];
            if kept.len() < max_bytes {
                let room = max_bytes - kept.len();
                kept.extend_from_slice(&slice[..slice.len().min(room)]);
            }
            reader.consume(nl + 1);
            if kept.last() == Some(&b'\r') {
                kept.pop();
            }
            return Some(String::from_utf8_lossy(&kept).into_owned());
        } else {
            let n = buf.len();
            if kept.len() < max_bytes {
                let room = max_bytes - kept.len();
                kept.extend_from_slice(&buf[..n.min(room)]);
            }
            reader.consume(n);
        }
    }
}

fn kill_process_group(pid: Option<u32>) {
    #[cfg(unix)]
    if let Some(p) = pid.filter(|&p| p > 1) {
        unsafe {
            let _ = libc::kill(-(p as i32), libc::SIGTERM);
        }
    }
    #[cfg(not(unix))]
    let _ = pid;
}

async fn terminate_process_tree(child: &mut tokio::process::Child, pid: Option<u32>) {
    #[cfg(unix)]
    if let Some(p) = pid.filter(|&p| p > 1) {
        unsafe {
            let _ = libc::kill(-(p as i32), libc::SIGTERM);
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
        unsafe {
            let _ = libc::kill(-(p as i32), libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    if let Some(p) = pid.filter(|&p| p > 1) {
        let _ = Command::new("taskkill")
            .args(["/PID", &p.to_string(), "/T", "/F"])
            .output()
            .await;
    }
    let _ = child.kill().await;
    let _ = child.wait().await;
}

async fn mark_session_offline(
    pool: &Pool,
    ns: &str,
    handle: &str,
    run_state: &str,
    cli_sid: Option<&str>,
) {
    if let Ok(c) = pool.get().await {
        if let Some(sid) = cli_sid.filter(|s| !s.is_empty()) {
            let patch = json!({ "cli_session_id": sid });
            let _ = c
                .execute(
                    "UPDATE memnos_control.agent_sessions
                     SET status = 'offline', run_state = $3, expires_at = now(),
                         metadata = COALESCE(metadata, '{}'::jsonb) || $4::jsonb
                     WHERE namespace = $1 AND name = $2",
                    &[&ns, &handle, &run_state, &patch],
                )
                .await;
        } else {
            let _ = c
                .execute(
                    "UPDATE memnos_control.agent_sessions
                     SET status = 'offline', run_state = $3, expires_at = now()
                     WHERE namespace = $1 AND name = $2",
                    &[&ns, &handle, &run_state],
                )
                .await;
        }
    }
}

async fn cleanup_worker_principal(pool: &Pool, worker_pid: i64, token_opt: Option<&str>) {
    if let Some(t) = token_opt {
        let _ = Control::revoke_token(pool, t).await;
    }
    if let Ok(c) = pool.get().await {
        let _ = c
            .execute(
                "UPDATE memnos_control.api_tokens SET revoked = true WHERE principal_id = $1",
                &[&worker_pid],
            )
            .await;
        let _ = c
            .execute(
                "DELETE FROM memnos_control.grants WHERE principal_id = $1",
                &[&worker_pid],
            )
            .await;
    }
}

async fn cleanup_worktree_force(repo_cwd: &Path, wt_opt: Option<&Path>, branch_opt: Option<&str>) {
    if let Some(wt) = wt_opt {
        let _ = Command::new("git")
            .args([
                "-C",
                &repo_cwd.to_string_lossy(),
                "worktree",
                "remove",
                "--force",
                &wt.to_string_lossy(),
            ])
            .output()
            .await;
    }
    if let Some(br) = branch_opt {
        let _ = Command::new("git")
            .args(["-C", &repo_cwd.to_string_lossy(), "branch", "-D", br])
            .output()
            .await;
    }
}

async fn cleanup_worktree_if_untouched(
    repo_cwd: &Path,
    wt: &Path,
    branch_opt: Option<&str>,
    base_sha: &str,
) {
    let st_out = Command::new("git")
        .args(["-C", &wt.to_string_lossy(), "status", "--porcelain"])
        .output()
        .await;
    let is_clean = st_out
        .map(|o| o.status.success() && o.stdout.is_empty())
        .unwrap_or(false);
    if !is_clean {
        return;
    }
    let head_out = Command::new("git")
        .args(["-C", &wt.to_string_lossy(), "rev-parse", "HEAD"])
        .output()
        .await;
    let same_commit = head_out
        .map(|o| o.status.success() && String::from_utf8_lossy(&o.stdout).trim() == base_sha)
        .unwrap_or(false);
    if same_commit {
        cleanup_worktree_force(repo_cwd, Some(wt), branch_opt).await;
    }
}

async fn resolve_worker_cwd(pool: &Pool, namespace: &str, explicit_cwd: &str) -> Result<PathBuf, String> {
    let mut allowed_roots: Vec<PathBuf> = Vec::new();
    if let Ok(c) = pool.get().await {
        if let Ok(rows) = c
            .query(
                "SELECT key FROM memnos_control.bindings WHERE namespace = $1 AND key_type = 'host_path' ORDER BY updated_at DESC LIMIT 20",
                &[&namespace],
            )
            .await
        {
            for r in rows {
                let k: String = r.get(0);
                if let Ok(canon) = std::fs::canonicalize(&k) {
                    if canon.is_dir() && canon != Path::new("/") {
                        allowed_roots.push(canon);
                    }
                }
            }
        }
        let local_mid = crate::nsresolve::machine_id();
        if let Ok(rows) = c
            .query(
                "SELECT cwd FROM memnos_control.agent_sessions WHERE namespace = $1 AND machine_id = $2 AND COALESCE(cwd, '') <> '' AND cwd <> '/' ORDER BY last_heartbeat_at DESC LIMIT 20",
                &[&namespace, &local_mid],
            )
            .await
        {
            for r in rows {
                let k: String = r.get(0);
                if let Ok(canon) = std::fs::canonicalize(&k) {
                    if canon.is_dir() && canon != Path::new("/") && !allowed_roots.contains(&canon) {
                        allowed_roots.push(canon);
                    }
                }
            }
        }
    }
    if let Ok(srv_cwd) = env::current_dir().and_then(std::fs::canonicalize) {
        if srv_cwd != Path::new("/") && !allowed_roots.contains(&srv_cwd) {
            allowed_roots.push(srv_cwd);
        }
    }

    let trimmed = explicit_cwd.trim();
    if !trimmed.is_empty() {
        let canon = std::fs::canonicalize(trimmed)
            .map_err(|_| format!("directory '{}' does not exist", trimmed))?;
        if !canon.is_dir() || canon == Path::new("/") {
            return Err(format!("invalid working directory '{}'", trimmed));
        }
        if allowed_roots.iter().any(|root| canon.starts_with(root)) {
            return Ok(canon);
        }
        return Err(format!(
            "directory '{}' is not bound to namespace '{}' — bind the repo folder in Settings -> Repo Routing first (or leave blank to auto-detect)",
            canon.display(),
            namespace
        ));
    }

    if let Some(first) = allowed_roots.into_iter().next() {
        return Ok(first);
    }
    Err(format!(
        "no bound repository folder found for namespace '{}' — bind a folder in Repo Routing first",
        namespace
    ))
}

fn build_worker_prompt(
    namespace: &str,
    handle: &str,
    channel: &str,
    role: &str,
    user_prompt: &str,
) -> String {
    let role_line = if role.trim().is_empty() {
        String::new()
    } else {
        format!(" Your assigned role is '{}'.", role.trim())
    };
    if !user_prompt.trim().is_empty() {
        let ch_note = if channel.is_empty() {
            String::new()
        } else {
            format!(
                " Post your concise completion summary or review verdict to `{}` (`channel_publish` with `sender_session='{handle}'`, `namespace='{namespace}'`) when finished.",
                channel
            )
        };
        format!(
            "[MEMNOS Headless Session @{handle} in namespace '{namespace}'.{role_line}] First call `recall` in '{namespace}' (and `corpus_check` if a PRD/spec corpus exists) for project context and constraints. Complete the following task thoroughly, save any durable decisions with `remember`,{ch_note} and exit cleanly:\n\n{}",
            user_prompt.trim()
        )
    } else {
        let target_ch = if channel.is_empty() { "#general" } else { channel };
        format!(
            "[MEMNOS Autonomous Task Worker @{handle} on `{target_ch}` in namespace '{namespace}'.{role_line}]\n\
             1. Call `recall` in '{namespace}' to load project constraints and PRD context.\n\
             2. Call `channel_poll` on channel `{target_ch}` (`session_name='{handle}'`, `namespace='{namespace}'`) to claim ready open task(s), and check any pending `@inbox` items.\n\
             3. If there are no ready claimable tasks or inbox items, summarize briefly that no ready tasks are pending and exit immediately.\n\
             4. For each claimed task: acquire a `lease_acquire` on files you edit, implement/verify the task against project constraints (`corpus_check`), `remember` key decisions, release leases, and mark the task done via `channel_ack(message_id=<id>, session_name='{handle}', namespace='{namespace}', status='done', resolution='<summary>')`.\n\
             5. Once open ready tasks are completed, exit cleanly."
        )
    }
}

fn structured_json_quota_error(raw: &str) -> Option<String> {
    let v: Value = serde_json::from_str(raw.trim()).ok()?;
    let is_err_type = v.get("type").and_then(|x| x.as_str()) == Some("error")
        || v.get("is_error").and_then(|x| x.as_bool()) == Some(true)
        || v.get("subtype")
            .and_then(|x| x.as_str())
            .map(|s| s.starts_with("error"))
            .unwrap_or(false);
    if !is_err_type && v.get("error").is_none() {
        return None;
    }
    let candidate = v
        .get("result")
        .and_then(|x| x.as_str())
        .or_else(|| v.get("message").and_then(|x| x.as_str()))
        .or_else(|| {
            v.get("error")
                .and_then(|e| e.get("message").and_then(|m| m.as_str()).or_else(|| e.as_str()))
        })
        .unwrap_or("");
    if looks_like_quota_error(candidate) {
        Some(candidate.chars().take(120).collect())
    } else {
        None
    }
}

fn short_path_label(raw: &str) -> String {
    let parts: Vec<&str> = raw.trim().split(['/', '\\']).filter(|s| !s.is_empty()).collect();
    if parts.len() >= 2 {
        format!("{}/{}", parts[parts.len() - 2], parts[parts.len() - 1])
    } else {
        raw.chars().take(60).collect()
    }
}

fn summarize_tool_call(tname: &str, inp: Option<&Value>) -> String {
    let empty = json!({});
    let args = inp.unwrap_or(&empty);
    if let Some(mcp_op) = tname.strip_prefix("mcp__memnos__") {
        let det = args
            .get("channel")
            .or_else(|| args.get("to_session"))
            .or_else(|| args.get("query"))
            .or_else(|| args.get("topic"))
            .and_then(|x| x.as_str())
            .unwrap_or("");
        return if det.is_empty() {
            format!("📡 memnos · {}", mcp_op)
        } else {
            format!("📡 memnos · {} ({})", mcp_op, det.chars().take(48).collect::<String>())
        };
    }
    match tname {
        "Read" | "read_file" => {
            let p = args
                .get("file_path")
                .or_else(|| args.get("path"))
                .or_else(|| args.get("Path"))
                .and_then(|x| x.as_str())
                .unwrap_or("");
            if p.is_empty() {
                "📖 Read file".to_string()
            } else {
                format!("📖 Read · {}", short_path_label(p))
            }
        }
        "Edit" | "MultiEdit" | "Write" | "edit_file" | "write_file" => {
            let p = args
                .get("file_path")
                .or_else(|| args.get("path"))
                .or_else(|| args.get("Path"))
                .and_then(|x| x.as_str())
                .unwrap_or("");
            if p.is_empty() {
                format!("✏️ {}", tname)
            } else {
                format!("✏️ {} · {}", tname, short_path_label(p))
            }
        }
        "Bash" | "run_command" => {
            let c = args
                .get("command")
                .or_else(|| args.get("CommandLine"))
                .or_else(|| args.get("description"))
                .and_then(|x| x.as_str())
                .unwrap_or("");
            let flat = c.split_whitespace().collect::<Vec<_>>().join(" ");
            if flat.is_empty() {
                "💻 Bash".to_string()
            } else {
                format!("💻 Bash · {}", flat.chars().take(84).collect::<String>())
            }
        }
        "Grep" | "Glob" => {
            let pat = args
                .get("pattern")
                .and_then(|x| x.as_str())
                .unwrap_or("");
            if pat.is_empty() {
                format!("🔍 {}", tname)
            } else {
                format!("🔍 {} · {}", tname, pat.chars().take(60).collect::<String>())
            }
        }
        _ => format!("🔧 {}", tname),
    }
}

fn summarize_stream_line(raw: &str) -> Option<String> {
    let t = raw.trim();
    if t.is_empty() {
        return None;
    }
    if let Ok(v) = serde_json::from_str::<Value>(t) {
        if let Some(msg) = v.get("message") {
            if let Some(arr) = msg.get("content").and_then(|c| c.as_array()) {
                for block in arr {
                    let btype = block.get("type").and_then(|x| x.as_str()).unwrap_or("");
                    if btype == "tool_use" {
                        let tname = block.get("name").and_then(|x| x.as_str()).unwrap_or("tool");
                        return Some(summarize_tool_call(tname, block.get("input")));
                    }
                    if btype == "text" {
                        if let Some(txt) = block.get("text").and_then(|x| x.as_str()) {
                            let flat = txt.split_whitespace().collect::<Vec<_>>().join(" ");
                            if !flat.is_empty() {
                                return Some(format!("💬 {}", flat.chars().take(108).collect::<String>()));
                            }
                        }
                    }
                }
            }
        }
        if v.get("type").and_then(|x| x.as_str()) == Some("result") {
            if let Some(res) = v.get("result").and_then(|x| x.as_str()) {
                let flat = res.split_whitespace().collect::<Vec<_>>().join(" ");
                if !flat.is_empty() {
                    return Some(format!("✅ {}", flat.chars().take(108).collect::<String>()));
                }
            }
        }
        if let Some(txt) = v.get("content").or_else(|| v.get("text")).and_then(|x| x.as_str()) {
            let flat = txt.split_whitespace().collect::<Vec<_>>().join(" ");
            if !flat.is_empty() {
                return Some(format!("💬 {}", flat.chars().take(108).collect::<String>()));
            }
        }
        return None;
    }
    Some(t.chars().take(108).collect())
}

pub fn looks_like_quota_error(s: &str) -> bool {
    let l = s.to_ascii_lowercase();
    l.contains("rate_limit_error")
        || l.contains("hit your limit")
        || l.contains("usage limit")
        || l.contains("quota exceeded")
        || l.contains("insufficient_quota")
        || l.contains("out of credits")
        || (l.contains("429") && (l.contains("rate limit") || l.contains("too many requests")))
}

fn pick_failover_harness(failed_raw: &str) -> Option<&'static str> {
    let failed = normalize_harness_key(failed_raw);
    let installed: Vec<HarnessBinary> = discover_harnesses()
        .into_iter()
        .filter(|h| h.installed && h.key != failed)
        .collect();
    for pref in ["antigravity", "claude-code", "codex", "gemini", "cursor"] {
        if installed.iter().any(|h| h.key == pref) {
            return Some(pref);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn structured_quota_ignores_normal_prose_and_tool_results() {
        let prose = r#"{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I checked rate_limit_error and hit your limit strings in spawner.rs"}]}}"#;
        assert!(structured_json_quota_error(prose).is_none());

        let ok_res = r#"{"type":"result","is_error":false,"result":"Reviewed quota exceeded handling cleanly."}"#;
        assert!(structured_json_quota_error(ok_res).is_none());

        let real_err = r#"{"type":"result","is_error":true,"result":"API Error: 429 rate_limit_error — you have hit your limit"}"#;
        assert!(structured_json_quota_error(real_err).is_some());
    }

    #[test]
    fn summarize_stream_line_extracts_human_readable_details() {
        let read_ev = r#"{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/repo/src/billing.ts"}}]}}"#;
        assert_eq!(
            summarize_stream_line(read_ev).as_deref(),
            Some("📖 Read · src/billing.ts")
        );

        let mcp_ev = r##"{"type":"assistant","message":{"content":[{"type":"tool_use","name":"mcp__memnos__channel_publish","input":{"channel":"#arch-review"}}]}}"##;
        assert_eq!(
            summarize_stream_line(mcp_ev).as_deref(),
            Some("📡 memnos · channel_publish (#arch-review)")
        );
    }

    #[test]
    fn session_id_sanitizer_blocks_flag_injection() {
        assert_eq!(
            sanitize_session_id("8ff58386-be5a-4d57-b6b7-66cb09d96069").as_deref(),
            Some("8ff58386-be5a-4d57-b6b7-66cb09d96069")
        );
        assert!(sanitize_session_id("--dangerously-skip-permissions").is_none());
        assert!(sanitize_session_id("bad id with spaces").is_none());
        assert!(sanitize_session_id("bad;rm").is_none());
    }
}
