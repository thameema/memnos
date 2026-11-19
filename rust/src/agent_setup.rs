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
"#;

const AGY_SKILL_MD: &str = r#"---
name: memnos
description: >-
  Use this skill when the user invokes /memnos or asks to register a topic channel,
  coordinate with peer Claude Code / Antigravity / Gemini sessions, pin constraints,
  audit recall, or run memnos doctor/tips/watch.
---

# MEMNOS — Long-Term Memory & Multi-Agent Mesh (Antigravity / `agy`)

Use either the `memnos` MCP tools (`recall`, `recall_wide`, `remember`, `channel_registry`, `channel_publish`, `channel_poll`, `channel_ack`, `inbox_send`, `inbox_list`, `inbox_retract`) or the `memnos` CLI:

- **Join or create a global cross-namespace topic channel:** `memnos registry <topic>` (or `channel_registry(topic=...)`)
- **List active terminal sessions (same-laptop vs remote) & topic channels:** `memnos registry`
- **Post a competing-consumer task (claimed by 1 listener, no duplicate work):** `memnos registry <topic> --task "..."`
- **Hands-free LISTEN/NOTIFY wakeup watcher:** `memnos watch --timeout 300`
- **Pin a permanent project rule:** `memnos remember "<rule>" --type constraint`
- **Platform doctor & entity-graph cleanup:** `memnos doctor --fix`
- **Feature showcase & workflow tips:** `memnos tips`
"#;

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
    env::current_exe()
        .ok()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| "memnos".to_string())
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

pub fn detect_installed_agents() -> Vec<&'static str> {
    let home = home_dir();
    let mut out = Vec::new();
    if home.join(".claude").exists() {
        out.push("claude-code");
    }
    if home.join(".gemini").join("config").exists() || home.join(".gemini").join("antigravity-cli").exists() {
        out.push("antigravity");
    }
    if home.join(".cursor").exists() {
        out.push("cursor");
    }
    if home.join(".codex").exists() {
        out.push("codex");
    }
    if home.join(".codeium").join("windsurf").exists() {
        out.push("windsurf");
    }
    if home.join(".gemini").exists() && !out.contains(&"antigravity") {
        out.push("gemini-cli");
    }
    if home.join(".omnigent").exists() {
        out.push("omnigent");
    }
    out
}

pub fn run_agent_setup(
    agent_raw: &str,
    namespace_opt: Option<&str>,
    transport_opt: Option<&str>,
    force: bool,
    agent_dir: Option<&str>,
    cfg: &Config,
) -> Result<(), String> {
    let Some(agent) = canonical_agent_name(agent_raw) else {
        return Err(format!(
            "unknown agent '{}' — choose: {}, all",
            agent_raw,
            SUPPORTED_AGENTS.join(", ")
        ));
    };

    if agent == "all" {
        let detected = detect_installed_agents();
        if detected.is_empty() {
            println!("[memnos] No supported agents detected on this machine.");
            println!("         Supported: {}", SUPPORTED_AGENTS.join(", "));
            println!("         To force-setup a specific agent: memnos setup <name> --force");
            return Ok(());
        }
        println!(
            "[memnos] Detected {} agent(s): {}\n",
            detected.len(),
            detected.join(", ")
        );
        for name in detected {
            println!("── {} ──────────────────────────────", name);
            if let Err(e) = run_agent_setup(name, namespace_opt, None, force, agent_dir, cfg) {
                println!("  warning: {} setup error: {}", name, e);
            }
            println!();
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
            } else {
                default_user_ns()
            }
        });

    let exe = memnos_exe_string();
    let home = home_dir();

    if agent == "claude-code" {
        let claude_dir = home.join(".claude");
        if !claude_dir.exists() && !force {
            println!("Claude Code not detected (~/.claude missing). Re-run with --force.");
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

        // 2. ~/.claude/settings.json lifecycle hooks
        let settings_path = claude_dir.join("settings.json");
        let mut settings = read_json_obj(&settings_path);
        if !settings.get("hooks").map(|v| v.is_object()).unwrap_or(false) {
            settings["hooks"] = json!({});
        }
        let qexe = shell_quote(&exe);
        let env_prefix = format!(
            "MEMNOS_URL={} MEMNOS_TOKEN={} MEMNOS_NS={}",
            shell_quote(&url),
            shell_quote(&token),
            shell_quote(&ns)
        );
        let mk_hook = |which: &str, timeout: u64| {
            json!([{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": format!("{} {} hook {}", env_prefix, qexe, which),
                    "timeout": timeout
                }]
            }])
        };
        settings["hooks"]["SessionStart"] = json!([{
            "hooks": [{
                "type": "command",
                "command": format!("{} {} hook status", env_prefix, qexe),
                "timeout": 15
            }]
        }]);
        settings["hooks"]["UserPromptSubmit"] = json!([{
            "hooks": [{
                "type": "command",
                "command": format!("{} {} hook recall", env_prefix, qexe),
                "timeout": 15
            }]
        }]);
        settings["hooks"]["Stop"] = json!([{
            "hooks": [{
                "type": "command",
                "command": format!("{} {} hook remember", env_prefix, qexe),
                "timeout": 15
            }]
        }]);
        settings["hooks"]["PreToolUse"] = mk_hook("enforce", 10);
        write_json_pretty(&settings_path, &settings)?;

        println!(
            "[memnos] claude-code wired -> ~/.claude.json & ~/.claude/settings.json (MCP {}, hooks, ns={}).",
            transport, ns
        );
        return Ok(());
    }

    if agent == "antigravity" {
        let cfg_dir = home.join(".gemini").join("config");
        if !cfg_dir.exists() && !home.join(".gemini").join("antigravity-cli").exists() && !force {
            println!("Antigravity not detected (no ~/.gemini/config). Re-run with --force.");
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
        let env_prefix = format!(
            "MEMNOS_URL={} MEMNOS_TOKEN={} MEMNOS_NS={}",
            shell_quote(&url),
            shell_quote(&token),
            shell_quote(&ns)
        );
        let hk = |which: &str, timeout: u64| {
            json!({
                "type": "command",
                "command": format!("{} {} hook {}", env_prefix, qexe, which),
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

        println!(
            "[memnos] antigravity wired -> {} (MCP {}, hooks, GEMINI.md, skill; ns={}).",
            cfg_dir.display(),
            transport,
            ns
        );
        println!("          Restart agy / the Antigravity IDE to activate memory.");
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
        println!(
            "[memnos] codex wired -> {} (MCP server 'memnos', ns={}).",
            toml_path.display(),
            ns
        );
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
    write_json_pretty(&target_path, &doc)?;
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
    Ok(())
}

pub fn run_autostart(remove: bool, include_proxy: bool) -> Result<(), String> {
    let exe = memnos_exe_string();
    let log_dir = memnos_dir();
    let _ = fs::create_dir_all(&log_dir);
    let server_log = log_dir.join("server.log");
    let proxy_log = log_dir.join("proxy.log");

    #[cfg(target_os = "macos")]
    {
        let agents_dir = home_dir().join("Library").join("LaunchAgents");
        fs::create_dir_all(&agents_dir).map_err(|e| e.to_string())?;
        let srv_plist = agents_dir.join("ai.memnos.server.plist");
        let prx_plist = agents_dir.join("ai.memnos.proxy.plist");

        if remove {
            for p in [&srv_plist, &prx_plist] {
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
        let _ = (server_log, proxy_log);
        if remove {
            let _ = Command::new("schtasks")
                .args(["/Delete", "/TN", "MemnosServer", "/F"])
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
        println!("[memnos] installed Windows Task Scheduler autostart (`MemnosServer`).");
        return Ok(());
    }

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let _ = (server_log, proxy_log);
        let unit_dir = home_dir().join(".config").join("systemd").join("user");
        fs::create_dir_all(&unit_dir).map_err(|e| e.to_string())?;
        let srv_unit = unit_dir.join("memnos.service");
        let prx_unit = unit_dir.join("memnos-proxy.service");

        if remove {
            let _ = Command::new("systemctl")
                .args(["--user", "disable", "--now", "memnos.service"])
                .status();
            let _ = Command::new("systemctl")
                .args(["--user", "disable", "--now", "memnos-proxy.service"])
                .status();
            let _ = fs::remove_file(&srv_unit);
            let _ = fs::remove_file(&prx_unit);
            println!("[memnos] removed systemd user autostart units.");
            return Ok(());
        }

        let unit = format!(
            "[Unit]\nDescription=memnos zero-downtime gateway\nAfter=network.target\n\n[Service]\nExecStart={} gateway\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n",
            exe
        );
        fs::write(&srv_unit, unit).map_err(|e| e.to_string())?;
        let _ = Command::new("systemctl")
            .args(["--user", "daemon-reload"])
            .status();
        let _ = Command::new("systemctl")
            .args(["--user", "enable", "--now", "memnos.service"])
            .status();
        println!(
            "[memnos] installed systemd user unit -> {}",
            srv_unit.display()
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
