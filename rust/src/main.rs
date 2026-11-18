//! memnos — Single-binary Rust engine for governed AI memory & multi-agent coordination.
//!
//! Works natively across macOS (`aarch64-apple-darwin`, `x86_64-apple-darwin`),
//! Linux (`x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`), and
//! Windows (`x86_64-pc-windows-msvc`, `aarch64-pc-windows-msvc`).
//! Publishable to PyPI via `maturin` (`bindings = "bin"`), Cargo, and Homebrew.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Exact Python `fnmatch.fnmatch` semantics (`*` matches any char including `/`, `?` matches any 1 char).
pub fn fnmatch(pat: &str, text: &str) -> bool {
    let p: Vec<char> = pat.chars().collect();
    let t: Vec<char> = text.chars().collect();
    let (mut pi, mut ti) = (0usize, 0usize);
    let (mut star_pi, mut star_ti) = (None::<usize>, 0usize);
    while ti < t.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star_pi = Some(pi);
            pi += 1;
            star_ti = ti;
        } else if let Some(sp) = star_pi {
            pi = sp + 1;
            star_ti += 1;
            ti = star_ti;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

#[derive(Debug, Default, Deserialize, Serialize, Clone)]
struct Config {
    #[serde(default)]
    dsn: Option<String>,
    #[serde(default)]
    port: Option<u16>,
    #[serde(default)]
    admin_token: Option<String>,
    #[serde(default)]
    agent_tokens: HashMap<String, String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalSessionInfo {
    name: String,
    display_label: String,
    suggested_topic: String,
    harness: String,
    pid: u32,
    tty: String,
    machine_id: String,
    host: String,
    cwd: String,
    auto_named: bool,
}

fn home_dir() -> PathBuf {
    if let Ok(h) = env::var("HOME") {
        if !h.is_empty() {
            return PathBuf::from(h);
        }
    }
    if let Ok(up) = env::var("USERPROFILE") {
        if !up.is_empty() {
            return PathBuf::from(up);
        }
    }
    PathBuf::from(".")
}

fn config_dir() -> PathBuf {
    home_dir().join(".memnos")
}

fn load_config() -> Config {
    let p = config_dir().join("config.json");
    if let Ok(raw) = fs::read_to_string(p) {
        if let Ok(cfg) = serde_json::from_str::<Config>(&raw) {
            return cfg;
        }
    }
    Config::default()
}

fn server_url(cfg: &Config) -> String {
    if let Ok(u) = env::var("MEMNOS_URL") {
        if !u.trim().is_empty() {
            return u.trim_end_matches('/').to_string();
        }
    }
    let port = cfg.port.unwrap_or(8900);
    format!("http://127.0.0.1:{}", port)
}

fn session_token(cfg: &Config) -> String {
    if let Ok(t) = env::var("MEMNOS_TOKEN") {
        if !t.trim().is_empty() {
            return t.trim().to_string();
        }
    }
    if let Some(t) = cfg.agent_tokens.get("claude-code") {
        if !t.is_empty() {
            return t.clone();
        }
    }
    cfg.admin_token.clone().unwrap_or_default()
}

fn sanitize_slug(s: &str, max_len: usize) -> String {
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

fn hostname() -> String {
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

fn machine_id() -> String {
    let s = sanitize_slug(&hostname(), 64);
    if s.is_empty() {
        "unknown".to_string()
    } else {
        s
    }
}

/// Cross-platform OS process liveness check (macOS, Linux, Windows).
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
            fn OpenProcess(dwDesiredAccess: u32, bInheritHandle: i32, dwProcessId: u32) -> *mut std::ffi::c_void;
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

fn git_root(cwd: &Path) -> Option<PathBuf> {
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

fn git_branch(cwd: &Path) -> String {
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

pub fn infer_topic_channel(cwd: &Path, topic_or_task: &str) -> String {
    let raw = topic_or_task.trim().trim_start_matches('#');
    if !raw.is_empty() {
        let slug = sanitize_slug(raw, 48);
        if !slug.is_empty() {
            return format!("#{}", slug);
        }
    }
    let root = git_root(cwd).unwrap_or_else(|| cwd.to_path_buf());
    let base = root
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("general");
    let repo_slug = sanitize_slug(base, 24);
    let repo_slug = if repo_slug.is_empty() {
        "general".to_string()
    } else {
        repo_slug
    };
    let branch = git_branch(cwd);
    let low_b = branch.to_ascii_lowercase();
    if !low_b.is_empty()
        && !matches!(
            low_b.as_str(),
            "main" | "master" | "develop" | "dev" | "head" | "trunk"
        )
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

pub fn resolve_namespace(cwd: &Path, payload: Option<&Value>) -> (String, String) {
    if let Some(d) = payload {
        if let Some(ns) = d.get("namespace").and_then(|v| v.as_str()) {
            let trimmed = ns.trim();
            if !trimmed.is_empty() && trimmed != "auto" {
                return (trimmed.to_string(), "explicit".to_string());
            }
        }
    }
    let cache_path = config_dir().join("bindings_cache.json");
    if let Ok(raw) = fs::read_to_string(cache_path) {
        if let Ok(val) = serde_json::from_str::<Value>(&raw) {
            if let Some(bindings) = val.get("bindings").and_then(|b| b.as_array()) {
                let cwd_str = cwd.to_string_lossy().to_string();
                let mid = machine_id();
                for b in bindings {
                    let kt = b.get("key_type").and_then(|v| v.as_str()).unwrap_or("");
                    let key = b.get("key").and_then(|v| v.as_str()).unwrap_or("");
                    let ns = b.get("namespace").and_then(|v| v.as_str()).unwrap_or("");
                    let hid = b.get("host_id").and_then(|v| v.as_str()).unwrap_or("");
                    if kt == "host_path" && hid == mid && (cwd_str == key || cwd_str.starts_with(&format!("{}/", key))) {
                        return (ns.to_string(), "binding".to_string());
                    }
                }
            }
        }
    }
    if let Ok(env_ns) = env::var("MEMNOS_NS") {
        let trimmed = env_ns.trim();
        if !trimmed.is_empty() && trimmed != "auto" {
            return (trimmed.to_string(), "env".to_string());
        }
    }
    let root = git_root(cwd).unwrap_or_else(|| cwd.to_path_buf());
    let base = root
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("default");
    let slug = sanitize_slug(base, 40);
    (
        format!("proj:{}", if slug.is_empty() { "default" } else { &slug }),
        "default".to_string(),
    )
}

pub fn detect_terminal_session(cwd: &Path) -> TerminalSessionInfo {
    let real_cwd = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
    let mut explicit_name = env::var("MEMNOS_SESSION")
        .unwrap_or_default()
        .trim()
        .trim_start_matches('@')
        .to_string();

    if explicit_name.is_empty() {
        let active_path = config_dir().join("active_sessions.json");
        if let Ok(raw) = fs::read_to_string(active_path) {
            if let Ok(map) = serde_json::from_str::<Value>(&raw) {
                if let Some(n) = map
                    .get(real_cwd.to_string_lossy().as_ref())
                    .and_then(|v| v.get("name"))
                    .and_then(|v| v.as_str())
                {
                    explicit_name = n.to_string();
                }
            }
        }
    }

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
            let parts: Vec<&str> = line.splitn(5, char::is_whitespace).filter(|s| !s.is_empty()).collect();
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

    TerminalSessionInfo {
        name,
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

#[cfg(unix)]
unsafe fn libc_getppid() -> i32 {
    extern "C" {
        fn getppid() -> i32;
    }
    getppid()
}

fn is_antigravity_hook(data: &Value) -> bool {
    const KEYS: &[&str] = &[
        "conversationId",
        "workspacePaths",
        "lastUserInput",
        "transcriptPath",
        "toolCall",
        "invocationNum",
        "terminationReason",
    ];
    KEYS.iter().any(|k| data.get(*k).is_some())
}

fn tool_match_subject(tool_name: &str, tool_input: &Value) -> String {
    let inner = match tool_name {
        "Bash" => tool_input.get("command").and_then(|v| v.as_str()).unwrap_or(""),
        "run_command" => tool_input.get("CommandLine").and_then(|v| v.as_str()).unwrap_or(""),
        "read_file" | "write_file" | "edit_file" => {
            tool_input.get("Path").and_then(|v| v.as_str()).unwrap_or("")
        }
        _ => tool_input.get("file_path").and_then(|v| v.as_str()).unwrap_or(""),
    };
    if inner.is_empty() {
        tool_name.to_string()
    } else {
        format!("{}({})", tool_name, inner)
    }
}

/// Native `<1ms` PreToolUse enforcement hook (`memnos hook enforce`).
pub fn evaluate_enforce_rules(ns: &str, tool_name: &str, tool_input: &Value) -> Option<(i64, String, String, String)> {
    let safe_ns: String = ns
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-') { c } else { '_' })
        .collect();
    let cache_path = config_dir().join("enforce_cache").join(format!("{}.json", safe_ns));
    let raw = fs::read_to_string(cache_path).ok()?;
    let cache: Value = serde_json::from_str(&raw).ok()?;
    let rules = cache.get("rules")?.as_array()?;
    if rules.is_empty() {
        return None;
    }
    let subject = tool_match_subject(tool_name, tool_input);
    let mut subjects = vec![subject, tool_name.to_string()];
    if tool_name == "run_command" {
        if let Some(cmd) = tool_input.get("CommandLine").and_then(|v| v.as_str()) {
            subjects.push(format!("Bash({})", cmd));
            subjects.push("Bash".to_string());
        }
    }
    let mut matched = Vec::new();
    for r in rules {
        let pat = r.get("tool_matcher").and_then(|v| v.as_str()).unwrap_or("");
        if !pat.is_empty() && subjects.iter().any(|s| fnmatch(pat, s)) {
            matched.push(r);
        }
    }
    if matched.is_empty() {
        return None;
    }
    let want_level = if matched
        .iter()
        .any(|r| r.get("enforce_level").and_then(|v| v.as_str()) == Some("block"))
    {
        "block"
    } else {
        "ask"
    };
    let fired = matched
        .into_iter()
        .find(|r| r.get("enforce_level").and_then(|v| v.as_str()) == Some(want_level))?;
    let id = fired.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
    let rule_text = fired.get("rule_text").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let src_ns = fired.get("namespace").and_then(|v| v.as_str()).unwrap_or(ns).to_string();
    Some((id, want_level.to_string(), rule_text, src_ns))
}

fn post_json(url: &str, token: &str, body: &Value, timeout_secs: u64) -> Result<Value, String> {
    let mut req = ureq::post(url)
        .timeout(Duration::from_secs(timeout_secs))
        .set("Content-Type", "application/json");
    if !token.is_empty() {
        req = req.set("Authorization", &format!("Bearer {}", token));
    }
    match req.send_json(body.clone()) {
        Ok(resp) => resp.into_json::<Value>().map_err(|e| e.to_string()),
        Err(ureq::Error::Status(code, resp)) => {
            let txt = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {}: {}", code, txt))
        }
        Err(e) => Err(e.to_string()),
    }
}

fn format_feature_showcase(ns: &str, sess_name: &str, topic_hint: &str) -> String {
    let mut lines = vec![
        "╭── ✨ MEMNOS (Rust Single-Binary Engine) — Autonomous Memory & Agent Mesh ────╮".to_string(),
        format!("│ Active Namespace: {:<24} Session: {:<23} │", ns, sess_name),
        "├──────────────────────────────────────────────────────────────────────────────┤".to_string(),
        "│ • /memnos registry <topic>   Join/create a cross-namespace topic channel     │".to_string(),
        "│ • /memnos !<rule>            Pin a standing rule into every future session   │".to_string(),
        "│ • /memnos <query>            Recall decisions, architecture & fixes          │".to_string(),
        "│ • memnos watch               Hands-free LISTEN/NOTIFY wakeup for idle agents │".to_string(),
        "│ • memnos doctor --fix        Self-heal knowledge graph & check health        │".to_string(),
        "│ • memnos explain             Audit what memories & rules were just injected  │".to_string(),
        "│ • memnos tips                Show all features & rotating workflow tips      │".to_string(),
    ];
    if !topic_hint.is_empty() {
        lines.push(format!(
            "│ 💡 Suggested topic for this repo: /memnos registry {:<26} │",
            topic_hint
        ));
    }
    lines.push("╰──────────────────────────────────────────────────────────────────────────────╯".to_string());
    lines.join("\n")
}

fn run_hook_enforce() -> i32 {
    let mut input = String::new();
    if io::stdin().read_to_string(&mut input).is_err() || input.trim().is_empty() {
        return 0;
    }
    let Ok(data) = serde_json::from_str::<Value>(&input) else {
        return 0;
    };
    let agy = is_antigravity_hook(&data);
    let tc = data.get("toolCall");
    let tool_name = tc
        .and_then(|t| t.get("name"))
        .or_else(|| data.get("tool_name"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let empty_obj = json!({});
    let tool_input = tc
        .and_then(|t| t.get("args"))
        .or_else(|| data.get("tool_input"))
        .unwrap_or(&empty_obj);
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let (enforce_ns, _) = resolve_namespace(&cwd, None);

    if let Some((id, level, rule_text, src_ns)) = evaluate_enforce_rules(&enforce_ns, tool_name, tool_input) {
        let decision = if level == "block" { "deny" } else { "ask" };
        let source = if src_ns != enforce_ns {
            format!(" [from {}]", src_ns)
        } else {
            String::new()
        };
        let reason = format!("memnos constraint (id={}){}: {}", id, source, rule_text);
        if agy {
            println!("{}", json!({"decision": decision, "reason": reason}));
        } else {
            println!(
                "{}",
                json!({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision,
                        "permissionDecisionReason": reason
                    }
                })
            );
        }
    } else if agy {
        println!("{}", json!({"decision": "allow"}));
    }
    0
}

fn run_registry(args: &[String], cfg: &Config) -> i32 {
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let (ns, _) = resolve_namespace(&cwd, None);
    let url = server_url(cfg);
    let tok = session_token(cfg);
    let info = detect_terminal_session(&cwd);

    let mut topic_parts = Vec::new();
    let mut task = String::new();
    let mut session_override = String::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--task" if i + 1 < args.len() => {
                task = args[i + 1].clone();
                i += 2;
            }
            "--session" if i + 1 < args.len() => {
                session_override = args[i + 1].clone();
                i += 2;
            }
            "--namespace" | "-n" | "--token" | "-d" | "--description" if i + 1 < args.len() => {
                i += 2;
            }
            "--from-beginning" => {
                i += 1;
            }
            s if !s.starts_with('-') => {
                topic_parts.push(s.to_string());
                i += 1;
            }
            _ => i += 1,
        }
    }
    let sess = if !session_override.is_empty() {
        session_override
    } else {
        info.name.clone()
    };
    let topic = topic_parts.join(" ").trim().to_string();
    if topic.is_empty() {
        let _ = post_json(
            &format!("{}/session/register", url),
            &tok,
            &json!({
                "namespace": ns,
                "name": sess,
                "harness": info.harness,
                "host": info.host,
                "cwd": info.cwd,
                "machine_id": info.machine_id,
                "pid": info.pid,
                "tty": info.tty,
                "ttl_seconds": 1800,
                "metadata": {
                    "auto_named": info.auto_named,
                    "display_label": info.display_label,
                    "suggested_topic": info.suggested_topic,
                }
            }),
            3,
        );
        match post_json(&format!("{}/channel/list", url), &tok, &json!({"namespace": ns}), 4) {
            Ok(cl) => {
                println!(
                    "=== memnos registry [Rust] (namespace={}, this session={} [{}]) ===",
                    ns, sess, info.display_label
                );
                println!("\nActive Terminal Sessions:");
                if let Some(sessions) = cl.get("active_sessions").and_then(|v| v.as_array()) {
                    for s in sessions {
                        let name = s.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                        let same = s.get("same_laptop").and_then(|v| v.as_bool()).unwrap_or(false);
                        let loc = if same {
                            "same-laptop".to_string()
                        } else {
                            format!("remote:{}", s.get("host").and_then(|v| v.as_str()).unwrap_or("?"))
                        };
                        let pid = s.get("pid").and_then(|v| v.as_u64()).unwrap_or(0);
                        let alive = s.get("pid_alive").and_then(|v| v.as_bool()).unwrap_or(true);
                        let tty = s.get("tty").and_then(|v| v.as_str()).unwrap_or("");
                        let unread = s.get("unread").and_then(|v| v.as_u64()).unwrap_or(0);
                        println!(
                            "  • {:<26} [{} pid={}({}) {}] unread={}",
                            name,
                            loc,
                            pid,
                            if alive { "alive" } else { "dead" },
                            tty,
                            unread
                        );
                    }
                }
                println!(
                    "\n💡 Suggested topic for this workspace: /memnos registry {}",
                    info.suggested_topic
                );
                0
            }
            Err(e) => {
                eprintln!("memnos registry error: {}", e);
                1
            }
        }
    } else {
        match post_json(
            &format!("{}/channel/registry", url),
            &tok,
            &json!({
                "namespace": ns,
                "topic": topic,
                "session_name": sess,
                "task": task,
                "harness": info.harness,
                "host": info.host,
                "cwd": info.cwd,
                "machine_id": info.machine_id,
                "pid": info.pid,
                "tty": info.tty,
            }),
            5,
        ) {
            Ok(out) => {
                let ch = out.get("channel").and_then(|v| v.as_str()).unwrap_or(&topic);
                let existed = out.get("existed").and_then(|v| v.as_bool()).unwrap_or(false);
                println!(
                    "✓ {} {} {} (shared across permitted namespaces)",
                    sess,
                    if existed {
                        "subscribed to existing global topic"
                    } else {
                        "created & subscribed to new global topic"
                    },
                    ch
                );
                0
            }
            Err(e) => {
                eprintln!("memnos registry error: {}", e);
                1
            }
        }
    }
}

fn run_watch(args: &[String], cfg: &Config) -> i32 {
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let (ns, _) = resolve_namespace(&cwd, None);
    let url = server_url(cfg);
    let tok = session_token(cfg);
    let info = detect_terminal_session(&cwd);

    let mut timeout_secs: u64 = 300;
    let mut channel = String::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--timeout" if i + 1 < args.len() => {
                timeout_secs = args[i + 1].parse().unwrap_or(300);
                i += 2;
            }
            "--channel" if i + 1 < args.len() => {
                channel = args[i + 1].clone();
                i += 2;
            }
            _ => i += 1,
        }
    }
    let _ = post_json(
        &format!("{}/session/register", url),
        &tok,
        &json!({
            "namespace": ns,
            "name": info.name,
            "harness": info.harness,
            "host": info.host,
            "cwd": info.cwd,
            "machine_id": info.machine_id,
            "pid": info.pid,
            "tty": info.tty,
            "run_state": "idle",
            "ttl_seconds": 300,
            "metadata": {"auto_named": info.auto_named, "display_label": info.display_label}
        }),
        3,
    );
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    while Instant::now() < deadline {
        let left = deadline.saturating_duration_since(Instant::now()).as_secs().clamp(1, 60);
        if let Ok(out) = post_json(
            &format!("{}/channel/wait", url),
            &tok,
            &json!({
                "namespace": ns,
                "session_name": info.name,
                "channel": channel,
                "timeout_seconds": left
            }),
            left + 15,
        ) {
            let msgs = out.get("messages").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let inb = out.get("inbox_items").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            if !msgs.is_empty() || !inb.is_empty() {
                for m in msgs {
                    println!(
                        "[{}] {} ({}, #{}): {}",
                        m.get("channel").and_then(|v| v.as_str()).unwrap_or(""),
                        m.get("sender_session").and_then(|v| v.as_str()).unwrap_or(""),
                        m.get("intent").and_then(|v| v.as_str()).unwrap_or("message"),
                        m.get("id").and_then(|v| v.as_i64()).unwrap_or(0),
                        m.get("content").and_then(|v| v.as_str()).unwrap_or("")
                    );
                }
                for item in inb {
                    println!(
                        "#{} [{}] from {}: {}",
                        item.get("id").and_then(|v| v.as_i64()).unwrap_or(0),
                        item.get("priority").and_then(|v| v.as_str()).unwrap_or("normal"),
                        item.get("from_session").and_then(|v| v.as_str()).unwrap_or(""),
                        item.get("content").and_then(|v| v.as_str()).unwrap_or("")
                    );
                }
                return 0;
            }
        } else {
            break;
        }
    }
    println!("(watch timeout — no new messages)");
    0
}

fn delegate_to_python(args: &[String]) -> i32 {
    let exe_dir = env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let repo_cli = exe_dir
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.join("memnos_cli.py"))
        .filter(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("memnos_cli.py"));

    let py_candidates = [
        exe_dir
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.join(".venv/bin/python"))
            .filter(|p| p.exists()),
        Some(PathBuf::from("python3")),
        Some(PathBuf::from("python")),
    ];
    for cand in py_candidates.into_iter().flatten() {
        if repo_cli.exists() {
            if let Ok(status) = Command::new(&cand).arg(&repo_cli).args(args).status() {
                return status.code().unwrap_or(1);
            }
        }
    }
    eprintln!("memnos: could not locate Python runtime for cold subcommand {:?}", args.first());
    1
}

/// Start background zero-touch registration + 25s OS PID liveness heartbeat for `memnos mcp`.
fn start_mcp_lifecycle_thread(cfg: Config) {
    if env::var("MEMNOS_MCP_AUTO_REGISTER").unwrap_or_else(|_| "1".to_string()) == "0" {
        return;
    }
    thread::spawn(move || {
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let (ns, _) = resolve_namespace(&cwd, None);
        let url = server_url(&cfg);
        let tok = session_token(&cfg);
        let info = detect_terminal_session(&cwd);
        let mut sess_name = info.name.clone();
        if let Ok(resp) = post_json(
            &format!("{}/session/register", url),
            &tok,
            &json!({
                "namespace": ns,
                "name": sess_name,
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
            3,
        ) {
            if let Some(n) = resp.get("name").and_then(|v| v.as_str()) {
                sess_name = n.to_string();
            }
        }
        loop {
            thread::sleep(Duration::from_secs(25));
            if !is_local_pid_alive(info.pid) {
                if info.auto_named {
                    let _ = post_json(
                        &format!("{}/session/unregister", url),
                        &tok,
                        &json!({"namespace": ns, "name": sess_name}),
                        2,
                    );
                }
                return;
            }
            let _ = post_json(
                &format!("{}/session/heartbeat", url),
                &tok,
                &json!({
                    "namespace": ns,
                    "name": sess_name,
                    "machine_id": info.machine_id,
                    "pid": info.pid,
                    "tty": info.tty,
                    "ttl_seconds": 120,
                    "publish_status": false
                }),
                3,
            );
        }
    });
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    let cfg = load_config();

    if args.is_empty() || args[0] == "-V" || args[0] == "--version" {
        if !args.is_empty() {
            println!("memnos {} (rust single-binary engine)", VERSION);
            return;
        }
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let (ns, _) = resolve_namespace(&cwd, None);
        let info = detect_terminal_session(&cwd);
        println!("memnos {} (rust single-binary engine)\n", VERSION);
        println!(
            "{}",
            format_feature_showcase(&ns, &info.name, &info.suggested_topic)
        );
        return;
    }

    let code = match args[0].as_str() {
        "tips" => {
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let (ns, _) = resolve_namespace(&cwd, None);
            let info = detect_terminal_session(&cwd);
            println!(
                "{}",
                format_feature_showcase(&ns, &info.name, &info.suggested_topic)
            );
            0
        }
        "hook" if args.get(1).map(|s| s.as_str()) == Some("enforce") => run_hook_enforce(),
        "registry" => run_registry(&args[1..], &cfg),
        "watch" => run_watch(&args[1..], &cfg),
        "mcp" => {
            start_mcp_lifecycle_thread(cfg);
            delegate_to_python(&args)
        }
        _ => delegate_to_python(&args),
    };
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pid_liveness_current_process() {
        let my_pid = std::process::id();
        assert!(is_local_pid_alive(my_pid));
        assert!(!is_local_pid_alive(0));
        assert!(!is_local_pid_alive(999_999));
    }

    #[test]
    fn test_infer_topic_channel_from_phrase_and_repo() {
        let cwd = env::current_dir().unwrap();
        assert_eq!(
            infer_topic_channel(&cwd, "rust single binary architecture"),
            "#rust-single-binary-architecture"
        );
        assert_eq!(infer_topic_channel(&cwd, "#arch-review"), "#arch-review");
        let inferred = infer_topic_channel(&cwd, "");
        assert!(inferred.starts_with('#') && inferred.len() > 1);
    }

    #[test]
    fn test_detect_terminal_session_human_readable() {
        let cwd = env::current_dir().unwrap();
        let info = detect_terminal_session(&cwd);
        assert!(!info.name.is_empty());
        assert!(info.suggested_topic.starts_with('#'));
        assert!(!info.machine_id.is_empty());
    }

    #[test]
    fn test_enforce_subject_matching() {
        let subj = tool_match_subject("Bash", &json!({"command": "rm -rf /tmp/foo"}));
        assert_eq!(subj, "Bash(rm -rf /tmp/foo)");
        assert!(fnmatch("Bash(rm -rf*)", &subj));
        assert!(!fnmatch("Bash(git status*)", &subj));
    }
}
