//! Cross-OS Agent Harness Wiring & Login Autostart (`agent_setup.rs`).
//!
//! Supports `claude-code`, `antigravity` (`agy`), `cursor`, `codex`, `windsurf`,
//! `gemini-cli`, `claude-desktop`, `zed`, `omnigent`, `hermes`, `openclaw`, and `all`,
//! plus cross-platform `autostart` (`launchd` on macOS, `systemd --user` on Linux,
//! `schtasks` on Windows).

use crate::config::{config_dir as memnos_dir, home_dir, server_url, session_token, Config};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const AGENT_MD: &str = r#"## memnos — long-term memory (MCP tools)
memnos gives you persistent, governed memory across sessions (namespace-scoped). Call its
MCP tools explicitly (this agent has no auto-inject hooks):
- `recall` / `recall_wide` — fetch relevant past decisions, facts, people, and project state
  before answering (recall_wide searches every namespace your key may read).
- `remember` — save a durable fact/decision worth keeping.
- **Staleness check:** before answering from a local note on a key project detail, call
  `reconcile_claim(statement, subject, predicate)`. If it returns `stale`, tell the user their
  local note is out of date and give the newer memnos value + its date.
- **Session identity badge:** if your harness does not already render a `[MEMNOS @...]` status line
  or banner, surface `[MEMNOS @<handle> · #<channels>]` on the first turn and whenever subscribed
  `#channels` change (never include harness vendor names in the badge).
"#;

const AGY_SKILL_MD: &str = r##"---
name: memnos
description: >-
  Use this skill when the user invokes /memnos or asks to register a topic channel,
  split a PRD into a multi-harness task plan, coordinate with peer Claude Code / Antigravity /
  Codex / Gemini sessions, pin constraints, audit recall, or run memnos doctor/tips/watch.
---

# MEMNOS — Long-Term Memory & Multi-Agent Mesh (Antigravity / `agy`)

Use either the `memnos` MCP tools (`recall`, `recall_wide`, `remember`, `corpus_ingest`, `channel_registry`, `channel_publish`, `channel_poll`, `channel_ack`, `inbox_send`, `inbox_list`, `inbox_retract`, `lease_acquire`, `lease_release`) or the `memnos` CLI:

- **Show this session's identity & subscribed channels:** `memnos id` (displays `[MEMNOS @<handle> · #<channels>]`)
- **Join or create a global cross-namespace topic channel:** `memnos registry <topic>` (or `channel_registry(topic=...)`)
- **List active terminal sessions (same-laptop vs remote) & topic channels:** `memnos registry`
- **Post a competing-consumer task (claimed by 1 listener, no duplicate work):** `memnos registry <topic> --task "..."`
- **PRD -> Multi-Harness Task Plan (`depends_on`, `preferred_harness`, `different_harness`):**
  1. Ingest the PRD into governed corpus memory: `corpus_ingest(corpus="payments-prd", content=...)` and pin non-negotiable rules via `remember(..., memory_type="constraint")`.
  2. Create/join a dedicated feature channel (e.g. `channel_registry(topic="#payments-prd")`).
  3. Publish atomic `claimable=true` tasks (`intent="task"`) with DAG & routing `metadata`:
     - `{"preferred_harness": "claude-code", "required_role": "backend"}` (auto-falls back if offline or quota-paused)
     - `{"depends_on": [101, 102]}` (stays gated until upstream task IDs are `channel_ack`ed `done`)
     - `{"depends_on": [103], "different_harness": true}` (cross-model code review — cannot be claimed by the authoring harness)
  4. Before editing shared files, acquire a lease (`lease_acquire(resource="src/billing.ts")`), check `corpus_check` against the PRD, then call `channel_ack(id=..., status="done", resolution="...")` and `lease_release`.
  5. Need background workers without opening terminals? Launch subscription-only headless workers from `MEMNOS.app` (**⚡ Launch Headless**) or `POST /workspace/api/spawner/launch` (auto-strips API keys for $0 API billing, auto-fails over across harnesses on quota limits, and auto-closes on exit).
- **Hands-free wakeups (no background polling loops):** Do NOT spawn `memnos channel wait` or `memnos watch` background tasks — `memnos wake-daemon` watches Postgres `LISTEN/NOTIFY` in the background and automatically wakes your session when idle. Subscribe, publish, and end your turn.
- **Pin a permanent project rule:** `memnos remember "<rule>" --type constraint`
- **Platform doctor & entity-graph cleanup:** `memnos doctor --fix`
- **Feature showcase & workflow tips:** `memnos tips`
"##;

pub const SUPPORTED_AGENTS: &[&str] = &[
    "claude-code",
    "antigravity",
    "cursor",
    "codex",
    "windsurf",
    "gemini-cli",
    "claude-desktop",
    "zed",
    "omnigent",
    "hermes",
    "openclaw",
];

pub fn canonical_agent_name(raw: &str) -> Option<&'static str> {
    match raw.trim().to_lowercase().as_str() {
        "claude" | "claude-code" | "cc" => Some("claude-code"),
        "agy" | "antigravity" => Some("antigravity"),
        "cursor" => Some("cursor"),
        "codex" => Some("codex"),
        "windsurf" => Some("windsurf"),
        "gemini" | "gemini-cli" => Some("gemini-cli"),
        "desktop" | "claude-desktop" => Some("claude-desktop"),
        "zed" => Some("zed"),
        "omnigent" => Some("omnigent"),
        "hermes" => Some("hermes"),
        "openclaw" => Some("openclaw"),
        "all" => Some("all"),
        _ => None,
    }
}

pub fn is_agent_target(raw: &str) -> bool {
    canonical_agent_name(raw).is_some()
}

fn memnos_exe_string() -> String {
    if let Ok(p) = env::current_exe() {
        let fname = p
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if fname.contains("desktop") {
            if let Some(dir) = p.parent() {
                #[cfg(windows)]
                let sib = dir.join("memnos.exe");
                #[cfg(not(windows))]
                let sib = dir.join("memnos");
                if sib.is_file() {
                    return sib.to_string_lossy().to_string();
                }
            }
            let local_cand = home_dir().join(".local").join("bin").join(if cfg!(windows) {
                "memnos.exe"
            } else {
                "memnos"
            });
            if local_cand.is_file() {
                return local_cand.to_string_lossy().to_string();
            }
            return "memnos".to_string();
        }
        return p.to_string_lossy().to_string();
    }
    "memnos".to_string()
}

fn make_hook_cmd(url: &str, token: &str, ns: &str, extra_kv: Option<(&str, &str)>, qexe: &str, sub: &str) -> String {
    #[cfg(windows)]
    {
        let mut parts = Vec::new();
        if let Some((k, v)) = extra_kv {
            parts.push(format!("set \"{}={}\"", k, v));
        }
        parts.push(format!("set \"MEMNOS_URL={}\"", url));
        parts.push(format!("set \"MEMNOS_TOKEN={}\"", token));
        parts.push(format!("set \"MEMNOS_NS={}\"", ns));
        parts.push(format!("{} {}", qexe, sub));
        parts.join(" && ")
    }
    #[cfg(not(windows))]
    {
        let extra = extra_kv
            .map(|(k, v)| format!("{}={} ", k, shell_quote(v)))
            .unwrap_or_default();
        format!(
            "{}MEMNOS_URL={} MEMNOS_TOKEN={} MEMNOS_NS={} {} {}",
            extra,
            shell_quote(url),
            shell_quote(token),
            shell_quote(ns),
            qexe,
            sub
        )
    }
}

fn shell_quote(s: &str) -> String {
    #[cfg(windows)]
    {
        if s.contains(' ') {
            format!("\"{}\"", s.replace('"', "\\\""))
        } else {
            s.to_string()
        }
    }
    #[cfg(not(windows))]
    {
        if s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '.' | '_' | '-' | ':'))
        {
            s.to_string()
        } else {
            format!("'{}'", s.replace('\'', "'\\''"))
        }
    }
}

fn backup_file(path: &Path) {
    if path.exists() {
        let bak = PathBuf::from(format!("{}.bak", path.display()));
        let _ = fs::copy(path, bak);
    }
}

fn read_json_obj(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| json!({}))
}

fn write_json_pretty(path: &Path, val: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    backup_file(path);
    let raw = serde_json::to_string_pretty(val).map_err(|e| e.to_string())?;
    fs::write(path, format!("{}\n", raw)).map_err(|e| e.to_string())
}

fn default_user_ns() -> String {
    let u = env::var("USER")
        .or_else(|_| env::var("USERNAME"))
        .unwrap_or_else(|_| "me".to_string());
    let slug: String = u
        .to_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    format!("user:{}", slug.trim_matches('-'))
}

fn isolated_home_discovery() -> bool {
    matches!(
        env::var("MEMNOS_ISOLATED_HOME_DISCOVERY")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn has_bin_on_path(bin: &str) -> bool {
    let home = home_dir();
    let isolated = isolated_home_discovery();
    let mut search_dirs: Vec<PathBuf> = env::var_os("PATH")
        .map(|p| env::split_paths(&p).collect())
        .unwrap_or_default();
    search_dirs.push(home.join(".local").join("bin"));
    search_dirs.push(home.join(".cargo").join("bin"));
    search_dirs.push(home.join(".bun").join("bin"));
    search_dirs.push(home.join(".npm-global").join("bin"));
    search_dirs.push(home.join(".volta").join("bin"));
    search_dirs.push(home.join(".asdf").join("shims"));
    #[cfg(unix)]
    {
        if !isolated {
            search_dirs.push(PathBuf::from("/opt/homebrew/bin"));
            search_dirs.push(PathBuf::from("/usr/local/bin"));
        }
        if let Ok(entries) = fs::read_dir(home.join(".nvm").join("versions").join("node")) {
            for ent in entries.flatten() {
                search_dirs.push(ent.path().join("bin"));
            }
        }
    }
    #[cfg(windows)]
    if !isolated {
        if let Ok(ad) = env::var("APPDATA") {
            search_dirs.push(PathBuf::from(ad).join("npm"));
        }
        if let Ok(lad) = env::var("LOCALAPPDATA") {
            let lpath = PathBuf::from(lad);
            search_dirs.push(lpath.join("Programs").join(bin));
            search_dirs.push(lpath.join("Microsoft").join("WinGet").join("Links"));
        }
    }

    for dir in search_dirs {
        if dir.join(bin).is_file() {
            return true;
        }
        #[cfg(windows)]
        for ext in [".exe", ".cmd", ".bat"] {
            if dir.join(format!("{}{}", bin, ext)).is_file() {
                return true;
            }
        }
    }
    false
}

fn has_macos_app(app_name: &str) -> bool {
    #[cfg(target_os = "macos")]
    {
        let usr_app = home_dir().join("Applications").join(app_name);
        if isolated_home_discovery() {
            return usr_app.exists();
        }
        let sys_app = PathBuf::from("/Applications").join(app_name);
        sys_app.exists() || usr_app.exists()
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app_name;
        false
    }
}

fn merge_hook_entries(existing: Option<&Value>, new_entry: Value) -> Value {
    let mut kept: Vec<Value> = Vec::new();
    if let Some(arr) = existing.and_then(|v| v.as_array()) {
        for item in arr {
            let s = serde_json::to_string(item).unwrap_or_default();
            if !s.contains("memnos") {
                kept.push(item.clone());
            }
        }
    }
    kept.push(new_entry);
    Value::Array(kept)
}

fn install_statusline_shim(memnos_sl_cmd: &str) {
    let bin_dir = memnos_dir().join("bin");
    let _ = fs::create_dir_all(&bin_dir);
    let local_bin_dir = home_dir().join(".local").join("bin");
    let _ = fs::create_dir_all(&local_bin_dir);

    #[cfg(unix)]
    {
        let body = format!("#!/bin/sh\nexec /usr/bin/env {} \"$@\"\n", memnos_sl_cmd);
        use std::os::unix::fs::PermissionsExt;
        let shim_path = bin_dir.join("statusline");
        if fs::write(&shim_path, &body).is_ok() {
            let _ = fs::set_permissions(&shim_path, fs::Permissions::from_mode(0o755));
        }
        let local_shim = local_bin_dir.join("statusline");
        let can_write_local = !local_shim.exists()
            || fs::read_to_string(&local_shim)
                .map(|s| s.contains("memnos"))
                .unwrap_or(false);
        if can_write_local && fs::write(&local_shim, &body).is_ok() {
            let _ = fs::set_permissions(&local_shim, fs::Permissions::from_mode(0o755));
        }
    }
    #[cfg(windows)]
    {
        let body = format!("@echo off\r\n{}\r\n", memnos_sl_cmd);
        let shim_path = bin_dir.join("statusline.cmd");
        let _ = fs::write(&shim_path, &body);
        let local_shim = local_bin_dir.join("statusline.cmd");
        let can_write_local = !local_shim.exists()
            || fs::read_to_string(&local_shim)
                .map(|s| s.contains("memnos"))
                .unwrap_or(false);
        if can_write_local {
            let _ = fs::write(&local_shim, &body);
        }
    }
}

fn build_chained_statusline_value(
    existing_sl: &str,
    memnos_sl_cmd: &str,
    mux_name: &str,
    extra_fallback_script: Option<&Path>,
    include_agy_flags: bool,
) -> Value {
    install_statusline_shim(memnos_sl_cmd);

    #[cfg(unix)]
    {
        let other_sl = if !existing_sl.is_empty()
            && !existing_sl.contains("memnos")
            && !existing_sl.contains(mux_name)
        {
            Some(existing_sl.to_string())
        } else if let Some(fb) = extra_fallback_script.filter(|p| p.is_file()) {
            Some(format!("/bin/sh {}", shell_quote(&fb.to_string_lossy())))
        } else {
            None
        };

        let chosen_cmd = if let Some(prev_cmd) = other_sl {
            let bin_dir = memnos_dir().join("bin");
            let _ = fs::create_dir_all(&bin_dir);
            let mux_path = bin_dir.join(mux_name);
            let mux_script = format!(
                "#!/bin/sh\npayload=$(cat 2>/dev/null || true)\n(printf '%s' \"$payload\" | {} >/dev/null 2>&1 &)\nprintf '%s' \"$payload\" | {} 2>/dev/null || true\n",
                prev_cmd, memnos_sl_cmd
            );
            if fs::write(&mux_path, mux_script).is_ok() {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&mux_path, fs::Permissions::from_mode(0o755));
                shell_quote(&mux_path.to_string_lossy())
            } else {
                memnos_sl_cmd.to_string()
            }
        } else {
            memnos_sl_cmd.to_string()
        };

        if include_agy_flags {
            json!({
                "type": "command",
                "command": chosen_cmd,
                "enabled": true,
                "stack_with_default": true,
            })
        } else {
            json!({
                "type": "command",
                "command": chosen_cmd,
            })
        }
    }
    #[cfg(not(unix))]
    {
        let _ = (existing_sl, mux_name, extra_fallback_script);
        if include_agy_flags {
            json!({
                "type": "command",
                "command": memnos_sl_cmd,
                "enabled": true,
                "stack_with_default": true,
            })
        } else {
            json!({
                "type": "command",
                "command": memnos_sl_cmd,
            })
        }
    }
}

pub fn detect_installed_agents() -> Vec<&'static str> {
    let home = home_dir();
    let mut out = Vec::new();
    if home.join(".claude").exists() || home.join(".claude.json").exists() || has_bin_on_path("claude") {
        out.push("claude-code");
    }
    if home.join(".gemini").join("config").exists()
        || home.join(".gemini").join("antigravity-cli").exists()
        || home.join(".gemini").join("antigravity").exists()
        || has_macos_app("Antigravity.app")
        || has_bin_on_path("agy")
    {
        out.push("antigravity");
    }
    if home.join(".cursor").exists()
        || has_macos_app("Cursor.app")
        || has_bin_on_path("cursor")
        || has_bin_on_path("cursor-agent")
    {
        out.push("cursor");
    }
    if home.join(".codex").exists() || has_bin_on_path("codex") {
        out.push("codex");
    }
    if home.join(".codeium").join("windsurf").exists()
        || has_macos_app("Windsurf.app")
        || has_bin_on_path("windsurf")
    {
        out.push("windsurf");
    }
    if home.join(".gemini").join("settings.json").exists()
        || has_bin_on_path("gemini")
        || (home.join(".gemini").exists() && !out.contains(&"antigravity"))
    {
        out.push("gemini-cli");
    }
    if home.join(".config").join("zed").exists()
        || has_macos_app("Zed.app")
        || has_bin_on_path("zed")
    {
        out.push("zed");
    }
    #[cfg(target_os = "macos")]
    let claude_desktop_present = home
        .join("Library")
        .join("Application Support")
        .join("Claude")
        .exists()
        || has_macos_app("Claude.app");
    #[cfg(target_os = "windows")]
    let claude_desktop_present = env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| home.clone())
        .join("Claude")
        .exists();
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let claude_desktop_present = home.join(".config").join("Claude").exists();
    if claude_desktop_present {
        out.push("claude-desktop");
    }
    if home.join(".omnigent").exists() || has_bin_on_path("omnigent") {
        out.push("omnigent");
    }
    if home.join(".hermes").exists() || has_bin_on_path("hermes") {
        out.push("hermes");
    }
    if home.join(".openclaw").exists() || has_bin_on_path("openclaw") {
        out.push("openclaw");
    }
    out
}

pub fn inspect_installed_agent_wiring() -> (Vec<&'static str>, Vec<&'static str>) {
    let home = home_dir();
    let detected = detect_installed_agents();
    let mut unwired = Vec::new();
    for &name in &detected {
        let is_wired = match name {
            "claude-code" => {
                let cj = read_json_obj(&home.join(".claude.json"));
                let st = read_json_obj(&home.join(".claude").join("settings.json"));
                cj.get("mcpServers")
                    .and_then(|m| m.get("memnos"))
                    .is_some()
                    && st
                        .get("statusLine")
                        .and_then(|s| s.get("command"))
                        .and_then(|c| c.as_str())
                        .map(|s| s.contains("memnos") || s.contains("statusline-mux"))
                        .unwrap_or(false)
            }
            "antigravity" => {
                let mc = read_json_obj(&home.join(".gemini").join("config").join("mcp_config.json"));
                let st = read_json_obj(
                    &home
                        .join(".gemini")
                        .join("antigravity-cli")
                        .join("settings.json"),
                );
                mc.get("mcpServers")
                    .and_then(|m| m.get("memnos"))
                    .is_some()
                    && st
                        .get("statusLine")
                        .and_then(|s| s.get("command"))
                        .and_then(|c| c.as_str())
                        .map(|s| s.contains("memnos") || s.contains("statusline-mux"))
                        .unwrap_or(false)
            }
            "gemini-cli" => {
                let st = read_json_obj(&home.join(".gemini").join("settings.json"));
                st.get("mcpServers")
                    .and_then(|m| m.get("memnos"))
                    .is_some()
                    && st
                        .get("hooks")
                        .and_then(|h| h.get("BeforeAgent"))
                        .map(|v| serde_json::to_string(v).unwrap_or_default().contains("memnos"))
                        .unwrap_or(false)
            }
            "codex" => fs::read_to_string(home.join(".codex").join("config.toml"))
                .map(|s| s.contains("[mcp_servers.memnos]"))
                .unwrap_or(false),
            "cursor" => read_json_obj(&home.join(".cursor").join("mcp.json"))
                .get("mcpServers")
                .and_then(|m| m.get("memnos"))
                .is_some(),
            "windsurf" => read_json_obj(
                &home
                    .join(".codeium")
                    .join("windsurf")
                    .join("mcp_config.json"),
            )
            .get("mcpServers")
            .and_then(|m| m.get("memnos"))
            .is_some(),
            "zed" => read_json_obj(&home.join(".config").join("zed").join("settings.json"))
                .get("context_servers")
                .and_then(|m| m.get("memnos"))
                .is_some(),
            "claude-desktop" => {
                #[cfg(target_os = "macos")]
                let p = home
                    .join("Library")
                    .join("Application Support")
                    .join("Claude")
                    .join("claude_desktop_config.json");
                #[cfg(target_os = "windows")]
                let p = env::var("APPDATA")
                    .map(PathBuf::from)
                    .unwrap_or_else(|_| home.clone())
                    .join("Claude")
                    .join("claude_desktop_config.json");
                #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
                let p = home
                    .join(".config")
                    .join("Claude")
                    .join("claude_desktop_config.json");
                read_json_obj(&p)
                    .get("mcpServers")
                    .and_then(|m| m.get("memnos"))
                    .is_some()
            }
            _ => read_json_obj(&home.join(format!(".{}", name)).join("mcp.json"))
                .get("mcpServers")
                .and_then(|m| m.get("memnos"))
                .is_some(),
        };
        if !is_wired {
            unwired.push(name);
        }
    }
    (detected, unwired)
}

pub fn auto_sync_installed_agents(cfg: &Config) -> Vec<&'static str> {
    let skip = matches!(
        env::var("MEMNOS_CI")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    ) || matches!(
        env::var("CI")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    ) || matches!(
        env::var("MEMNOS_SKIP_AUTO_WIRE")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    );
    let token = session_token(cfg);
    if skip || token.trim().is_empty() {
        return Vec::new();
    }
    let url = server_url(cfg);
    let qexe = shell_quote(&memnos_exe_string());
    let sl_cmd = make_hook_cmd(&url, &token, "auto", None, &qexe, "hook statusline");
    install_statusline_shim(&sl_cmd);

    let detected = detect_installed_agents();
    let mut synced = Vec::new();
    for name in detected {
        if run_agent_setup_impl(name, Some("auto"), None, true, None, cfg, true).is_ok() {
            synced.push(name);
        }
    }
    synced
}

pub fn run_agent_setup(
    agent_raw: &str,
    namespace_opt: Option<&str>,
    transport_opt: Option<&str>,
    force: bool,
    agent_dir: Option<&str>,
    cfg: &Config,
) -> Result<(), String> {
    run_agent_setup_impl(
        agent_raw,
        namespace_opt,
        transport_opt,
        force,
        agent_dir,
        cfg,
        false,
    )
}

fn run_agent_setup_impl(
    agent_raw: &str,
    namespace_opt: Option<&str>,
    transport_opt: Option<&str>,
    force: bool,
    agent_dir: Option<&str>,
    cfg: &Config,
    quiet: bool,
) -> Result<(), String> {
    let Some(agent) = canonical_agent_name(agent_raw) else {
        return Err(format!(
            "unknown agent '{}' — choose: {}, all",
            agent_raw,
            SUPPORTED_AGENTS.join(", ")
        ));
    };

    if agent == "all" {
        let url = server_url(cfg);
        let token = session_token(cfg);
        let qexe = shell_quote(&memnos_exe_string());
        let sl_cmd = make_hook_cmd(
            &url,
            &token,
            namespace_opt.unwrap_or("auto"),
            None,
            &qexe,
            "hook statusline",
        );
        install_statusline_shim(&sl_cmd);

        let detected = detect_installed_agents();
        if detected.is_empty() {
            if !quiet {
                println!("[memnos] No supported agents detected on this machine.");
                println!("         Supported: {}", SUPPORTED_AGENTS.join(", "));
                println!("         To force-setup a specific agent: memnos setup <name> --force");
            }
            return Ok(());
        }
        if !quiet {
            println!(
                "[memnos] Detected {} agent(s): {}\n",
                detected.len(),
                detected.join(", ")
            );
        }
        for name in detected {
            if !quiet {
                println!("── {} ──────────────────────────────", name);
            }
            if let Err(e) =
                run_agent_setup_impl(name, namespace_opt, None, force, agent_dir, cfg, quiet)
            {
                if !quiet {
                    println!("  warning: {} setup error: {}", name, e);
                }
            }
            if !quiet {
                println!();
            }
        }
        return Ok(());
    }

    let explicit_http = transport_opt == Some("http");
    let transport = match agent {
        // Antigravity defaults to stdio so MEMNOS_TOKEN passes via env and 25s PID heartbeats run
        "antigravity" => {
            if explicit_http {
                "http"
            } else {
                "stdio"
            }
        }
        "claude-code" | "omnigent" => transport_opt.unwrap_or("http"),
        _ => {
            if explicit_http {
                return Err(format!(
                    "--transport http is only supported for claude-code, omnigent, and antigravity (got '{}')",
                    agent
                ));
            }
            "stdio"
        }
    };

    let url = server_url(cfg);
    let token = session_token(cfg);
    let ns = namespace_opt
        .map(|s| s.to_string())
        .or_else(|| env::var("MEMNOS_NS").ok().filter(|s| !s.is_empty()))
        .unwrap_or_else(|| {
            if matches!(agent, "omnigent" | "hermes" | "openclaw") {
                format!("agent:{}", agent)
            } else if matches!(
                agent,
                "claude-code" | "antigravity" | "gemini-cli" | "cursor" | "codex"
            ) {
                "auto".to_string()
            } else {
                default_user_ns()
            }
        });

    let exe = memnos_exe_string();
    let home = home_dir();
    let qexe_top = shell_quote(&exe);
    install_statusline_shim(&make_hook_cmd(
        &url,
        &token,
        &ns,
        None,
        &qexe_top,
        "hook statusline",
    ));

    if agent == "claude-code" {
        let claude_dir = home.join(".claude");
        if !claude_dir.exists() && !home.join(".claude.json").exists() && !has_bin_on_path("claude") && !force {
            if !quiet {
                println!("Claude Code not detected (~/.claude missing). Re-run with --force.");
            }
            return Ok(());
        }
        fs::create_dir_all(&claude_dir).map_err(|e| e.to_string())?;

        // 1. ~/.claude.json MCP server
        let claude_json = home.join(".claude.json");
        let mut cj = read_json_obj(&claude_json);
        if !cj.get("mcpServers").map(|v| v.is_object()).unwrap_or(false) {
            cj["mcpServers"] = json!({});
        }
        cj["mcpServers"]["memnos"] = if transport == "http" {
            json!({
                "type": "http",
                "url": format!("{}/mcp", url),
                "headers": {
                    "Authorization": format!("Bearer {}", token),
                    "X-Memnos-Namespace": ns,
                }
            })
        } else {
            json!({
                "type": "stdio",
                "command": exe,
                "args": ["mcp"],
                "env": {
                    "MEMNOS_URL": url,
                    "MEMNOS_TOKEN": token,
                    "MEMNOS_NS": ns,
                }
            })
        };
        write_json_pretty(&claude_json, &cj)?;

        // 2. ~/.claude/settings.json lifecycle hooks + terminal-agnostic statusLine
        let settings_path = claude_dir.join("settings.json");
        let mut settings = read_json_obj(&settings_path);
        if !settings.get("hooks").map(|v| v.is_object()).unwrap_or(false) {
            settings["hooks"] = json!({});
        }
        let qexe = shell_quote(&exe);
        let mk_hook_entry = |which: &str, timeout: u64, with_matcher: bool| {
            let mut entry = json!({
                "hooks": [{
                    "type": "command",
                    "command": make_hook_cmd(&url, &token, &ns, None, &qexe, &format!("hook {}", which)),
                    "timeout": timeout
                }]
            });
            if with_matcher {
                entry["matcher"] = json!("*");
            }
            entry
        };
        settings["hooks"]["SessionStart"] = merge_hook_entries(
            settings["hooks"].get("SessionStart"),
            mk_hook_entry("status", 15, false),
        );
        settings["hooks"]["UserPromptSubmit"] = merge_hook_entries(
            settings["hooks"].get("UserPromptSubmit"),
            mk_hook_entry("recall", 15, false),
        );
        settings["hooks"]["Stop"] = merge_hook_entries(
            settings["hooks"].get("Stop"),
            mk_hook_entry("remember", 15, false),
        );
        settings["hooks"]["PreToolUse"] = merge_hook_entries(
            settings["hooks"].get("PreToolUse"),
            mk_hook_entry("enforce", 10, true),
        );

        let memnos_sl_cmd = make_hook_cmd(&url, &token, &ns, None, &qexe, "hook statusline");
        let existing_sl = settings
            .get("statusLine")
            .and_then(|v| v.get("command"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let orca_sl = home.join(".orca/agent-hooks/claude-statusline.sh");
        settings["statusLine"] = build_chained_statusline_value(
            &existing_sl,
            &memnos_sl_cmd,
            "claude-statusline-mux.sh",
            Some(&orca_sl),
            false,
        );
        write_json_pretty(&settings_path, &settings)?;

        // 3. ~/.claude/CLAUDE.md memory instructions
        let claude_md = claude_dir.join("CLAUDE.md");
        let cur_md = fs::read_to_string(&claude_md).unwrap_or_default();
        if !cur_md.contains("## memnos — long-term memory") {
            backup_file(&claude_md);
            let next_md = if cur_md.is_empty() {
                AGENT_MD.to_string()
            } else {
                format!("{}\n\n{}", cur_md.trim_end(), AGENT_MD)
            };
            let _ = fs::write(&claude_md, next_md);
        }

        if !quiet {
            println!(
                "[memnos] claude-code wired -> ~/.claude.json & ~/.claude/settings.json (MCP {}, hooks, statusLine, CLAUDE.md, ns={}).",
                transport, ns
            );
        }
        return Ok(());
    }

    if agent == "antigravity" {
        let cfg_dir = home.join(".gemini").join("config");
        if !cfg_dir.exists()
            && !home.join(".gemini").join("antigravity-cli").exists()
            && !home.join(".gemini").join("antigravity").exists()
            && !has_macos_app("Antigravity.app")
            && !has_bin_on_path("agy")
            && !force
        {
            if !quiet {
                println!("Antigravity not detected (no ~/.gemini/config). Re-run with --force.");
            }
            return Ok(());
        }
        fs::create_dir_all(&cfg_dir).map_err(|e| e.to_string())?;

        // 1. MCP config (~/.gemini/config/mcp_config.json)
        let mcp_path = cfg_dir.join("mcp_config.json");
        let mut d = read_json_obj(&mcp_path);
        if !d.get("mcpServers").map(|v| v.is_object()).unwrap_or(false) {
            d["mcpServers"] = json!({});
        }
        d["mcpServers"]["memnos"] = if transport == "http" {
            json!({
                "url": format!("{}/mcp", url),
                "headers": {
                    "Authorization": format!("Bearer {}", token),
                    "X-Memnos-Namespace": ns,
                }
            })
        } else {
            json!({
                "command": exe,
                "args": ["mcp"],
                "env": {
                    "MEMNOS_URL": url,
                    "MEMNOS_TOKEN": token,
                    "MEMNOS_NS": ns,
                }
            })
        };
        write_json_pretty(&mcp_path, &d)?;

        // Also mirror into ~/.gemini/antigravity/mcp_config.json if present
        let alt_mcp = home.join(".gemini").join("antigravity").join("mcp_config.json");
        if alt_mcp.parent().map(|p| p.exists()).unwrap_or(false) {
            let mut ad = read_json_obj(&alt_mcp);
            if !ad.get("mcpServers").map(|v| v.is_object()).unwrap_or(false) {
                ad["mcpServers"] = json!({});
            }
            ad["mcpServers"]["memnos"] = d["mcpServers"]["memnos"].clone();
            let _ = write_json_pretty(&alt_mcp, &ad);
        }

        // 2. Lifecycle hooks (~/.gemini/config/hooks.json)
        let hooks_path = cfg_dir.join("hooks.json");
        let mut h = read_json_obj(&hooks_path);
        let qexe = shell_quote(&exe);
        let hk = |which: &str, timeout: u64| {
            json!({
                "type": "command",
                "command": make_hook_cmd(&url, &token, &ns, None, &qexe, &format!("hook {}", which)),
                "timeout": timeout
            })
        };
        h["memnos"] = json!({
            "SessionStart": [hk("status", 15)],
            "PreInvocation": [hk("recall", 15)],
            "Stop": [hk("remember", 15)],
            "PreToolUse": [{"matcher": "*", "hooks": [hk("enforce", 10)]}]
        });
        write_json_pretty(&hooks_path, &h)?;

        // 3. GEMINI.md
        let gm_path = cfg_dir.join("GEMINI.md");
        let cur = fs::read_to_string(&gm_path).unwrap_or_default();
        if !cur.contains("## memnos — long-term memory") {
            backup_file(&gm_path);
            let next = if cur.is_empty() {
                AGENT_MD.to_string()
            } else {
                format!("{}\n\n{}", cur.trim_end(), AGENT_MD)
            };
            fs::write(&gm_path, next).map_err(|e| e.to_string())?;
        }

        // 4. Skill (~/.gemini/config/skills/memnos/SKILL.md)
        let sk_dir = cfg_dir.join("skills").join("memnos");
        fs::create_dir_all(&sk_dir).map_err(|e| e.to_string())?;
        fs::write(sk_dir.join("SKILL.md"), AGY_SKILL_MD).map_err(|e| e.to_string())?;

        // 5. Antigravity CLI persistent statusLine (~/.gemini/antigravity-cli/settings.json)
        let agy_cli_dir = home.join(".gemini").join("antigravity-cli");
        fs::create_dir_all(&agy_cli_dir).map_err(|e| e.to_string())?;
        let agy_settings_path = agy_cli_dir.join("settings.json");
        let mut agy_settings = read_json_obj(&agy_settings_path);
        let sl_cmd = make_hook_cmd(&url, &token, &ns, None, &qexe, "hook statusline");
        let existing_sl = agy_settings
            .get("statusLine")
            .and_then(|v| v.get("command"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        agy_settings["statusLine"] = build_chained_statusline_value(
            &existing_sl,
            &sl_cmd,
            "agy-statusline-mux.sh",
            None,
            true,
        );
        write_json_pretty(&agy_settings_path, &agy_settings)?;

        if !quiet {
            println!(
                "[memnos] antigravity wired -> {} (MCP {}, hooks, statusLine, GEMINI.md, skill; ns={}).",
                cfg_dir.display(),
                transport,
                ns
            );
        }
        return Ok(());
    }

    if agent == "codex" {
        let codex_dir = home.join(".codex");
        fs::create_dir_all(&codex_dir).map_err(|e| e.to_string())?;
        let toml_path = codex_dir.join("config.toml");
        let existing = fs::read_to_string(&toml_path).unwrap_or_default();
        if !existing.contains("[mcp_servers.memnos]") {
            backup_file(&toml_path);
            let block = format!(
                "\n[mcp_servers.memnos]\ncommand = {:?}\nargs = [\"mcp\"]\n\n[mcp_servers.memnos.env]\nMEMNOS_URL = {:?}\nMEMNOS_TOKEN = {:?}\nMEMNOS_NS = {:?}\n",
                exe, url, token, ns
            );
            fs::write(&toml_path, format!("{}{}", existing, block)).map_err(|e| e.to_string())?;
        }
        let agents_md = codex_dir.join("AGENTS.md");
        let cur = fs::read_to_string(&agents_md).unwrap_or_default();
        if !cur.contains("## memnos — long-term memory") {
            backup_file(&agents_md);
            fs::write(
                &agents_md,
                if cur.is_empty() {
                    AGENT_MD.to_string()
                } else {
                    format!("{}\n\n{}", cur.trim_end(), AGENT_MD)
                },
            )
            .map_err(|e| e.to_string())?;
        }
        let qexe = shell_quote(&exe);
        let codex_hooks_path = codex_dir.join("hooks.json");
        let mut ch = read_json_obj(&codex_hooks_path);
        if !ch.get("hooks").map(|v| v.is_object()).unwrap_or(false) {
            ch["hooks"] = json!({});
        }
        let cx_hook = |which: &str| {
            json!({
                "hooks": [{
                    "type": "command",
                    "command": make_hook_cmd(&url, &token, &ns, None, &qexe, &format!("hook {}", which)),
                    "timeout": 15
                }]
            })
        };
        ch["hooks"]["SessionStart"] =
            merge_hook_entries(ch["hooks"].get("SessionStart"), cx_hook("status"));
        ch["hooks"]["UserPromptSubmit"] =
            merge_hook_entries(ch["hooks"].get("UserPromptSubmit"), cx_hook("recall"));
        let _ = write_json_pretty(&codex_hooks_path, &ch);
        if !quiet {
            println!(
                "[memnos] codex wired -> {} & hooks.json (MCP server 'memnos', ns={}).",
                toml_path.display(),
                ns
            );
        }
        return Ok(());
    }

    // Standard JSON/YAML MCP configs
    let (target_path, top_key) = match agent {
        "cursor" => (home.join(".cursor").join("mcp.json"), "mcpServers"),
        "windsurf" => (
            home.join(".codeium")
                .join("windsurf")
                .join("mcp_config.json"),
            "mcpServers",
        ),
        "gemini-cli" => (home.join(".gemini").join("settings.json"), "mcpServers"),
        "zed" => (
            home.join(".config").join("zed").join("settings.json"),
            "context_servers",
        ),
        "claude-desktop" => {
            #[cfg(target_os = "macos")]
            let p = home
                .join("Library")
                .join("Application Support")
                .join("Claude")
                .join("claude_desktop_config.json");
            #[cfg(target_os = "windows")]
            let p = env::var("APPDATA")
                .map(PathBuf::from)
                .unwrap_or_else(|_| home.clone())
                .join("Claude")
                .join("claude_desktop_config.json");
            #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
            let p = home
                .join(".config")
                .join("Claude")
                .join("claude_desktop_config.json");
            (p, "mcpServers")
        }
        "omnigent" => {
            let p = agent_dir
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".omnigent").join("mcp.json"));
            (p, "mcpServers")
        }
        _ => (
            home.join(format!(".{}", agent)).join("mcp.json"),
            "mcpServers",
        ),
    };

    let mut doc = read_json_obj(&target_path);
    if !doc.get(top_key).map(|v| v.is_object()).unwrap_or(false) {
        doc[top_key] = json!({});
    }
    doc[top_key]["memnos"] = json!({
        "command": exe,
        "args": ["mcp"],
        "env": {
            "MEMNOS_URL": url,
            "MEMNOS_TOKEN": token,
            "MEMNOS_NS": ns,
        }
    });
    if agent == "gemini-cli" {
        let qexe = shell_quote(&exe);
        if !doc.get("hooks").map(|v| v.is_object()).unwrap_or(false) {
            doc["hooks"] = json!({});
        }
        let g_hook = |which: &str| {
            json!({
                "hooks": [{
                    "type": "command",
                    "command": make_hook_cmd(&url, &token, &ns, Some(("GEMINI_CLI", "1")), &qexe, &format!("hook {}", which)),
                    "timeout": 15000
                }]
            })
        };
        doc["hooks"]["SessionStart"] =
            merge_hook_entries(doc["hooks"].get("SessionStart"), g_hook("status"));
        doc["hooks"]["BeforeAgent"] =
            merge_hook_entries(doc["hooks"].get("BeforeAgent"), g_hook("recall"));
        doc["hooks"]["AfterAgent"] =
            merge_hook_entries(doc["hooks"].get("AfterAgent"), g_hook("remember"));

        // Also write ~/.gemini/GEMINI.md so Gemini CLI knows the MCP tools & session badge rule
        let gm_path = home.join(".gemini").join("GEMINI.md");
        let cur = fs::read_to_string(&gm_path).unwrap_or_default();
        if !cur.contains("## memnos — long-term memory") {
            backup_file(&gm_path);
            let next = if cur.is_empty() {
                AGENT_MD.to_string()
            } else {
                format!("{}\n\n{}", cur.trim_end(), AGENT_MD)
            };
            let _ = fs::write(&gm_path, next);
        }
    }
    write_json_pretty(&target_path, &doc)?;

    // If user typed shorthand `gemini` and Antigravity is also installed, wire Antigravity too
    if agent == "gemini-cli"
        && agent_raw.trim().eq_ignore_ascii_case("gemini")
        && (home.join(".gemini").join("config").exists()
            || home.join(".gemini").join("antigravity-cli").exists()
            || has_macos_app("Antigravity.app")
            || has_bin_on_path("agy"))
    {
        let _ = run_agent_setup_impl(
            "antigravity",
            namespace_opt,
            None,
            force,
            agent_dir,
            cfg,
            quiet,
        );
    }
    if agent == "cursor" {
        let qexe = shell_quote(&exe);
        let cur_hooks_path = home.join(".cursor").join("hooks.json");
        let mut cur_hooks = read_json_obj(&cur_hooks_path);
        cur_hooks["version"] = json!(1);
        if !cur_hooks.get("hooks").map(|v| v.is_object()).unwrap_or(false) {
            cur_hooks["hooks"] = json!({});
        }
        let cur_entry = |which: &str| {
            json!({
                "command": make_hook_cmd(&url, &token, &ns, None, &qexe, &format!("hook {}", which)),
                "timeout": 15
            })
        };
        cur_hooks["hooks"]["sessionStart"] =
            merge_hook_entries(cur_hooks["hooks"].get("sessionStart"), cur_entry("status"));
        cur_hooks["hooks"]["beforeSubmitPrompt"] =
            merge_hook_entries(cur_hooks["hooks"].get("beforeSubmitPrompt"), cur_entry("recall"));
        cur_hooks["hooks"]["stop"] =
            merge_hook_entries(cur_hooks["hooks"].get("stop"), cur_entry("remember"));
        let _ = write_json_pretty(&cur_hooks_path, &cur_hooks);
    }
    if !quiet {
        println!(
            "[memnos] {} wired -> {} (MCP server 'memnos', ns={}).",
            agent,
            target_path.display(),
            ns
        );
        let has_openai = env::var("OPENAI_API_KEY")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| cfg.openai.clone().filter(|s| !s.trim().is_empty()))
            .is_some();
        if !has_openai {
            let (local_model, _) = crate::rerank::default_local_embed_model_from_env();
            let (emb_spec, rerank_spec) = crate::rerank::specs_for_embed_model(&local_model);
            if !crate::rerank::is_model_cached(emb_spec)
                || !crate::rerank::is_model_cached(rerank_spec)
            {
                println!(
                    "[memnos] note: embeddings are not set up yet — run `memnos setup embeddings` to choose between your OpenAI key (no download) or running locally on this computer (~{} MB one-time download, private & free, `memnos models pull --yes`).",
                    emb_spec.approx_mb + rerank_spec.approx_mb
                );
            }
        }
    }
    Ok(())
}

pub fn run_autostart(remove: bool, include_proxy: bool) -> Result<(), String> {
    let exe = memnos_exe_string();
    let log_dir = memnos_dir();
    let _ = fs::create_dir_all(&log_dir);
    let server_log = log_dir.join("server.log");
    let proxy_log = log_dir.join("proxy.log");
    let wake_log = log_dir.join("wake-daemon.log");

    #[cfg(target_os = "macos")]
    {
        let agents_dir = home_dir().join("Library").join("LaunchAgents");
        fs::create_dir_all(&agents_dir).map_err(|e| e.to_string())?;
        let srv_plist = agents_dir.join("ai.memnos.server.plist");
        let prx_plist = agents_dir.join("ai.memnos.proxy.plist");
        let wake_plist = agents_dir.join("ai.memnos.wake-daemon.plist");

        if remove {
            for p in [&srv_plist, &prx_plist, &wake_plist] {
                if p.exists() {
                    let _ = Command::new("launchctl")
                        .args(["unload", &p.to_string_lossy()])
                        .status();
                    let _ = fs::remove_file(p);
                }
            }
            println!("[memnos] autostart launchd agents removed.");
            return Ok(());
        }

        let plist_xml = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.memnos.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>{}</string>
        <string>gateway</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{}</string>
    <key>StandardErrorPath</key>
    <string>{}</string>
</dict>
</plist>
"#,
            exe,
            server_log.display(),
            server_log.display()
        );
        fs::write(&srv_plist, plist_xml).map_err(|e| e.to_string())?;
        let _ = Command::new("launchctl")
            .args(["unload", &srv_plist.to_string_lossy()])
            .status();
        let _ = Command::new("launchctl")
            .args(["load", &srv_plist.to_string_lossy()])
            .status();
        println!(
            "[memnos] installed macOS launchd service -> {}",
            srv_plist.display()
        );

        let wake_xml = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.memnos.wake-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{}</string>
        <string>wake-daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{}</string>
    <key>StandardErrorPath</key>
    <string>{}</string>
</dict>
</plist>
"#,
            exe,
            wake_log.display(),
            wake_log.display()
        );
        fs::write(&wake_plist, wake_xml).map_err(|e| e.to_string())?;
        let _ = Command::new("launchctl")
            .args(["unload", &wake_plist.to_string_lossy()])
            .status();
        let _ = Command::new("launchctl")
            .args(["load", &wake_plist.to_string_lossy()])
            .status();
        println!(
            "[memnos] installed macOS launchd wake-daemon -> {}",
            wake_plist.display()
        );

        if include_proxy {
            let pxml = format!(
                r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.memnos.proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>{}</string>
        <string>proxy</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{}</string>
    <key>StandardErrorPath</key>
    <string>{}</string>
</dict>
</plist>
"#,
                exe,
                proxy_log.display(),
                proxy_log.display()
            );
            fs::write(&prx_plist, pxml).map_err(|e| e.to_string())?;
            let _ = Command::new("launchctl")
                .args(["unload", &prx_plist.to_string_lossy()])
                .status();
            let _ = Command::new("launchctl")
                .args(["load", &prx_plist.to_string_lossy()])
                .status();
            println!(
                "[memnos] installed macOS launchd proxy service -> {}",
                prx_plist.display()
            );
        }
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        let _ = (server_log, proxy_log, wake_log);
        if remove {
            let _ = Command::new("schtasks")
                .args(["/Delete", "/TN", "MemnosServer", "/F"])
                .status();
            let _ = Command::new("schtasks")
                .args(["/Delete", "/TN", "MemnosWakeDaemon", "/F"])
                .status();
            let _ = Command::new("schtasks")
                .args(["/Delete", "/TN", "MemnosProxy", "/F"])
                .status();
            println!("[memnos] removed Windows Task Scheduler autostart entries.");
            return Ok(());
        }
        let tr = format!("\"{}\" gateway", exe);
        let status = Command::new("schtasks")
            .args([
                "/Create",
                "/SC",
                "ONLOGON",
                "/TN",
                "MemnosServer",
                "/TR",
                &tr,
                "/F",
            ])
            .status()
            .map_err(|e| format!("schtasks failed: {}", e))?;
        if !status.success() {
            return Err("schtasks failed to create MemnosServer task".to_string());
        }
        let wtr = format!("\"{}\" wake-daemon", exe);
        let _ = Command::new("schtasks")
            .args([
                "/Create",
                "/SC",
                "ONLOGON",
                "/TN",
                "MemnosWakeDaemon",
                "/TR",
                &wtr,
                "/F",
            ])
            .status();
        if include_proxy {
            let ptr = format!("\"{}\" proxy", exe);
            let _ = Command::new("schtasks")
                .args([
                    "/Create",
                    "/SC",
                    "ONLOGON",
                    "/TN",
                    "MemnosProxy",
                    "/TR",
                    &ptr,
                    "/F",
                ])
                .status();
        }
        println!("[memnos] installed Windows Task Scheduler autostart (`MemnosServer` + `MemnosWakeDaemon`).");
        return Ok(());
    }

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let _ = (server_log, proxy_log, wake_log);
        let unit_dir = home_dir().join(".config").join("systemd").join("user");
        fs::create_dir_all(&unit_dir).map_err(|e| e.to_string())?;
        let srv_unit = unit_dir.join("memnos.service");
        let wake_unit = unit_dir.join("memnos-wake-daemon.service");
        let prx_unit = unit_dir.join("memnos-proxy.service");

        if remove {
            let _ = Command::new("systemctl")
                .args(["--user", "disable", "--now", "memnos.service"])
                .status();
            let _ = Command::new("systemctl")
                .args(["--user", "disable", "--now", "memnos-wake-daemon.service"])
                .status();
            let _ = Command::new("systemctl")
                .args(["--user", "disable", "--now", "memnos-proxy.service"])
                .status();
            let _ = fs::remove_file(&srv_unit);
            let _ = fs::remove_file(&wake_unit);
            let _ = fs::remove_file(&prx_unit);
            println!("[memnos] removed systemd user autostart units.");
            return Ok(());
        }

        let unit = format!(
            "[Unit]\nDescription=memnos zero-downtime gateway\nAfter=network.target\n\n[Service]\nExecStart={} gateway\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n",
            exe
        );
        fs::write(&srv_unit, unit).map_err(|e| e.to_string())?;
        let wunit = format!(
            "[Unit]\nDescription=memnos LISTEN/NOTIFY wake daemon\nAfter=memnos.service\n\n[Service]\nExecStart={} wake-daemon\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n",
            exe
        );
        fs::write(&wake_unit, wunit).map_err(|e| e.to_string())?;
        let _ = Command::new("systemctl")
            .args(["--user", "daemon-reload"])
            .status();
        let _ = Command::new("systemctl")
            .args(["--user", "enable", "--now", "memnos.service"])
            .status();
        let _ = Command::new("systemctl")
            .args(["--user", "enable", "--now", "memnos-wake-daemon.service"])
            .status();
        println!(
            "[memnos] installed systemd user units -> {}, {}",
            srv_unit.display(),
            wake_unit.display()
        );
        if include_proxy {
            let punit = format!(
                "[Unit]\nDescription=memnos LLM capture proxy\nAfter=network.target\n\n[Service]\nExecStart={} proxy\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n",
                exe
            );
            fs::write(&prx_unit, punit).map_err(|e| e.to_string())?;
            let _ = Command::new("systemctl")
                .args(["--user", "enable", "--now", "memnos-proxy.service"])
                .status();
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn terminal_agnostic_auto_discovery_coexists_and_chains_statuslines() {
        let _guard = ENV_LOCK.lock().unwrap();
        let tmp = std::env::temp_dir().join(format!(
            "memnos-agent-discovery-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&tmp);
        fs::create_dir_all(&tmp).unwrap();

        let prev_home = env::var_os("HOME");
        let prev_userprofile = env::var_os("USERPROFILE");
        let prev_path = env::var_os("PATH");
        let prev_ci = env::var_os("CI");
        let prev_mci = env::var_os("MEMNOS_CI");
        let prev_skip = env::var_os("MEMNOS_SKIP_AUTO_WIRE");
        let prev_iso = env::var_os("MEMNOS_ISOLATED_HOME_DISCOVERY");

        env::set_var("HOME", &tmp);
        env::set_var("USERPROFILE", &tmp);
        env::set_var("MEMNOS_ISOLATED_HOME_DISCOVERY", "1");
        env::remove_var("CI");
        env::remove_var("MEMNOS_CI");
        env::remove_var("MEMNOS_SKIP_AUTO_WIRE");

        // Populate fake harness footprints + NVM binary dir (simulating non-login terminal)
        let nvm_bin = tmp
            .join(".nvm")
            .join("versions")
            .join("node")
            .join("v22.0.0")
            .join("bin");
        fs::create_dir_all(&nvm_bin).unwrap();
        #[cfg(windows)]
        fs::write(nvm_bin.join("gemini.cmd"), "@echo off\r\n").unwrap();
        #[cfg(not(windows))]
        fs::write(nvm_bin.join("gemini"), "#!/bin/sh\n").unwrap();
        env::set_var("PATH", &tmp.join("empty-path"));

        // 1) Claude Code with pre-existing third-party statusLine + custom hook
        fs::create_dir_all(tmp.join(".claude")).unwrap();
        fs::write(
            tmp.join(".claude").join("settings.json"),
            serde_json::to_string_pretty(&json!({
                "statusLine": { "type": "command", "command": "/usr/local/bin/custom-sl" },
                "hooks": {
                    "SessionStart": [{ "hooks": [{ "type": "command", "command": "echo third-party-claude" }] }]
                }
            }))
            .unwrap(),
        )
        .unwrap();

        // 2) Antigravity AND Gemini CLI coexisting in ~/.gemini
        fs::create_dir_all(tmp.join(".gemini").join("config")).unwrap();
        fs::write(
            tmp.join(".gemini").join("settings.json"),
            serde_json::to_string_pretty(&json!({
                "theme": "Default",
                "hooks": {
                    "BeforeAgent": [{ "hooks": [{ "type": "command", "command": "echo third-party-gemini" }] }]
                }
            }))
            .unwrap(),
        )
        .unwrap();

        // 3) Cursor with pre-existing hook + Codex
        fs::create_dir_all(tmp.join(".cursor")).unwrap();
        fs::write(
            tmp.join(".cursor").join("hooks.json"),
            serde_json::to_string_pretty(&json!({
                "version": 1,
                "hooks": {
                    "sessionStart": [{ "command": "echo third-party-cursor", "timeout": 5 }]
                }
            }))
            .unwrap(),
        )
        .unwrap();
        fs::create_dir_all(tmp.join(".codex")).unwrap();

        let (detected_before, unwired_before) = inspect_installed_agent_wiring();
        assert!(detected_before.contains(&"claude-code"));
        assert!(detected_before.contains(&"antigravity"));
        assert!(
            detected_before.contains(&"gemini-cli"),
            "gemini-cli must NOT be skipped when antigravity is also installed"
        );
        assert!(detected_before.contains(&"cursor"));
        assert!(detected_before.contains(&"codex"));
        assert_eq!(detected_before.len(), unwired_before.len());

        let mut cfg = Config::default();
        cfg.port = Some(8900);
        cfg.admin_token = Some("mnk_test_token_123".to_string());

        let synced = auto_sync_installed_agents(&cfg);
        assert!(synced.contains(&"claude-code"));
        assert!(synced.contains(&"antigravity"));
        assert!(synced.contains(&"gemini-cli"));
        assert!(synced.contains(&"cursor"));
        assert!(synced.contains(&"codex"));

        // Run a second time to verify idempotency (no duplicate hooks or duplicate MD blocks)
        let _ = auto_sync_installed_agents(&cfg);

        let (_, unwired_after) = inspect_installed_agent_wiring();
        assert!(
            unwired_after.is_empty(),
            "all detected harnesses should be wired, got unwired={:?}",
            unwired_after
        );

        // Verify ~/.memnos/bin/statusline AND ~/.local/bin/statusline shims exist
        #[cfg(unix)]
        {
            assert!(tmp.join(".memnos").join("bin").join("statusline").is_file());
            assert!(tmp.join(".local").join("bin").join("statusline").is_file());
            // Verify Claude pre-existing statusLine was chained into claude-statusline-mux.sh
            let mux = fs::read_to_string(
                tmp.join(".memnos")
                    .join("bin")
                    .join("claude-statusline-mux.sh"),
            )
            .unwrap();
            assert!(mux.contains("/usr/local/bin/custom-sl"));
            assert!(mux.contains("hook statusline"));
        }
        #[cfg(windows)]
        {
            assert!(tmp
                .join(".memnos")
                .join("bin")
                .join("statusline.cmd")
                .is_file());
            assert!(tmp
                .join(".local")
                .join("bin")
                .join("statusline.cmd")
                .is_file());
        }

        // Verify third-party hooks were preserved alongside memnos hooks
        let claude_st = read_json_obj(&tmp.join(".claude").join("settings.json"));
        let cs_hooks = serde_json::to_string(&claude_st["hooks"]["SessionStart"]).unwrap();
        assert!(cs_hooks.contains("third-party-claude") && cs_hooks.contains("hook status"));
        assert_eq!(claude_st["hooks"]["SessionStart"].as_array().unwrap().len(), 2);

        let gem_st = read_json_obj(&tmp.join(".gemini").join("settings.json"));
        let gb_hooks = serde_json::to_string(&gem_st["hooks"]["BeforeAgent"]).unwrap();
        assert!(gb_hooks.contains("third-party-gemini") && gb_hooks.contains("GEMINI_CLI"));
        assert_eq!(gem_st["hooks"]["BeforeAgent"].as_array().unwrap().len(), 2);
        assert!(
            fs::read_to_string(tmp.join(".gemini").join("GEMINI.md"))
                .unwrap()
                .matches("## memnos — long-term memory")
                .count()
                == 1
        );

        let cur_hk = read_json_obj(&tmp.join(".cursor").join("hooks.json"));
        assert_eq!(cur_hk["hooks"]["sessionStart"].as_array().unwrap().len(), 2);

        // Restore env
        match prev_home {
            Some(v) => env::set_var("HOME", v),
            None => env::remove_var("HOME"),
        }
        match prev_userprofile {
            Some(v) => env::set_var("USERPROFILE", v),
            None => env::remove_var("USERPROFILE"),
        }
        match prev_path {
            Some(v) => env::set_var("PATH", v),
            None => env::remove_var("PATH"),
        }
        match prev_ci {
            Some(v) => env::set_var("CI", v),
            None => env::remove_var("CI"),
        }
        match prev_mci {
            Some(v) => env::set_var("MEMNOS_CI", v),
            None => env::remove_var("MEMNOS_CI"),
        }
        match prev_skip {
            Some(v) => env::set_var("MEMNOS_SKIP_AUTO_WIRE", v),
            None => env::remove_var("MEMNOS_SKIP_AUTO_WIRE"),
        }
        match prev_iso {
            Some(v) => env::set_var("MEMNOS_ISOLATED_HOME_DISCOVERY", v),
            None => env::remove_var("MEMNOS_ISOLATED_HOME_DISCOVERY"),
        }
        let _ = fs::remove_dir_all(&tmp);
    }
}
