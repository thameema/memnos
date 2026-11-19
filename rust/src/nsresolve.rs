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
    let Ok(raw) = fs::read_to_string(transcript_path) else {
        return String::new();
    };
    let mut title = String::new();
    for (i, line) in raw.lines().enumerate() {
        if i > 64 {
            break;
        }
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
                title = t.to_string();
            }
        }
    }
    if title.is_empty() {
        String::new()
    } else {
        slugify_session_title(&title)
    }
}

pub fn save_active_session(name: &str, cwd: &Path, namespace: &str) {
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let cwd_key = real_cwd.to_string_lossy().to_string();
    let (tty, harness, _) = parent_tty_and_harness();
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
        map.insert(format!("{}::{}", cwd_key, tty), entry.clone());
    }
    if !harness.is_empty() {
        map.insert(format!("{}::harness:{}", cwd_key, harness), entry.clone());
    } else {
        map.entry(cwd_key).or_insert(entry);
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
    }
}
