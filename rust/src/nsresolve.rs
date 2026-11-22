//! Cross-OS namespace resolution, terminal session detection, and PID liveness (`nsresolve`).

use crate::config::config_dir;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalSessionInfo {
    pub name: String,
    pub fallback_suffix: String,
    pub display_label: String,
    pub suggested_topic: String,
    pub harness: String,
    pub pid: u32,
    pub tty: String,
    pub machine_id: String,
    pub host: String,
    pub cwd: String,
    pub auto_named: bool,
    pub orca_terminal_id: String,
    pub orca_pane_key: String,
    pub orca_worktree_id: String,
    pub tmux_pane: String,
}

impl TerminalSessionInfo {
    pub fn session_metadata(&self) -> Value {
        let mut m = serde_json::Map::new();
        m.insert("auto_named".into(), json!(self.auto_named));
        m.insert("display_label".into(), json!(self.display_label));
        m.insert("suggested_topic".into(), json!(self.suggested_topic));
        if !self.orca_terminal_id.is_empty() {
            m.insert("orca_terminal_id".into(), json!(self.orca_terminal_id));
        }
        if !self.orca_pane_key.is_empty() {
            m.insert("orca_pane_key".into(), json!(self.orca_pane_key));
        }
        if !self.orca_worktree_id.is_empty() {
            m.insert("orca_worktree_id".into(), json!(self.orca_worktree_id));
        }
        if !self.tmux_pane.is_empty() {
            m.insert("tmux_pane".into(), json!(self.tmux_pane));
        }
        Value::Object(m)
    }
}

pub fn sanitize_slug(s: &str, max_len: usize) -> String {
    let mut out = String::with_capacity(s.len());
    let mut prev_dash = false;
    for ch in s.chars() {
        let low = ch.to_ascii_lowercase();
        if low.is_ascii_alphanumeric() {
            out.push(low);
            prev_dash = false;
        } else if !prev_dash && !out.is_empty() {
            out.push('-');
            prev_dash = true;
        }
        if out.len() >= max_len {
            break;
        }
    }
    out.trim_matches('-').to_string()
}

pub fn hostname() -> String {
    if let Ok(h) = env::var("HOSTNAME").or_else(|_| env::var("COMPUTERNAME")) {
        if !h.trim().is_empty() {
            return h.trim().to_string();
        }
    }
    if let Ok(out) = Command::new("hostname").output() {
        let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !s.is_empty() {
            return s;
        }
    }
    "localhost".to_string()
}

pub fn machine_id() -> String {
    let s = sanitize_slug(&hostname(), 64);
    let mid = if s.is_empty() {
        "unknown".to_string()
    } else {
        s
    };
    let dir = config_dir();
    if fs::create_dir_all(&dir).is_ok() {
        let _ = fs::write(dir.join("machine_id"), &mid);
    }
    mid
}

/// Cross-OS process liveness check (macOS, Linux, Windows).
pub fn is_local_pid_alive(pid: u32) -> bool {
    if pid <= 1 {
        return false;
    }
    #[cfg(unix)]
    {
        extern "C" {
            fn kill(pid: i32, sig: i32) -> i32;
        }
        let rc = unsafe { kill(pid as i32, 0) };
        if rc == 0 {
            return true;
        }
        let err = io::Error::last_os_error();
        err.raw_os_error() == Some(1) // EPERM means process exists
    }
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        extern "system" {
            fn OpenProcess(
                dwDesiredAccess: u32,
                bInheritHandle: i32,
                dwProcessId: u32,
            ) -> *mut std::ffi::c_void;
            fn GetExitCodeProcess(hProcess: *mut std::ffi::c_void, lpExitCode: *mut u32) -> i32;
            fn CloseHandle(hObject: *mut std::ffi::c_void) -> i32;
        }
        unsafe {
            let handle = OpenProcess(0x1000, 0, pid); // PROCESS_QUERY_LIMITED_INFORMATION
            if handle.is_null() {
                return false;
            }
            let mut code: u32 = 0;
            let ok = GetExitCodeProcess(handle, &mut code);
            CloseHandle(handle);
            ok != 0 && code == 259 // STILL_ACTIVE
        }
    }
}

pub fn git_root(cwd: &Path) -> Option<PathBuf> {
    let out = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(cwd)
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(PathBuf::from(s))
    }
}

pub fn git_branch(cwd: &Path) -> String {
    let out = Command::new("git")
        .args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(cwd)
        .stderr(Stdio::null())
        .output();
    if let Ok(o) = out {
        if o.status.success() {
            let b = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if !b.is_empty() && b != "HEAD" {
                return b;
            }
        }
    }
    String::new()
}

pub fn normalize_remote(url: &str) -> Option<String> {
    let mut u = url.trim().to_string();
    if u.is_empty() {
        return None;
    }
    if let Some(rest) = u.strip_prefix("git@") {
        u = rest.replacen(':', "/", 1);
    } else {
        let re = Regex::new(r"(?i)^[a-z0-9+.\-]+://").unwrap();
        u = re.replace(&u, "").to_string();
        if let Some((host_part, rest)) = u.split_once('/') {
            if let Some((_, clean_host)) = host_part.split_once('@') {
                u = format!("{}/{}", clean_host, rest);
            }
        }
    }
    if let Some(stripped) = u.strip_suffix(".git") {
        u = stripped.to_string();
    }
    let clean = u.trim_end_matches('/').to_ascii_lowercase();
    if clean.is_empty() {
        None
    } else {
        Some(clean)
    }
}

pub fn repo_key(cwd: &Path) -> Option<String> {
    for remote in ["origin", "github", "upstream"] {
        let out = Command::new("git")
            .args(["remote", "get-url", remote])
            .current_dir(cwd)
            .stderr(Stdio::null())
            .output();
        if let Ok(o) = out {
            if o.status.success() {
                let url = String::from_utf8_lossy(&o.stdout).trim().to_string();
                if let Some(norm) = normalize_remote(&url) {
                    return Some(norm);
                }
            }
        }
    }
    None
}

pub fn resolve_with_source(cwd: &Path, payload: Option<&Value>) -> (String, String) {
    if let Some(d) = payload {
        if let Some(ns) = d.get("namespace").and_then(|v| v.as_str()) {
            let t = ns.trim();
            if !t.is_empty() && t != "auto" {
                return (t.to_string(), "explicit".to_string());
            }
        }
    }
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let cwd_str = real_cwd.to_string_lossy().to_string();
    let rk = repo_key(&real_cwd);
    let mid = machine_id();

    // 1. Server-synced bindings cache
    if let Ok(raw) = fs::read_to_string(config_dir().join("bindings_cache.json")) {
        if let Ok(val) = serde_json::from_str::<Value>(&raw) {
            if let Some(bindings) = val.get("bindings").and_then(|b| b.as_array()) {
                if let Some(ref repo) = rk {
                    for b in bindings {
                        if b.get("key_type").and_then(|v| v.as_str()) == Some("repo")
                            && b.get("key").and_then(|v| v.as_str()) == Some(repo.as_str())
                        {
                            if let Some(ns) = b.get("namespace").and_then(|v| v.as_str()) {
                                return (ns.to_string(), "binding".to_string());
                            }
                        }
                    }
                    for b in bindings {
                        if b.get("key_type").and_then(|v| v.as_str()) == Some("host_repo")
                            && b.get("host_id").and_then(|v| v.as_str()) == Some(mid.as_str())
                            && b.get("key").and_then(|v| v.as_str()) == Some(repo.as_str())
                        {
                            if let Some(ns) = b.get("namespace").and_then(|v| v.as_str()) {
                                return (ns.to_string(), "binding".to_string());
                            }
                        }
                    }
                }
                for b in bindings {
                    if b.get("key_type").and_then(|v| v.as_str()) == Some("host_path")
                        && b.get("host_id").and_then(|v| v.as_str()) == Some(mid.as_str())
                    {
                        let key = b.get("key").and_then(|v| v.as_str()).unwrap_or("");
                        if !key.is_empty()
                            && (cwd_str == key || cwd_str.starts_with(&format!("{}/", key)))
                        {
                            if let Some(ns) = b.get("namespace").and_then(|v| v.as_str()) {
                                return (ns.to_string(), "binding".to_string());
                            }
                        }
                    }
                }
            }
        }
    }

    // 2. Local ns_overrides.json
    if let Ok(raw) = fs::read_to_string(config_dir().join("ns_overrides.json")) {
        if let Ok(map) = serde_json::from_str::<Value>(&raw) {
            if let Some(obj) = map.as_object() {
                let mut cur = Some(real_cwd.as_path());
                while let Some(p) = cur {
                    if let Some(ns) = obj.get(&p.to_string_lossy().to_string()).and_then(|v| v.as_str()) {
                        return (ns.to_string(), "override".to_string());
                    }
                    cur = p.parent();
                }
            }
        }
    }

    // 3. MEMNOS_NS env
    if let Ok(env_ns) = env::var("MEMNOS_NS") {
        let t = env_ns.trim();
        if !t.is_empty() && t != "auto" {
            return (t.to_string(), "env".to_string());
        }
    }

    // 4. Derived proj:<repo>
    let root = git_root(&real_cwd).unwrap_or(real_cwd);
    let base = root.file_name().and_then(|s| s.to_str()).unwrap_or("default");
    let slug = sanitize_slug(base, 40);
    (
        format!("proj:{}", if slug.is_empty() { "default" } else { &slug }),
        "default".to_string(),
    )
}

pub fn infer_topic_channel(cwd: &Path, topic_or_task: &str) -> String {
    let raw = topic_or_task.trim().trim_start_matches('#');
    if !raw.is_empty() {
        let slug = sanitize_slug(raw, 48);
        if !slug.is_empty() {
            return format!("#{}", slug);
        }
    }
    let root = git_root(cwd).unwrap_or_else(|| cwd.to_path_buf());
    let base = root.file_name().and_then(|s| s.to_str()).unwrap_or("general");
    let repo_slug = {
        let s = sanitize_slug(base, 24);
        if s.is_empty() { "general".to_string() } else { s }
    };
    let branch = git_branch(cwd);
    let low_b = branch.to_ascii_lowercase();
    if !low_b.is_empty()
        && !matches!(low_b.as_str(), "main" | "master" | "develop" | "dev" | "head" | "trunk")
    {
        let stripped = low_b
            .trim_start_matches("feat/")
            .trim_start_matches("feature/")
            .trim_start_matches("fix/")
            .trim_start_matches("bugfix/")
            .trim_start_matches("chore/");
        let b_slug = sanitize_slug(stripped, 24);
        if !b_slug.is_empty() && b_slug != repo_slug {
            return format!("#{}-{}", repo_slug, b_slug);
        }
    }
    format!("#{}", repo_slug)
}

fn parent_tty_and_harness() -> (String, String, u32) {
    let mut harness = String::new();
    let mut agent_pid = std::process::id();
    let mut tty_str = String::new();

    if env::var("ANTIGRAVITY_CLI").is_ok() || env::var("AGY_SESSION_ID").is_ok() {
        harness = "antigravity".to_string();
    } else if env::var("CLAUDE_CODE").is_ok()
        || env::var("CLAUDECODE").is_ok()
        || env::var("CLAUDE_PLUGIN_ROOT").is_ok()
        || env::var("CLAUDE_PROJECT_DIR").is_ok()
    {
        harness = "claude-code".to_string();
    } else if env::var("CURSOR_VERSION").is_ok() || env::var("CURSOR_TRACE_ID").is_ok() {
        harness = "cursor".to_string();
    } else if env::var("GEMINI_CLI").is_ok() {
        harness = "gemini".to_string();
    }

    #[cfg(unix)]
    {
        let mut cur = unsafe { libc_getppid() };
        if cur > 1 {
            agent_pid = cur as u32;
        }
        for _ in 0..6 {
            if cur <= 1 {
                break;
            }
            let out = Command::new("ps")
                .args(["-o", "pid=,ppid=,tty=,comm=,args=", "-p", &cur.to_string()])
                .output();
            let Ok(o) = out else { break };
            let line = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if line.is_empty() {
                break;
            }
            let parts: Vec<&str> = line
                .splitn(5, char::is_whitespace)
                .filter(|s| !s.is_empty())
                .collect();
            if parts.len() < 4 {
                break;
            }
            let p_pid: u32 = parts[0].parse().unwrap_or(cur as u32);
            let p_ppid: i32 = parts[1].parse().unwrap_or(0);
            let p_tty = parts[2];
            let p_comm = parts[3].to_ascii_lowercase();
            let p_base = Path::new(&p_comm)
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or(&p_comm);
            let p_args = if parts.len() > 4 { parts[4] } else { parts[3] };
            if tty_str.is_empty() && p_tty != "??" && p_tty != "?" && p_tty != "-" {
                tty_str = p_tty.to_string();
            }
            let low = p_args.to_ascii_lowercase();
            if harness.is_empty() {
                if low.contains("claude.app") && !p_base.contains("disclaimer") {
                    harness = "claude-desktop".to_string();
                    agent_pid = p_pid;
                } else if p_base == "claude" {
                    harness = "claude-code".to_string();
                    agent_pid = p_pid;
                } else if p_base == "agy" || p_base == "antigravity" {
                    harness = "antigravity".to_string();
                    agent_pid = p_pid;
                } else if p_base == "gemini" {
                    harness = "gemini".to_string();
                    agent_pid = p_pid;
                } else if p_base == "cursor" {
                    harness = "cursor".to_string();
                    agent_pid = p_pid;
                }
            } else if (harness == "claude-code" && p_base == "claude")
                || (harness == "antigravity" && (p_base == "agy" || p_base == "antigravity"))
            {
                agent_pid = p_pid;
            }
            cur = p_ppid;
        }
    }

    (tty_str, harness, agent_pid)
}

fn slugify_session_title(title: &str) -> String {
    use once_cell::sync::Lazy;
    static HEX_TAIL_RE: Lazy<Regex> =
        Lazy::new(|| Regex::new(r"(?i)\|\s*[0-9a-f]{8}\s*$").unwrap());
    static NON_SLUG_RE: Lazy<Regex> =
        Lazy::new(|| Regex::new(r"[^a-z0-9_.:-]+").unwrap());
    let stripped = HEX_TAIL_RE.replace(title.trim(), "").to_ascii_lowercase();
    let slug = NON_SLUG_RE
        .replace_all(&stripped, "-")
        .trim_matches(|c| matches!(c, '-' | '.' | '_' | ':'))
        .to_string();
    slug.chars().take(64).collect()
}

pub fn transcript_session_name(transcript_path: &str) -> String {
    if transcript_path.is_empty() {
        return String::new();
    }
    let Ok(mut f) = fs::File::open(transcript_path) else {
        return String::new();
    };
    use std::io::{Read, Seek, SeekFrom};
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    if len == 0 {
        return String::new();
    }

    let extract_from_chunk = |chunk: &str| -> String {
        for line in chunk.lines().rev() {
            if !line.contains("agentName") && !line.contains("customTitle") {
                continue;
            }
            if let Ok(obj) = serde_json::from_str::<Value>(line) {
                let t = obj
                    .get("agentName")
                    .or_else(|| obj.get("customTitle"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if !t.is_empty() {
                    let s = slugify_session_title(t);
                    if !s.is_empty() {
                        return s;
                    }
                }
            }
        }
        String::new()
    };

    // 1. Scan the tail (last 128 KB) in reverse so mid-session `/rename` wins immediately!
    let tail_start = len.saturating_sub(131_072);
    if f.seek(SeekFrom::Start(tail_start)).is_ok() {
        let mut tail = String::new();
        if f.read_to_string(&mut tail).is_ok() {
            let found = extract_from_chunk(&tail);
            if !found.is_empty() {
                return found;
            }
        }
    }

    // 2. Fallback: if transcript > 128 KB and title was only written near line 0, check head (32 KB)
    if tail_start > 0 && f.seek(SeekFrom::Start(0)).is_ok() {
        let mut head_buf = vec![0u8; 32_768.min(len as usize)];
        if let Ok(n) = f.read(&mut head_buf) {
            let head = String::from_utf8_lossy(&head_buf[..n]);
            let found = extract_from_chunk(&head);
            if !found.is_empty() {
                return found;
            }
        }
    }
    String::new()
}

pub fn save_active_session(name: &str, cwd: &Path, namespace: &str) {
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let cwd_key = real_cwd.to_string_lossy().to_string();
    let (tty, harness, _) = parent_tty_and_harness();
    save_active_session_for_tty(name, &cwd_key, namespace, &tty, &harness);
}

pub fn save_active_session_for_tty(
    name: &str,
    cwd_key: &str,
    namespace: &str,
    tty: &str,
    harness: &str,
) {
    let p = config_dir().join("active_sessions.json");
    let mut map: serde_json::Map<String, Value> = fs::read_to_string(&p)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    let entry = json!({
        "name": name,
        "namespace": namespace,
        "tty": tty,
        "harness": harness,
    });
    if !tty.is_empty() {
        let suffix = format!("::{}", tty);
        // Update any stale subdirectory entries belonging to this exact interactive TTY!
        let stale_tty_keys: Vec<String> = map
            .keys()
            .filter(|k| k.ends_with(&suffix))
            .cloned()
            .collect();
        for k in stale_tty_keys {
            if let Some(obj) = map.get_mut(&k).and_then(|v| v.as_object_mut()) {
                obj.insert("name".to_string(), json!(name));
            }
        }
        map.insert(format!("{}::{}", cwd_key, tty), entry.clone());
    }
    if !harness.is_empty() {
        map.insert(format!("{}::harness:{}", cwd_key, harness), entry.clone());
    } else if !cwd_key.is_empty() {
        map.entry(cwd_key.to_string()).or_insert(entry);
    }
    if fs::create_dir_all(config_dir()).is_ok() {
        let _ = fs::write(&p, serde_json::to_string_pretty(&map).unwrap_or_default());
    }
}

pub fn clear_active_session(name: &str, cwd: &Path) {
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let p = config_dir().join("active_sessions.json");
    let Ok(raw) = fs::read_to_string(&p) else { return };
    let Ok(mut map) = serde_json::from_str::<serde_json::Map<String, Value>>(&raw) else {
        return;
    };
    let cwd_key = real_cwd.to_string_lossy().to_string();
    let (tty, harness, _) = parent_tty_and_harness();
    let mut changed = false;
    for k in [
        format!("{}::{}", cwd_key, tty),
        format!("{}::harness:{}", cwd_key, harness),
        cwd_key,
    ] {
        if let Some(cur) = map.get(&k) {
            if name.is_empty() || cur.get("name").and_then(|v| v.as_str()) == Some(name) {
                map.remove(&k);
                changed = true;
            }
        }
    }
    if changed {
        let _ = fs::write(&p, serde_json::to_string_pretty(&map).unwrap_or_default());
    }
}

pub fn resolve_active_session(cwd: &Path) -> String {
    resolve_active_session_with_data(cwd, None)
}

pub fn resolve_active_session_with_data(cwd: &Path, data: Option<&Value>) -> String {
    if let Ok(env_s) = env::var("MEMNOS_SESSION") {
        let clean = env_s.trim().trim_start_matches('@');
        if !clean.is_empty() {
            return clean.to_string();
        }
    }
    if let Some(d) = data {
        if let Some(sn) = d
            .get("session_name")
            .or_else(|| d.get("sessionName"))
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            let slug = slugify_session_title(sn);
            if !slug.is_empty() {
                return slug;
            }
        }
        let tp = d
            .get("transcript_path")
            .or_else(|| d.get("transcriptPath"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let from_tp = transcript_session_name(tp);
        if !from_tp.is_empty() {
            return from_tp;
        }
    }
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let cwd_key = real_cwd.to_string_lossy().to_string();
    let (tty, harness, _) = parent_tty_and_harness();
    if let Ok(raw) = fs::read_to_string(config_dir().join("active_sessions.json")) {
        if let Ok(map) = serde_json::from_str::<Value>(&raw) {
            if !tty.is_empty() {
                let tty_key = format!("{}::{}", cwd_key, tty);
                if let Some(n) = map
                    .get(&tty_key)
                    .and_then(|v| v.get("name"))
                    .and_then(|v| v.as_str())
                {
                    return n.to_string();
                }
            }
            if !harness.is_empty() {
                let h_key = format!("{}::harness:{}", cwd_key, harness);
                if let Some(n) = map
                    .get(&h_key)
                    .and_then(|v| v.get("name"))
                    .and_then(|v| v.as_str())
                {
                    return n.to_string();
                }
            }
            if let Some(bare) = map.get(&cwd_key) {
                let bare_h = bare
                    .get("harness")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if (bare_h.is_empty() || harness.is_empty() || bare_h == harness)
                    && (harness.is_empty()
                        || !bare
                            .get("name")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .starts_with(if harness == "claude-code" {
                                "agy-"
                            } else if harness == "antigravity" {
                                "tommy-"
                            } else {
                                "\0"
                            }))
                {
                    if let Some(n) = bare.get("name").and_then(|v| v.as_str()) {
                        return n.to_string();
                    }
                }
            }
        }
    }
    String::new()
}

#[cfg(unix)]
unsafe fn libc_getppid() -> i32 {
    extern "C" {
        fn getppid() -> i32;
    }
    getppid()
}

pub fn detect_terminal_session(cwd: &Path) -> TerminalSessionInfo {
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let explicit_name = resolve_active_session(&real_cwd);
    let mut harness = String::new();
    let mut agent_pid = std::process::id();
    let mut tty_str = String::new();

    #[cfg(unix)]
    {
        let mut cur = unsafe { libc_getppid() };
        if cur > 1 {
            agent_pid = cur as u32;
        }
        for _ in 0..5 {
            if cur <= 1 {
                break;
            }
            let out = Command::new("ps")
                .args(["-o", "pid=,ppid=,tty=,comm=,args=", "-p", &cur.to_string()])
                .output();
            let Ok(o) = out else { break };
            let line = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if line.is_empty() {
                break;
            }
            let parts: Vec<&str> = line
                .splitn(5, char::is_whitespace)
                .filter(|s| !s.is_empty())
                .collect();
            if parts.len() < 4 {
                break;
            }
            let p_pid: u32 = parts[0].parse().unwrap_or(cur as u32);
            let p_ppid: i32 = parts[1].parse().unwrap_or(0);
            let p_tty = parts[2];
            let p_args = if parts.len() > 4 { parts[4] } else { parts[3] };
            if tty_str.is_empty() && p_tty != "??" && p_tty != "?" && p_tty != "-" {
                tty_str = p_tty.to_string();
            }
            let low = p_args.to_ascii_lowercase();
            if harness.is_empty() {
                if low.contains("claude.app") {
                    harness = "claude-desktop".to_string();
                    agent_pid = p_pid;
                } else if low.contains("claude") && !low.contains("disclaimer") {
                    harness = "claude-code".to_string();
                    agent_pid = p_pid;
                } else if low.contains("agy") || low.contains("antigravity") {
                    harness = "antigravity".to_string();
                    agent_pid = p_pid;
                } else if low.contains("gemini") {
                    harness = "gemini".to_string();
                    agent_pid = p_pid;
                } else if low.contains("cursor") {
                    harness = "cursor".to_string();
                    agent_pid = p_pid;
                }
            }
            cur = p_ppid;
        }
    }

    if harness.is_empty() {
        if env::var("CLAUDE_PLUGIN_ROOT").is_ok() || env::var("CLAUDE_PROJECT_DIR").is_ok() {
            harness = "claude-code".to_string();
        } else if env::var("ANTIGRAVITY_CLI").is_ok() {
            harness = "antigravity".to_string();
        } else if env::var("CURSOR_VERSION").is_ok() {
            harness = "cursor".to_string();
        }
    }

    let root = git_root(&real_cwd).unwrap_or_else(|| real_cwd.clone());
    let repo_base = root.file_name().and_then(|s| s.to_str()).unwrap_or("workspace");
    let repo_slug = {
        let s = sanitize_slug(repo_base, 22);
        if s.is_empty() { "workspace".to_string() } else { s }
    };
    let branch = git_branch(&real_cwd);
    let low_b = branch.to_ascii_lowercase();
    let branch_slug = if !low_b.is_empty()
        && !matches!(low_b.as_str(), "main" | "master" | "develop" | "dev" | "head" | "trunk")
    {
        sanitize_slug(
            low_b
                .trim_start_matches("feat/")
                .trim_start_matches("feature/")
                .trim_start_matches("fix/"),
            18,
        )
    } else {
        String::new()
    };

    let h_prefix = match harness.as_str() {
        "claude-code" => "claude",
        "claude-desktop" => "claude-app",
        "antigravity" => "agy",
        "gemini" => "gemini",
        "cursor" => "cursor",
        _ => "agent",
    };
    let h_title = match harness.as_str() {
        "claude-code" => "Claude Code",
        "claude-desktop" => "Claude Desktop",
        "antigravity" => "Antigravity",
        "gemini" => "Gemini CLI",
        "cursor" => "Cursor",
        _ => "Agent",
    };

    let auto_named = explicit_name.is_empty();
    let name = if auto_named {
        if !branch_slug.is_empty() && branch_slug != repo_slug {
            format!("{}-{}-{}", h_prefix, repo_slug, branch_slug)
        } else {
            format!("{}-{}", h_prefix, repo_slug)
        }
    } else {
        explicit_name
    };

    let mut display_label = format!("{} · {}", h_title, repo_slug);
    if !branch.is_empty() {
        display_label.push_str(&format!(" ({})", branch));
    }
    if !tty_str.is_empty() {
        display_label.push_str(&format!(" [{}]", tty_str));
    }

    let mut orca_terminal_id = env::var("ORCA_TERMINAL_HANDLE")
        .or_else(|_| env::var("ORCA_TERMINAL_ID"))
        .unwrap_or_default()
        .trim()
        .to_string();
    let mut orca_pane_key = env::var("ORCA_PANE_KEY")
        .or_else(|_| env::var("ORCA_AGENT_PANE"))
        .unwrap_or_default()
        .trim()
        .to_string();
    let mut orca_worktree_id = env::var("ORCA_WORKTREE_ID")
        .or_else(|_| env::var("ORCA_WORKSPACE_ID"))
        .unwrap_or_default()
        .trim()
        .to_string();
    let mut tmux_pane = env::var("TMUX_PANE")
        .unwrap_or_default()
        .trim()
        .to_string();

    // When `memnos mcp` is spawned by an IDE/CLI harness that scrubs child env vars,
    // recover Orca / tmux terminal identifiers from the ancestor harness process on Unix.
    #[cfg(unix)]
    if (orca_terminal_id.is_empty() && tmux_pane.is_empty()) && agent_pid > 1 {
        if let Ok(out) = Command::new("ps")
            .args(["eww", "-o", "command=", "-p", &agent_pid.to_string()])
            .stderr(Stdio::null())
            .output()
        {
            let cmdline = String::from_utf8_lossy(&out.stdout);
            for tok in cmdline.split_whitespace() {
                if orca_terminal_id.is_empty() {
                    if let Some(v) = tok
                        .strip_prefix("ORCA_TERMINAL_HANDLE=")
                        .or_else(|| tok.strip_prefix("ORCA_TERMINAL_ID="))
                    {
                        if !v.is_empty() {
                            orca_terminal_id = v.to_string();
                        }
                    }
                }
                if orca_pane_key.is_empty() {
                    if let Some(v) = tok
                        .strip_prefix("ORCA_PANE_KEY=")
                        .or_else(|| tok.strip_prefix("ORCA_AGENT_PANE="))
                    {
                        if !v.is_empty() {
                            orca_pane_key = v.to_string();
                        }
                    }
                }
                if orca_worktree_id.is_empty() {
                    if let Some(v) = tok
                        .strip_prefix("ORCA_WORKTREE_ID=")
                        .or_else(|| tok.strip_prefix("ORCA_WORKSPACE_ID="))
                    {
                        if !v.is_empty() {
                            orca_worktree_id = v.to_string();
                        }
                    }
                }
                if tmux_pane.is_empty() {
                    if let Some(v) = tok.strip_prefix("TMUX_PANE=") {
                        if !v.is_empty() {
                            tmux_pane = v.to_string();
                        }
                    }
                }
            }
        }
    }

    let tty_slug = sanitize_slug(&tty_str.replace("ttys", "s").replace("pts", "p"), 12);
    TerminalSessionInfo {
        name,
        fallback_suffix: if tty_slug.is_empty() {
            format!("p{}", agent_pid % 10000)
        } else {
            tty_slug
        },
        display_label,
        suggested_topic: infer_topic_channel(&real_cwd, ""),
        harness: if harness.is_empty() { "cli".to_string() } else { harness },
        pid: agent_pid,
        tty: tty_str,
        machine_id: machine_id(),
        host: hostname(),
        cwd: real_cwd.to_string_lossy().to_string(),
        auto_named,
        orca_terminal_id,
        orca_pane_key,
        orca_worktree_id,
        tmux_pane,
    }
}
