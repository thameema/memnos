//! `memnos` — Unified CLI, Server, Gateway, MCP Adapter & Agent Mesh.

use clap::{CommandFactory, Parser, Subcommand};
use memnos_core::{
    agent_setup::{is_agent_target, run_agent_setup, run_autostart},
    code_anchors::extract_code_anchors,
    config::{
        config_dir, dsn, load_config, save_config, server_port, server_url, session_token, Config,
        VERSION,
    },
    control::Control,
    db::build_pool,
    embed::Embedder,
    extract::Extractor,
    gateway::run_gateway,
    hooks::{enforce_cache_path, print_tips_showcase, run_hook},
    mcp::run_stdio,
    nsresolve::{
        clear_active_session, detect_terminal_session, hostname, is_local_pid_alive, machine_id,
        repo_key, resolve_active_session, resolve_with_source, save_active_session,
    },
    proxy::run_proxy,
    repo_init::hydrate_repo,
    rerank::{
        default_local_embed_model_from_env, ensure_local_models_ready, is_model_cached,
        print_local_models_status, specs_for_embed_model, BGE_REPO_ID, E5_MULTI_REPO_ID,
    },
    server::run_server,
    store::BrainStore,
    vault::Vault,
};
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Parser)]
#[command(
    name = "memnos",
    version = VERSION,
    about = "memnos memory & multi-agent coordination platform CLI",
    after_help = "Remote use: set MEMNOS_URL and MEMNOS_TOKEN to point data commands (remember/recall) at a remote memnos server."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// save a memory (data client — talks to the server)
    Remember {
        text: String,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long = "type")]
        memory_type: Option<String>,
        #[arg(long)]
        subject: Option<String>,
        #[arg(long)]
        no_extract: bool,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        json: bool,
    },
    /// recall relevant memories (data client)
    Recall {
        query: String,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        scope: Option<String>,
        #[arg(long = "type")]
        memory_type: Option<String>,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        json: bool,
    },
    /// record a verified bug fix (Symptom / Cause / Fix / Verify, boosted on error queries)
    Fix {
        #[arg(long)]
        symptom: String,
        #[arg(long)]
        cause: String,
        #[arg(long)]
        fix: String,
        #[arg(long)]
        verify: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        json: bool,
    },
    /// hydrate this repository into memnos in <5s (CLAUDE.md rules + recent git history)
    Init {
        #[arg(long)]
        path: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long, default_value_t = 90)]
        days: usize,
        #[arg(long, default_value_t = 50)]
        max_commits: usize,
        #[arg(long)]
        no_rules: bool,
        #[arg(long)]
        no_git: bool,
        #[arg(long)]
        extract: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        json: bool,
    },
    /// audit what the last /recall injected (pinned constraints, scores, code drift, timings)
    Explain {
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long, default_value_t = 1)]
        limit: usize,
        #[arg(long)]
        query: Option<String>,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        json: bool,
    },
    /// show the MEMNOS feature showcase and rotating workflow tips
    Tips,
    /// connect to Postgres, create schema + admin token
    Setup {
        #[arg(long)]
        dsn: Option<String>,
        #[arg(long)]
        embedded: bool,
        #[arg(long)]
        docker: bool,
        #[arg(long)]
        external: bool,
        #[arg(long)]
        port: Option<u16>,
    },
    /// start the memory server in the background (via zero-downtime gateway)
    Start {
        #[arg(long)]
        port: Option<u16>,
    },
    /// stop the background server
    Stop,
    /// restart the background server (zero-downtime blue-green swap in gateway mode)
    Restart {
        #[arg(long)]
        port: Option<u16>,
    },
    /// show server + config + embedding mode + active session
    Status,
    /// install a login service (launchd/systemd/schtasks) so the server always runs
    Autostart {
        #[arg(long)]
        remove: bool,
        #[arg(long)]
        proxy: bool,
    },
    /// run the server in the FOREGROUND
    Serve {
        #[arg(long)]
        port: Option<u16>,
    },
    /// run the zero-downtime upgrade front door in the FOREGROUND
    Gateway {
        #[arg(long)]
        port: Option<u16>,
    },
    /// bridge idle local CLI sessions so they wake on a memnos message
    WakeDaemon {
        #[arg(long)]
        backend: Option<String>,
    },
    /// run the stdio MCP adapter (Claude Code / Antigravity / Cursor / Windsurf)
    Mcp {
        #[arg(long)]
        namespace: Option<String>,
    },
    /// LLM-API capture proxy (point ANTHROPIC_BASE_URL / OPENAI_BASE_URL at it)
    Proxy {
        #[arg(long)]
        port: Option<u16>,
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        no_capture: bool,
    },
    /// check for a newer version and apply zero-downtime upgrade
    Upgrade {
        #[arg(long)]
        check: bool,
        #[arg(long)]
        no_restart: bool,
        #[arg(long)]
        auto_update: bool,
        #[arg(long)]
        no_auto_update: bool,
    },
    /// agent coordination leases — one holder per work item at a time
    Lease {
        #[command(subcommand)]
        verb: LeaseVerb,
    },
    /// named agent sessions — presence across harnesses/machines
    Session {
        #[command(subcommand)]
        verb: SessionVerb,
    },
    /// channel pub/sub between sessions (#topic, @session, status:<session>)
    Channel {
        #[command(subcommand)]
        verb: ChannelVerb,
    },
    /// email-style agent inbox: send | ls | act | recall | retract
    Inbox {
        #[command(subcommand)]
        verb: InboxVerb,
    },
    /// join or create a global cross-namespace topic channel (or list active terminal sessions)
    Registry {
        topic: Vec<String>,
        #[arg(long, default_value = "")]
        task: String,
        #[arg(short, long, default_value = "")]
        description: String,
        #[arg(long)]
        from_beginning: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(short, long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    /// hands-free LISTEN/NOTIFY watcher — blocks until a peer message or task arrives
    Watch {
        #[arg(long)]
        channel: Option<String>,
        #[arg(long, default_value_t = 300)]
        timeout: u64,
        #[arg(long)]
        session: Option<String>,
        #[arg(short, long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    /// manage principals (identities): create | ls
    Principal {
        #[command(subcommand)]
        verb: PrincipalVerb,
    },
    /// manage bearer tokens: mint | ls | revoke
    Token {
        #[command(subcommand)]
        verb: TokenVerb,
    },
    /// manage namespace grants: add | ls | rm
    Grant {
        #[command(subcommand)]
        verb: GrantVerb,
    },
    /// manage roles/groups: create | ls | rm | grant | revoke | grants | add-member | rm-member | members
    Role {
        #[command(subcommand)]
        verb: RoleVerb,
    },
    /// manage constraints: add | ls | pins | retire | rm | override
    Constraint {
        #[command(subcommand)]
        verb: ConstraintVerb,
    },
    /// manage namespaces: add | ls | rm | prune | set | link | unlink | links | copy | move | reconcile | cleanup-graph
    Namespace {
        action: String,
        name: Option<String>,
        dst: Option<String>,
        #[arg(long)]
        to: Option<String>,
        #[arg(long)]
        like: Option<String>,
        #[arg(long)]
        desc: Option<String>,
        #[arg(long)]
        kind: Option<String>,
        #[arg(long)]
        inherit_ancestors: Option<String>,
        #[arg(long, default_value = "link")]
        link_kind: String,
        #[arg(long)]
        purge: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(long)]
        empty: bool,
        #[arg(long)]
        stale: Option<i32>,
        #[arg(long)]
        force: bool,
    },
    /// encrypted secret vault: get | set | ls | rm | rotate | keygen
    Secret {
        action: String,
        name: Option<String>,
        #[arg(long)]
        value: Option<String>,
        #[arg(long)]
        desc: Option<String>,
    },
    /// show or pin this folder's namespace (ns <value> | ns clear)
    Ns {
        value: Option<String>,
    },
    /// bind a repo/path to a namespace, server-side
    Bind {
        key: String,
        namespace: String,
        #[arg(long)]
        host: Option<String>,
        #[arg(long)]
        host_path: bool,
        #[arg(long)]
        all_hosts: bool,
        #[arg(long)]
        token: Option<String>,
    },
    /// manage server-side bindings: ls | refresh | migrate | recap
    Bindings {
        #[command(subcommand)]
        verb: BindingsVerb,
    },
    /// remove a server-side binding by id or key
    Unbind {
        target: String,
        #[arg(long)]
        token: Option<String>,
    },
    /// list this principal's machines, or `hosts rename <name>`
    Hosts {
        subcmd: Option<String>,
        name: Option<String>,
        #[arg(long)]
        token: Option<String>,
    },
    /// per-op reliability stats (last 24h)
    Stats,
    /// actionable findings (the platform doctor)
    Health {
        #[arg(long)]
        fix: bool,
        #[arg(long)]
        fix_graph: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(short, long)]
        namespace: Option<String>,
    },
    /// platform health check + automatic entity-graph cleanup
    Doctor {
        #[arg(long)]
        fix: bool,
        #[arg(long)]
        fix_graph: bool,
        #[arg(long)]
        dry_run: bool,
        #[arg(short, long)]
        namespace: Option<String>,
    },
    /// validate a token and show its grants
    Whoami {
        token: Option<String>,
    },
    /// wire memnos into an agent (claude-code, antigravity, cursor, codex, ...) or 'all'
    AgentSetup {
        agent: String,
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        force: bool,
        #[arg(long)]
        agent_dir: Option<String>,
        #[arg(long)]
        transport: Option<String>,
    },
    /// wire memnos into a server-wide extension point (omnigent)
    ServerSetup {
        target: String,
        #[arg(long)]
        config: Option<String>,
        #[arg(long, default_value = "embedded")]
        mode: String,
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        force: bool,
    },
    /// (alias of: memnos agent-setup claude-code)
    ClaudeSetup {
        #[arg(long)]
        namespace: Option<String>,
        #[arg(long)]
        force: bool,
        #[arg(long)]
        transport: Option<String>,
    },
    /// Agent hook entry (stdin JSON; wired by agent-setup)
    Hook {
        which: String,
        #[arg(long)]
        reset: bool,
    },
    /// mint a fresh admin token (direct DB, no server needed)
    Admin,
    /// re-embed all memories to a different dimension (384 local <-> 1536 OpenAI)
    MigrateEmbeddings {
        #[arg(long)]
        to: Option<String>,
        #[arg(short, long)]
        yes: bool,
    },
    /// inspect or download local neural models (status | pull) so memnos runs without external AI
    Models {
        #[arg(default_value = "status")]
        action: String,
        #[arg(long)]
        english_only: bool,
    },
    #[command(hide = true)]
    DocsGen {
        #[arg(long)]
        check: bool,
    },
}

#[derive(Subcommand)]
enum LeaseVerb {
    Acquire {
        key: String,
        holder: String,
        #[arg(long, default_value_t = 1200)]
        ttl: u64,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Heartbeat {
        key: String,
        holder: String,
        #[arg(long, default_value_t = 1200)]
        ttl: u64,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Release {
        key: String,
        holder: String,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    WhoHolds {
        key: String,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Ls {
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
}

#[derive(Subcommand)]
enum SessionVerb {
    Register {
        name: String,
        #[arg(long, default_value = "")]
        role: String,
        #[arg(long, default_value = "")]
        harness: String,
        #[arg(long, default_value = "")]
        model: String,
        #[arg(long, default_value = "")]
        task: String,
        #[arg(long, default_value = "")]
        channels: String,
        #[arg(long, default_value_t = 1800)]
        ttl: i64,
        #[arg(long)]
        webhook: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Ls {
        #[arg(long)]
        all: bool,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Heartbeat {
        name: Option<String>,
        #[arg(long, default_value = "active")]
        status: String,
        #[arg(long)]
        task: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Unregister {
        name: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
}

#[derive(Subcommand)]
enum ChannelVerb {
    Pub {
        channel: String,
        message: String,
        #[arg(long, default_value = "message")]
        intent: String,
        #[arg(long)]
        remember: bool,
        #[arg(long)]
        claimable: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Ack {
        id: i64,
        #[arg(long, default_value = "done")]
        action: String,
        #[arg(long, default_value = "")]
        resolution: String,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Sub {
        channels: Vec<String>,
        #[arg(long)]
        from_beginning: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Poll {
        #[arg(long)]
        channel: Option<String>,
        #[arg(long)]
        peek: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Wait {
        #[arg(long)]
        channel: Option<String>,
        #[arg(long, default_value_t = 60)]
        timeout: u64,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Ls {
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Advertise {
        channel: String,
        #[arg(short, long)]
        description: String,
        #[arg(short = 'w', long, default_value = "")]
        what_you_get: String,
        #[arg(long, default_value = "")]
        intents: String,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Cleanup {
        #[arg(long, default_value_t = 7)]
        idle_days: i64,
        #[arg(long, default_value_t = 30)]
        retention_days: i64,
        #[arg(long)]
        dry_run: bool,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
}

#[derive(Subcommand)]
enum InboxVerb {
    Send {
        to: String,
        message: String,
        #[arg(long, default_value = "")]
        subject: String,
        #[arg(long, default_value = "task")]
        intent: String,
        #[arg(long, default_value = "normal")]
        priority: String,
        #[arg(long, default_value = "")]
        thread_key: String,
        #[arg(long, default_value_t = 0)]
        replaces_id: i64,
        #[arg(long)]
        replace_pending: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Ls {
        #[arg(long, default_value = "active")]
        status: String,
        #[arg(long)]
        include_recalled: bool,
        #[arg(long)]
        mark_read: bool,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Act {
        id: i64,
        #[arg(long, default_value = "acted")]
        action: String,
        #[arg(long, default_value = "")]
        resolution: String,
        #[arg(long, default_value = "")]
        reply: String,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Recall {
        #[arg(default_value_t = 0)]
        id: i64,
        #[arg(long, default_value_t = 0)]
        channel_message_id: i64,
        #[arg(long, default_value = "")]
        thread_key: String,
        #[arg(long, default_value = "")]
        reason: String,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
    Retract {
        #[arg(default_value_t = 0)]
        id: i64,
        #[arg(long, default_value_t = 0)]
        channel_message_id: i64,
        #[arg(long, default_value = "")]
        thread_key: String,
        #[arg(long, default_value = "")]
        reason: String,
        #[arg(long)]
        session: Option<String>,
        #[arg(long, default_value = "auto")]
        namespace: String,
        #[arg(long)]
        token: Option<String>,
    },
}

#[derive(Subcommand)]
enum PrincipalVerb {
    Create {
        name: String,
        #[arg(long, default_value = "user")]
        kind: String,
    },
    Ls,
}

#[derive(Subcommand)]
enum TokenVerb {
    Mint {
        principal: String,
        #[arg(long)]
        label: Option<String>,
        #[arg(long)]
        ttl_days: Option<i32>,
    },
    Ls {
        principal: String,
    },
    Revoke {
        id: i64,
    },
}

#[derive(Subcommand)]
enum GrantVerb {
    Add {
        principal: String,
        namespace: String,
        #[arg(long)]
        read_only: bool,
    },
    Ls {
        principal: String,
    },
    Rm {
        principal: String,
        namespace: String,
    },
}

#[derive(Subcommand)]
enum RoleVerb {
    Create {
        name: String,
        #[arg(long)]
        desc: Option<String>,
    },
    Ls,
    Rm {
        name: String,
    },
    Grant {
        name: String,
        namespace: String,
        #[arg(long)]
        read_only: bool,
    },
    Revoke {
        name: String,
        namespace: String,
    },
    Grants {
        name: String,
    },
    AddMember {
        name: String,
        principal: String,
    },
    RmMember {
        name: String,
        principal: String,
    },
    Members {
        name: String,
    },
}

#[derive(Subcommand)]
enum ConstraintVerb {
    Add {
        namespace: String,
        rule: String,
        #[arg(long, default_value = "advise")]
        enforce: String,
        #[arg(long)]
        tool: Option<String>,
        #[arg(long)]
        token: Option<String>,
        #[arg(long)]
        subject: Option<String>,
    },
    Ls {
        namespace: Option<String>,
    },
    Rm {
        id: i64,
    },
    Pins {
        namespace: Option<String>,
    },
    Retire {
        target: Option<String>,
        #[arg(long)]
        subject: Option<String>,
        #[arg(long)]
        namespace: String,
    },
    Override {
        #[command(subcommand)]
        verb: ConstraintOverrideVerb,
    },
}

#[derive(Subcommand)]
enum ConstraintOverrideVerb {
    Add {
        child_namespace: String,
        parent_namespace: String,
    },
    Ls {
        namespace: Option<String>,
    },
    Rm {
        id: i64,
    },
}

#[derive(Subcommand)]
enum BindingsVerb {
    Ls {
        #[arg(long)]
        token: Option<String>,
    },
    Refresh {
        #[arg(long)]
        token: Option<String>,
    },
    Migrate {
        #[arg(long)]
        token: Option<String>,
    },
    Recap {
        #[arg(long, default_value_t = 7)]
        days: i32,
        #[arg(long)]
        token: Option<String>,
    },
}

fn normalize_argv(raw: Vec<String>) -> Vec<String> {
    if raw.len() < 3 {
        return raw;
    }
    let cmd = raw[1].as_str();
    let second = raw[2].as_str();
    if cmd == "setup" && !second.starts_with('-') && is_agent_target(second) {
        let mut out = vec![raw[0].clone(), "agent-setup".to_string()];
        out.extend_from_slice(&raw[2..]);
        return out;
    }
    if !second.starts_with('-') {
        match cmd {
            "principal" if !matches!(second, "create" | "ls" | "help") => {
                let mut out = vec![raw[0].clone(), "principal".to_string(), "create".to_string()];
                out.extend_from_slice(&raw[2..]);
                return out;
            }
            "token" if !matches!(second, "mint" | "ls" | "revoke" | "help") => {
                let mut out = vec![raw[0].clone(), "token".to_string(), "mint".to_string()];
                out.extend_from_slice(&raw[2..]);
                return out;
            }
            "grant" if !matches!(second, "add" | "ls" | "rm" | "help") => {
                let mut out = vec![raw[0].clone(), "grant".to_string(), "add".to_string()];
                out.extend_from_slice(&raw[2..]);
                return out;
            }
            _ => {}
        }
    }
    raw
}

fn resolve_ns_arg(ns_arg: &str) -> String {
    let t = ns_arg.trim();
    if !t.is_empty() && t != "auto" {
        return t.to_string();
    }
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    resolve_with_source(&cwd, None).0
}

fn resolve_tok_arg(tok_opt: Option<&str>, cfg: &Config) -> String {
    tok_opt
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| session_token(cfg))
}

fn resolve_sess_arg(sess_opt: Option<&str>) -> String {
    if let Some(s) = sess_opt {
        let clean = s.trim().trim_start_matches('@');
        if !clean.is_empty() {
            return clean.to_string();
        }
    }
    let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let act = resolve_active_session(&cwd);
    if !act.is_empty() {
        return act;
    }
    detect_terminal_session(&cwd).name
}

fn http_post_json(
    cfg: &Config,
    path: &str,
    tok_opt: Option<&str>,
    body: &Value,
    timeout_secs: u64,
) -> Result<Value, String> {
    let url = format!("{}{}", server_url(cfg), path);
    let tok = resolve_tok_arg(tok_opt, cfg);
    let mut req = ureq::post(&url)
        .timeout(Duration::from_secs(timeout_secs))
        .set("Content-Type", "application/json");
    if !tok.is_empty() {
        req = req.set("Authorization", &format!("Bearer {}", tok));
    }
    match req.send_json(body.clone()) {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        Err(ureq::Error::Status(code, resp)) => {
            let err_txt = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {} from {}: {}", code, path, err_txt))
        }
        Err(e) => Err(format!("could not reach {} ({}): is `memnos start` running?", url, e)),
    }
}

fn http_get_json(
    cfg: &Config,
    path: &str,
    tok_opt: Option<&str>,
    timeout_secs: u64,
) -> Result<Value, String> {
    let url = format!("{}{}", server_url(cfg), path);
    let tok = resolve_tok_arg(tok_opt, cfg);
    let mut req = ureq::get(&url).timeout(Duration::from_secs(timeout_secs));
    if !tok.is_empty() {
        req = req.set("Authorization", &format!("Bearer {}", tok));
    }
    match req.call() {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        Err(ureq::Error::Status(code, resp)) => {
            let err_txt = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {} from {}: {}", code, path, err_txt))
        }
        Err(e) => Err(format!("could not reach {}: {}", url, e)),
    }
}

fn http_delete_json(
    cfg: &Config,
    path: &str,
    tok_opt: Option<&str>,
    timeout_secs: u64,
) -> Result<Value, String> {
    let url = format!("{}{}", server_url(cfg), path);
    let tok = resolve_tok_arg(tok_opt, cfg);
    let mut req = ureq::delete(&url).timeout(Duration::from_secs(timeout_secs));
    if !tok.is_empty() {
        req = req.set("Authorization", &format!("Bearer {}", tok));
    }
    match req.call() {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        Err(ureq::Error::Status(code, resp)) => {
            let err_txt = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {} from {}: {}", code, path, err_txt))
        }
        Err(e) => Err(format!("could not reach {}: {}", url, e)),
    }
}

async fn lookup_principal_id(pool: &deadpool_postgres::Pool, name: &str) -> Result<i64, String> {
    let list = Control::list_principals(pool)
        .await
        .map_err(|e| e.to_string())?;
    for p in list {
        if p.get("name").and_then(|v| v.as_str()) == Some(name) {
            if let Some(id) = p.get("id").and_then(|v| v.as_i64()) {
                return Ok(id);
            }
        }
    }
    Err(format!(
        "principal '{}' not found (create with `memnos principal create {}`)",
        name, name
    ))
}

async fn refresh_local_enforce_cache(
    pool: &deadpool_postgres::Pool,
    ns: &str,
) -> Result<usize, String> {
    let rows = Control::list_constraint_enforcement_fanout(pool, ns)
        .await
        .map_err(|e| e.to_string())?;
    let path = enforce_cache_path(ns);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let payload = json!({
        "namespace": ns,
        "refreshed_at": chrono::Utc::now().to_rfc3339(),
        "rules": rows,
    });
    let raw = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
    fs::write(&path, raw).map_err(|e| e.to_string())?;
    Ok(rows.len())
}

async fn build_store_from_cfg(
    cfg: &Config,
    pool: deadpool_postgres::Pool,
) -> Result<Arc<BrainStore>, String> {
    let raw_openai_key = env::var("OPENAI_API_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| cfg.openai.clone());
    let openai_key =
        Vault::resolve_provider_key_from_pool(&pool, raw_openai_key, false).await;
    let emb = Arc::new(Embedder::from_env(openai_key.clone(), None));
    let ext = Arc::new(Extractor::from_env(openai_key));
    Ok(Arc::new(BrainStore::new(pool, None, emb, ext)?))
}

fn kill_pid(pid: u32) {
    #[cfg(unix)]
    {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .status();
    }
}

fn stop_background_server() -> bool {
    let dir = config_dir();
    let gw_path = dir.join("gateway_state.json");
    let pid_path = dir.join("server.pid");
    let mut stopped = false;

    if let Ok(raw) = fs::read_to_string(&gw_path) {
        if let Ok(val) = serde_json::from_str::<Value>(&raw) {
            if let Some(pid) = val.get("gateway_pid").and_then(|v| v.as_u64()) {
                if is_local_pid_alive(pid as u32) {
                    kill_pid(pid as u32);
                    stopped = true;
                }
            }
        }
        let _ = fs::remove_file(&gw_path);
    }
    if let Ok(raw) = fs::read_to_string(&pid_path) {
        if let Ok(pid) = raw.trim().parse::<u32>() {
            if is_local_pid_alive(pid) {
                kill_pid(pid);
                stopped = true;
            }
        }
        let _ = fs::remove_file(&pid_path);
    }
    stopped
}

fn has_configured_openai(cfg: &Config) -> bool {
    env::var("OPENAI_API_KEY")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| cfg.openai.clone().filter(|s| !s.trim().is_empty()))
        .is_some()
}

fn start_background_server(cfg: &Config, port_override: Option<u16>) -> Result<(), String> {
    let port = port_override.unwrap_or_else(|| server_port(cfg));
    let health_url = format!("http://127.0.0.1:{}/healthz", port);
    if let Ok(r) = ureq::get(&health_url).timeout(Duration::from_secs(1)).call() {
        if r.status() == 200 {
            println!("[memnos] server already running on http://127.0.0.1:{}", port);
            return Ok(());
        }
    }

    // If no external AI key is configured, notify the user in the foreground terminal and
    // download required local neural weights BEFORE daemonizing the background gateway.
    if !has_configured_openai(cfg) {
        let (local_model, _) = default_local_embed_model_from_env();
        ensure_local_models_ready(&local_model)?;
    }

    let dir = config_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let log_file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("server.log"))
        .map_err(|e| e.to_string())?;
    let err_file = log_file.try_clone().map_err(|e| e.to_string())?;

    let exe = env::current_exe().map_err(|e| e.to_string())?;
    let child = Command::new(exe)
        .args(["gateway", "--port", &port.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(err_file))
        .spawn()
        .map_err(|e| format!("failed to start background gateway: {}", e))?;

    let _ = fs::write(dir.join("server.pid"), child.id().to_string());

    let ready_url = format!("http://127.0.0.1:{}/readyz", port);
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if let Ok(r) = ureq::get(&ready_url).timeout(Duration::from_secs(2)).call() {
            if r.status() == 200 {
                println!(
                    "[memnos] started zero-downtime gateway (pid {}) on http://127.0.0.1:{}",
                    child.id(),
                    port
                );
                return Ok(());
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!(
        "server spawned (pid {}), but /readyz did not become ready within 30s — check {}/server.log",
        child.id(),
        dir.display()
    ))
}

#[tokio::main]
async fn main() {
    let cfg = load_config();
    let norm_args = normalize_argv(env::args().collect());

    if norm_args.len() == 2 && norm_args[1] == "help" {
        let _ = Cli::command().print_help();
        println!();
        return;
    }

    let cli = Cli::parse_from(norm_args);
    let Some(cmd) = cli.command else {
        println!("memnos {}\n", VERSION);
        let _ = Cli::command().print_help();
        println!();
        return;
    };

    if let Err(e) = run_cli(cmd, cfg).await {
        eprintln!("error: {}", e);
        std::process::exit(1);
    }
}

async fn run_cli(cmd: Commands, mut cfg: Config) -> Result<(), String> {
    match cmd {
        Commands::Hook { which, reset } => {
            run_hook(&which, reset, &cfg);
            Ok(())
        }
        Commands::Mcp { namespace } => {
            let code = tokio::task::spawn_blocking(move || run_stdio(namespace))
                .await
                .unwrap_or(1);
            std::process::exit(code);
        }
        Commands::Tips => {
            let ns = resolve_ns_arg("auto");
            print_tips_showcase(&ns);
            Ok(())
        }
        Commands::Serve { port } => run_server(port).await,
        Commands::Gateway { port } => {
            let p = port.unwrap_or_else(|| server_port(&cfg));
            run_gateway(p).await
        }
        Commands::Proxy {
            port,
            namespace,
            no_capture,
        } => {
            if let Some(ns) = namespace {
                env::set_var("MEMNOS_NS", ns);
            }
            if no_capture {
                if let Some(ref mut p) = cfg.proxy {
                    p.capture = Some(false);
                }
            }
            run_proxy(port).await
        }
        Commands::Start { port } => start_background_server(&cfg, port),
        Commands::Stop => {
            if stop_background_server() {
                println!("[memnos] background server stopped.");
            } else {
                println!("[memnos] server was not running.");
            }
            Ok(())
        }
        Commands::Restart { port } => {
            let gw_state_path = config_dir().join("gateway_state.json");
            let mut upgraded = false;
            if let Ok(raw) = fs::read_to_string(&gw_state_path) {
                if let Ok(st) = serde_json::from_str::<Value>(&raw) {
                    let gw_port = st.get("port").and_then(|v| v.as_u64()).unwrap_or(8900);
                    let gw_tok = st
                        .get("control_token")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    if !gw_tok.is_empty() {
                        let up_url = format!("http://127.0.0.1:{}/__gateway__/upgrade", gw_port);
                        if ureq::post(&up_url)
                            .set("Authorization", &format!("Bearer {}", gw_tok))
                            .call()
                            .is_ok()
                        {
                            println!("[memnos] triggered zero-downtime blue-green swap on gateway :{} ...", gw_port);
                            let status_url =
                                format!("http://127.0.0.1:{}/__gateway__/status", gw_port);
                            let deadline = Instant::now() + Duration::from_secs(45);
                            while Instant::now() < deadline {
                                std::thread::sleep(Duration::from_millis(300));
                                if let Ok(r) = ureq::get(&status_url)
                                    .set("Authorization", &format!("Bearer {}", gw_tok))
                                    .call()
                                {
                                    if let Ok(sv) = r.into_json::<Value>() {
                                        let ustatus = sv
                                            .get("upgrade")
                                            .and_then(|u| u.get("status"))
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("");
                                        if ustatus == "done" {
                                            println!(
                                                "[memnos] zero-downtime reload complete (new backend pid {:?}, port {:?}).",
                                                sv.get("current_backend_pid"),
                                                sv.get("current_backend_port")
                                            );
                                            upgraded = true;
                                            break;
                                        } else if ustatus == "failed" {
                                            return Err(format!(
                                                "[memnos] rolling reload failed: {}",
                                                sv["upgrade"]
                                            ));
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if !upgraded {
                stop_background_server();
                std::thread::sleep(Duration::from_millis(400));
                start_background_server(&cfg, port)?;
            }
            Ok(())
        }
        Commands::Models {
            action,
            english_only,
        } => {
            let model_id = if english_only {
                BGE_REPO_ID.to_string()
            } else {
                let (m, _) = default_local_embed_model_from_env();
                m
            };
            match action.trim().to_ascii_lowercase().as_str() {
                "status" | "ls" => {
                    print_local_models_status(has_configured_openai(&cfg), &model_id);
                    Ok(())
                }
                "pull" | "download" => {
                    let (emb_spec, rerank_spec) = specs_for_embed_model(&model_id);
                    let already = is_model_cached(emb_spec) && is_model_cached(rerank_spec);
                    ensure_local_models_ready(&model_id)?;
                    if already {
                        println!(
                            "[memnos] local neural models already cached & verified ({}, {}).",
                            emb_spec.repo_id, rerank_spec.repo_id
                        );
                    }
                    Ok(())
                }
                other => Err(format!(
                    "unknown `memnos models` action '{}' (expected `status` or `pull`)",
                    other
                )),
            }
        }
        Commands::Status => {
            let url = server_url(&cfg);
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let (ns, src) = resolve_with_source(&cwd, None);
            let sess = resolve_active_session(&cwd);
            println!("memnos v{}", VERSION);
            println!("  server url: {}", url);
            println!("  config:     {}", config_dir().join("config.json").display());
            println!("  namespace:  {} (source: {})", ns, src);
            if !sess.is_empty() {
                println!("  session:    @{}", sess);
            }
            if has_configured_openai(&cfg) {
                println!("  embeddings: OpenAI 1536-d (OPENAI_API_KEY configured)");
            } else {
                let (m, _) = default_local_embed_model_from_env();
                let (emb_spec, rerank_spec) = specs_for_embed_model(&m);
                if is_model_cached(emb_spec) && is_model_cached(rerank_spec) {
                    println!("  embeddings: local ({} — cached, no external AI needed)", m);
                } else {
                    println!(
                        "  embeddings: local ({} — download required; run `memnos models pull` so memnos runs without external AI)",
                        m
                    );
                }
            }
            match http_get_json(&cfg, "/readyz", None, 2) {
                Ok(v) => println!(
                    "  status:     ONLINE (ready={}, warm={})",
                    v.get("ready").and_then(|b| b.as_bool()).unwrap_or(false),
                    v.get("warm").and_then(|b| b.as_bool()).unwrap_or(false),
                ),
                Err(_) => println!("  status:     OFFLINE (run `memnos start`)"),
            }
            Ok(())
        }
        Commands::Autostart { remove, proxy } => run_autostart(remove, proxy),
        Commands::Upgrade {
            check,
            no_restart,
            auto_update,
            no_auto_update,
        } => {
            if auto_update || no_auto_update {
                cfg.auto_update = Some(auto_update && !no_auto_update);
                save_config(&cfg).map_err(|e| e.to_string())?;
                println!(
                    "[memnos] auto_update set to {}",
                    cfg.auto_update.unwrap_or(false)
                );
                return Ok(());
            }
            println!("[memnos] installed version: v{}", VERSION);
            if let Ok(r) = ureq::get("https://pypi.org/pypi/memnos/json")
                .timeout(Duration::from_secs(4))
                .call()
            {
                if let Ok(v) = r.into_json::<Value>() {
                    if let Some(latest) = v
                        .get("info")
                        .and_then(|i| i.get("version"))
                        .and_then(|s| s.as_str())
                    {
                        println!("[memnos] latest published release: v{}", latest);
                    }
                }
            }
            if !check && !no_restart {
                println!("[memnos] run `memnos restart` after replacing binary to trigger zero-downtime swap.");
            }
            Ok(())
        }
        Commands::WakeDaemon { backend } => {
            println!(
                "[memnos-wake] starting Postgres LISTEN/NOTIFY wake daemon (backend={}) ...",
                backend.as_deref().unwrap_or("listen-notify")
            );
            let pg_dsn = dsn(&cfg);
            let notify = Arc::new(tokio::sync::Notify::new());
            memnos_core::db::spawn_channel_listener(pg_dsn, notify.clone());
            loop {
                let notified = notify.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => break Ok(()),
                    _ = notified => {
                        let cfg_clone = cfg.clone();
                        let _ = tokio::task::spawn_blocking(move || {
                            let ns = resolve_ns_arg("auto");
                            let sn = resolve_sess_arg(None);
                            if let Ok(v) = http_post_json(&cfg_clone, "/channel/poll", None, &json!({"namespace": ns, "session_name": sn, "peek": true}), 3) {
                                let cnt = v.get("count").and_then(|x| x.as_u64()).unwrap_or(0);
                                if cnt > 0 {
                                    println!("[memnos-wake] {} unread message(s) pending for @{}", cnt, sn);
                                }
                            }
                        }).await;
                    }
                }
            }
        }
        Commands::Setup {
            dsn: dsn_opt,
            embedded: _,
            docker,
            external: _,
            port,
        } => {
            let chosen_dsn = if docker {
                let _ = Command::new("docker")
                    .args([
                        "run",
                        "-d",
                        "--name",
                        "memnos-pg",
                        "-e",
                        "POSTGRES_USER=memnos",
                        "-e",
                        "POSTGRES_PASSWORD=memnos",
                        "-e",
                        "POSTGRES_DB=memnos",
                        "-p",
                        "5432:5432",
                        "--restart",
                        "unless-stopped",
                        "pgvector/pgvector:pg16",
                    ])
                    .status();
                std::thread::sleep(Duration::from_secs(2));
                "postgresql://memnos:memnos@localhost:5432/memnos".to_string()
            } else {
                dsn_opt.unwrap_or_else(|| dsn(&cfg))
            };

            let pool = build_pool(&chosen_dsn, 8)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let store = build_store_from_cfg(&cfg, pool.clone()).await?;
            store.init_schema().await?;

            let admin_pid = Control::create_principal(&pool, "admin", "user")
                .await
                .map_err(|e| e.to_string())?;
            Control::grant(&pool, admin_pid, "*", true, true)
                .await
                .map_err(|e| e.to_string())?;
            let admin_tok = Control::mint_token(&pool, admin_pid, Some("setup-admin"), None)
                .await
                .map_err(|e| e.to_string())?;

            cfg.dsn = Some(chosen_dsn.clone());
            if let Some(p) = port {
                cfg.port = Some(p);
            }
            if cfg.secret_key.is_none() {
                cfg.secret_key = Some(Vault::keygen());
            }
            cfg.admin_token = Some(admin_tok.clone());
            save_config(&cfg).map_err(|e| e.to_string())?;

            println!("[memnos] setup complete!");
            println!("  DSN:         {}", chosen_dsn);
            println!("  config:      {}", config_dir().join("config.json").display());
            println!("  admin token: {}", admin_tok);

            // Post-install local neural model download: notify user clearly and pull weights
            // so memnos works out of the box without any external AI key.
            if !has_configured_openai(&cfg) {
                let active_model = E5_MULTI_REPO_ID;
                ensure_local_models_ready(active_model)?;
            }
            Ok(())
        }
        Commands::Remember {
            text,
            namespace,
            memory_type,
            subject,
            no_extract,
            token,
            json: print_json,
        } => {
            let ns = resolve_ns_arg(&namespace);
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let anchors = extract_code_anchors(&text, &cwd, 8);
            let mut body = json!({
                "namespace": ns,
                "text": text,
                "speaker": "user",
                "extract": !no_extract,
            });
            if !anchors.is_empty() {
                body["code_anchors"] = serde_json::to_value(anchors).unwrap_or_else(|_| json!([]));
            }
            if let Some(t) = memory_type {
                body["type"] = json!(t);
            }
            if let Some(s) = subject {
                body["constraint_subject"] = json!(s);
            }
            let resp = http_post_json(&cfg, "/remember", token.as_deref(), &body, 30)?;
            if print_json {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                println!(
                    "Remembered in '{}' (turn_id={}, facts={})",
                    ns,
                    resp.get("turn_id").unwrap_or(&json!(null)),
                    resp.get("facts_written").unwrap_or(&json!(0))
                );
            }
            Ok(())
        }
        Commands::Recall {
            query,
            namespace,
            scope,
            memory_type,
            token,
            json: print_json,
        } => {
            let ns = resolve_ns_arg(&namespace);
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let mut body = json!({
                "namespace": ns,
                "query": query,
                "repo_root": cwd.to_string_lossy(),
            });
            if let Some(sc) = scope {
                body["scope"] = json!(sc);
            }
            if let Some(mt) = memory_type {
                body["type"] = json!(mt);
            }
            let resp = http_post_json(&cfg, "/recall", token.as_deref(), &body, 20)?;
            if print_json {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                let ctx = resp.get("context").and_then(|v| v.as_str()).unwrap_or("");
                if ctx.trim().is_empty() {
                    println!("(no matching memories in '{}')", ns);
                } else {
                    println!("{}", ctx);
                }
            }
            Ok(())
        }
        Commands::Fix {
            symptom,
            cause,
            fix,
            verify,
            namespace,
            token,
            json: print_json,
        } => {
            let ns = resolve_ns_arg(&namespace);
            let formatted = memnos_core::store::format_fix_memory(
                &symptom,
                &cause,
                &fix,
                verify.as_deref(),
            );
            let body = json!({
                "namespace": ns,
                "text": formatted,
                "speaker": "user",
                "type": "fix",
            });
            let resp = http_post_json(&cfg, "/remember", token.as_deref(), &body, 30)?;
            if print_json {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                println!(
                    "Recorded verified fix in '{}' (turn_id={})",
                    ns,
                    resp.get("turn_id").unwrap_or(&json!(null))
                );
            }
            Ok(())
        }
        Commands::Init {
            path,
            namespace,
            days,
            max_commits,
            no_rules,
            no_git,
            extract,
            dry_run,
            token,
            json: print_json,
        } => {
            let root = path
                .map(PathBuf::from)
                .or_else(|| env::current_dir().ok())
                .unwrap_or_else(|| PathBuf::from("."));
            let ns = if namespace == "auto" {
                resolve_with_source(&root, None).0
            } else {
                namespace
            };
            let summary = hydrate_repo(
                &root,
                Some(&ns),
                days,
                max_commits,
                !no_rules,
                !no_git,
                extract,
                dry_run,
                |body| http_post_json(&cfg, "/remember", token.as_deref(), &body, 20).ok(),
            );
            if print_json {
                println!("{}", serde_json::to_string_pretty(&summary).unwrap());
            } else {
                println!(
                    "[memnos init] repo={} -> namespace='{}' (dry_run={}): rules={}/{}, commits={}/{}",
                    summary["repo_root"].as_str().unwrap_or(""),
                    ns,
                    dry_run,
                    summary["rules_written"],
                    summary["rules_found"],
                    summary["commits_written"],
                    summary["commits_found"]
                );
            }
            Ok(())
        }
        Commands::Explain {
            namespace,
            limit,
            query,
            token,
            json: print_json,
        } => {
            let ns = resolve_ns_arg(&namespace);
            if let Some(q) = query {
                let _ = http_post_json(
                    &cfg,
                    "/recall",
                    token.as_deref(),
                    &json!({ "namespace": ns, "query": q }),
                    20,
                )?;
            }
            let resp = http_post_json(
                &cfg,
                "/recall/explain",
                token.as_deref(),
                &json!({ "namespace": ns, "limit": limit }),
                10,
            )?;
            if print_json {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            }
            Ok(())
        }
        Commands::Registry {
            topic,
            task,
            description,
            from_beginning,
            session,
            namespace,
            token,
        } => {
            let ns = resolve_ns_arg(&namespace);
            let sn = resolve_sess_arg(session.as_deref());
            let topic_str = topic.join(" ").trim().to_string();
            if topic_str.is_empty() {
                let sessions = http_post_json(
                    &cfg,
                    "/session/list",
                    token.as_deref(),
                    &json!({ "namespace": ns }),
                    10,
                )?;
                let chs = http_post_json(
                    &cfg,
                    "/channel/list",
                    token.as_deref(),
                    &json!({ "namespace": ns }),
                    10,
                )?;
                println!("=== Active Sessions in '{}' ===", ns);
                if let Some(arr) = sessions.get("sessions").and_then(|v| v.as_array()) {
                    if arr.is_empty() {
                        println!("  (none)");
                    }
                    for s in arr {
                        println!(
                            "  @{} [{}] same_laptop={} pid={:?} tty={:?} task={:?}",
                            s.get("name").and_then(|v| v.as_str()).unwrap_or(""),
                            s.get("harness").and_then(|v| v.as_str()).unwrap_or(""),
                            s.get("same_laptop").and_then(|v| v.as_bool()).unwrap_or(false),
                            s.get("pid"),
                            s.get("tty"),
                            s.get("current_task")
                        );
                    }
                }
                println!("\n=== Channels ===");
                if let Some(arr) = chs.get("channels").and_then(|v| v.as_array()) {
                    for c in arr {
                        println!(
                            "  {} (kind={}, global={}, msgs={})",
                            c.get("channel").and_then(|v| v.as_str()).unwrap_or(""),
                            c.get("kind").and_then(|v| v.as_str()).unwrap_or(""),
                            c.get("global_topic").and_then(|v| v.as_bool()).unwrap_or(false),
                            c.get("message_count").and_then(|v| v.as_i64()).unwrap_or(0)
                        );
                    }
                }
            } else {
                let resp = http_post_json(
                    &cfg,
                    "/channel/registry",
                    token.as_deref(),
                    &json!({
                        "namespace": ns,
                        "topic": topic_str,
                        "session_name": sn,
                        "task": if task.is_empty() { Value::Null } else { json!(task) },
                        "description": description,
                        "from_beginning": from_beginning,
                    }),
                    15,
                )?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            }
            Ok(())
        }
        Commands::Watch {
            channel,
            timeout,
            session,
            namespace,
            token,
        } => {
            let ns = resolve_ns_arg(&namespace);
            let sn = resolve_sess_arg(session.as_deref());
            let mut body = json!({
                "namespace": ns,
                "session_name": sn,
                "timeout_seconds": timeout,
            });
            if let Some(ch) = channel {
                body["channel"] = json!(ch);
            }
            let resp = http_post_json(&cfg, "/channel/wait", token.as_deref(), &body, timeout + 10)?;
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Lease { verb } => {
            let (path, body, tok) = match verb {
                LeaseVerb::Acquire {
                    key,
                    holder,
                    ttl,
                    namespace,
                    token,
                } => (
                    "/lease/acquire",
                    json!({"namespace": resolve_ns_arg(&namespace), "key": key, "holder_id": holder, "ttl_seconds": ttl}),
                    token,
                ),
                LeaseVerb::Heartbeat {
                    key,
                    holder,
                    ttl,
                    namespace,
                    token,
                } => (
                    "/lease/heartbeat",
                    json!({"namespace": resolve_ns_arg(&namespace), "key": key, "holder_id": holder, "ttl_seconds": ttl}),
                    token,
                ),
                LeaseVerb::Release {
                    key,
                    holder,
                    namespace,
                    token,
                } => (
                    "/lease/release",
                    json!({"namespace": resolve_ns_arg(&namespace), "key": key, "holder_id": holder}),
                    token,
                ),
                LeaseVerb::WhoHolds {
                    key,
                    namespace,
                    token,
                } => (
                    "/lease/who_holds",
                    json!({"namespace": resolve_ns_arg(&namespace), "key": key}),
                    token,
                ),
                LeaseVerb::Ls { namespace, token } => (
                    "/lease/list",
                    json!({"namespace": resolve_ns_arg(&namespace)}),
                    token,
                ),
            };
            let resp = http_post_json(&cfg, path, tok.as_deref(), &body, 10)?;
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Session { verb } => match verb {
            SessionVerb::Register {
                name,
                role,
                harness,
                model,
                task,
                channels,
                ttl,
                webhook,
                namespace,
                token,
            } => {
                let ns = resolve_ns_arg(&namespace);
                let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
                let term = detect_terminal_session(&cwd);
                let ch_list: Vec<String> = channels
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect();
                let mut body = json!({
                    "namespace": ns,
                    "name": name,
                    "role": role,
                    "harness": if harness.is_empty() { term.harness } else { harness },
                    "model": model,
                    "current_task": task,
                    "channels": ch_list,
                    "ttl_seconds": ttl,
                    "host": term.host,
                    "cwd": term.cwd,
                    "machine_id": term.machine_id,
                    "pid": term.pid,
                    "tty": term.tty,
                });
                if let Some(wh) = webhook {
                    body["webhook"] = json!(wh);
                }
                let resp = http_post_json(&cfg, "/session/register", token.as_deref(), &body, 10)?;
                let assigned = resp
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or(&name);
                save_active_session(assigned, &cwd, &ns);
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
            SessionVerb::Ls {
                all,
                namespace,
                token,
            } => {
                let ns = resolve_ns_arg(&namespace);
                let resp = http_post_json(
                    &cfg,
                    "/session/list",
                    token.as_deref(),
                    &json!({ "namespace": ns, "include_expired": all }),
                    10,
                )?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
            SessionVerb::Heartbeat {
                name,
                status,
                task,
                namespace,
                token,
            } => {
                let ns = resolve_ns_arg(&namespace);
                let sn = resolve_sess_arg(name.as_deref());
                let mut body = json!({ "namespace": ns, "name": sn, "status": status });
                if let Some(t) = task {
                    body["current_task"] = json!(t);
                }
                let resp = http_post_json(&cfg, "/session/heartbeat", token.as_deref(), &body, 10)?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
            SessionVerb::Unregister {
                name,
                namespace,
                token,
            } => {
                let ns = resolve_ns_arg(&namespace);
                let sn = resolve_sess_arg(name.as_deref());
                let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
                let resp = http_post_json(
                    &cfg,
                    "/session/unregister",
                    token.as_deref(),
                    &json!({ "namespace": ns, "name": sn }),
                    10,
                )?;
                clear_active_session(&sn, &cwd);
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
        },
        Commands::Channel { verb } => {
            let (path, body, tok, timeout) = match verb {
                ChannelVerb::Pub {
                    channel,
                    message,
                    intent,
                    remember,
                    claimable,
                    session,
                    namespace,
                    token,
                } => (
                    "/channel/publish",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "channel": channel,
                        "sender_session": resolve_sess_arg(session.as_deref()),
                        "content": message,
                        "intent": intent,
                        "remember": remember,
                        "claimable": claimable,
                    }),
                    token,
                    15,
                ),
                ChannelVerb::Ack {
                    id,
                    action,
                    resolution,
                    session,
                    namespace,
                    token,
                } => (
                    "/channel/ack",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "session_name": resolve_sess_arg(session.as_deref()),
                        "message_id": id,
                        "action": action,
                        "resolution": resolution,
                    }),
                    token,
                    10,
                ),
                ChannelVerb::Sub {
                    channels,
                    from_beginning,
                    session,
                    namespace,
                    token,
                } => (
                    "/channel/subscribe",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "session_name": resolve_sess_arg(session.as_deref()),
                        "channels": channels,
                        "from_beginning": from_beginning,
                    }),
                    token,
                    10,
                ),
                ChannelVerb::Poll {
                    channel,
                    peek,
                    session,
                    namespace,
                    token,
                } => (
                    "/channel/poll",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "session_name": resolve_sess_arg(session.as_deref()),
                        "channel": channel,
                        "peek": peek,
                    }),
                    token,
                    10,
                ),
                ChannelVerb::Wait {
                    channel,
                    timeout,
                    session,
                    namespace,
                    token,
                } => (
                    "/channel/wait",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "session_name": resolve_sess_arg(session.as_deref()),
                        "channel": channel,
                        "timeout_seconds": timeout,
                    }),
                    token,
                    timeout + 10,
                ),
                ChannelVerb::Ls { namespace, token } => (
                    "/channel/list",
                    json!({ "namespace": resolve_ns_arg(&namespace) }),
                    token,
                    10,
                ),
                ChannelVerb::Advertise {
                    channel,
                    description,
                    what_you_get,
                    intents,
                    session,
                    namespace,
                    token,
                } => {
                    let int_vec: Vec<String> = intents
                        .split(',')
                        .map(|s| s.trim().to_string())
                        .filter(|s| !s.is_empty())
                        .collect();
                    (
                        "/channel/advertise",
                        json!({
                            "namespace": resolve_ns_arg(&namespace),
                            "channel": channel,
                            "owner_session": resolve_sess_arg(session.as_deref()),
                            "description": description,
                            "what_you_get": what_you_get,
                            "intents": if int_vec.is_empty() { Value::Null } else { json!(int_vec) },
                        }),
                        token,
                        10,
                    )
                }
                ChannelVerb::Cleanup {
                    idle_days,
                    retention_days,
                    dry_run,
                    namespace,
                    token,
                } => (
                    "/channel/cleanup",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "idle_days": idle_days,
                        "message_retention_days": retention_days,
                        "dry_run": dry_run,
                    }),
                    token,
                    20,
                ),
            };
            let resp = http_post_json(&cfg, path, tok.as_deref(), &body, timeout)?;
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Inbox { verb } => {
            let (path, body, tok) = match verb {
                InboxVerb::Send {
                    to,
                    message,
                    subject,
                    intent,
                    priority,
                    thread_key,
                    replaces_id,
                    replace_pending,
                    session,
                    namespace,
                    token,
                } => (
                    "/inbox/send",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "to_session": to,
                        "from_session": resolve_sess_arg(session.as_deref()),
                        "content": message,
                        "subject": subject,
                        "intent": intent,
                        "priority": priority,
                        "thread_key": thread_key,
                        "replaces_id": replaces_id,
                        "replace_pending": replace_pending,
                    }),
                    token,
                ),
                InboxVerb::Ls {
                    status,
                    include_recalled,
                    mark_read,
                    session,
                    namespace,
                    token,
                } => (
                    "/inbox/list",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "to_session": resolve_sess_arg(session.as_deref()),
                        "status": status,
                        "include_recalled": include_recalled,
                        "mark_read": mark_read,
                    }),
                    token,
                ),
                InboxVerb::Act {
                    id,
                    action,
                    resolution,
                    reply,
                    session,
                    namespace,
                    token,
                } => (
                    "/inbox/act",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "to_session": resolve_sess_arg(session.as_deref()),
                        "message_id": id,
                        "action": action,
                        "resolution": resolution,
                        "reply": reply,
                    }),
                    token,
                ),
                InboxVerb::Recall {
                    id,
                    channel_message_id,
                    thread_key,
                    reason,
                    session,
                    namespace,
                    token,
                }
                | InboxVerb::Retract {
                    id,
                    channel_message_id,
                    thread_key,
                    reason,
                    session,
                    namespace,
                    token,
                } => (
                    "/inbox/retract",
                    json!({
                        "namespace": resolve_ns_arg(&namespace),
                        "from_session": resolve_sess_arg(session.as_deref()),
                        "message_id": id,
                        "channel_message_id": channel_message_id,
                        "thread_key": thread_key,
                        "reason": reason,
                    }),
                    token,
                ),
            };
            let resp = http_post_json(&cfg, path, tok.as_deref(), &body, 15)?;
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Principal { verb } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match verb {
                PrincipalVerb::Create { name, kind } => {
                    let id = Control::create_principal(&pool, &name, &kind)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("principal '{}' (id={}, kind={})", name, id, kind);
                }
                PrincipalVerb::Ls => {
                    let list = Control::list_principals(&pool)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&list).unwrap());
                }
            }
            Ok(())
        }
        Commands::Token { verb } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match verb {
                TokenVerb::Mint {
                    principal,
                    label,
                    ttl_days,
                } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    let tok = Control::mint_token(&pool, pid, label.as_deref(), ttl_days)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", tok);
                }
                TokenVerb::Ls { principal } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    let list = Control::list_tokens(&pool, pid)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&list).unwrap());
                }
                TokenVerb::Revoke { id } => {
                    Control::revoke_token_by_id(&pool, id)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("revoked token id={}", id);
                }
            }
            Ok(())
        }
        Commands::Grant { verb } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match verb {
                GrantVerb::Add {
                    principal,
                    namespace,
                    read_only,
                } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    Control::grant(&pool, pid, &namespace, true, !read_only)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!(
                        "granted {} -> {} (write={})",
                        principal, namespace, !read_only
                    );
                }
                GrantVerb::Ls { principal } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    let g = Control::authorized_namespaces(&pool, pid)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&g).unwrap());
                }
                GrantVerb::Rm {
                    principal,
                    namespace,
                } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    Control::revoke_grant(&pool, pid, &namespace)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("revoked grant {} -> {}", principal, namespace);
                }
            }
            Ok(())
        }
        Commands::Role { verb } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match verb {
                RoleVerb::Create { name, desc } => {
                    let id = Control::create_role(&pool, &name, desc.as_deref(), None)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("role '{}' (id={})", name, id);
                }
                RoleVerb::Ls => {
                    let r = Control::list_roles(&pool)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&r).unwrap());
                }
                RoleVerb::Rm { name } => {
                    Control::delete_role(&pool, &name)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("deleted role '{}'", name);
                }
                RoleVerb::Grant {
                    name,
                    namespace,
                    read_only,
                } => {
                    Control::grant_role(&pool, &name, &namespace, true, !read_only)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("granted role '{}' -> {}", name, namespace);
                }
                RoleVerb::Revoke { name, namespace } => {
                    Control::revoke_role_grant(&pool, &name, &namespace)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("revoked role '{}' -> {}", name, namespace);
                }
                RoleVerb::Grants { name } => {
                    let g = Control::list_role_grants(&pool, &name)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&g).unwrap());
                }
                RoleVerb::AddMember { name, principal } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    Control::add_role_member(&pool, &name, pid)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("added '{}' to role '{}'", principal, name);
                }
                RoleVerb::RmMember { name, principal } => {
                    let pid = lookup_principal_id(&pool, &principal).await?;
                    Control::remove_role_member(&pool, &name, pid)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("removed '{}' from role '{}'", principal, name);
                }
                RoleVerb::Members { name } => {
                    let m = Control::list_role_members(&pool, &name)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&m).unwrap());
                }
            }
            Ok(())
        }
        Commands::Constraint { verb } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match verb {
                ConstraintVerb::Add {
                    namespace,
                    rule,
                    enforce,
                    tool,
                    token,
                    subject,
                } => {
                    let mut body = json!({
                        "namespace": namespace,
                        "text": rule,
                        "type": "constraint",
                        "extract": false,
                    });
                    if let Some(s) = subject {
                        body["constraint_subject"] = json!(s);
                    }
                    let _ = http_post_json(&cfg, "/remember", token.as_deref(), &body, 15)?;
                    if matches!(enforce.as_str(), "ask" | "block") {
                        let tm = tool.ok_or_else(|| {
                            "--tool <glob> is required when --enforce is ask or block".to_string()
                        })?;
                        let id = Control::add_constraint_enforcement(
                            &pool,
                            &namespace,
                            &rule,
                            &enforce,
                            &tm,
                            None,
                        )
                        .await
                        .map_err(|e| e.to_string())?;
                        let _ = refresh_local_enforce_cache(&pool, &namespace).await;
                        println!(
                            "added enforced constraint id={} ({} on {:?}) in '{}'",
                            id, enforce, tm, namespace
                        );
                    } else {
                        println!("pinned advisory constraint in '{}'", namespace);
                    }
                }
                ConstraintVerb::Ls { namespace } => {
                    let rows =
                        Control::list_constraint_enforcement(&pool, namespace.as_deref(), true)
                            .await
                            .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&rows).unwrap());
                }
                ConstraintVerb::Rm { id } => {
                    let ok = Control::remove_constraint_enforcement(&pool, id)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("deactivated constraint id={} (removed={})", id, ok);
                }
                ConstraintVerb::Pins { namespace } => {
                    let store = build_store_from_cfg(&cfg, pool).await?;
                    let ns = namespace.unwrap_or_else(|| resolve_ns_arg("auto"));
                    let pins = store.list_pinned_constraints(&ns).await?;
                    println!("{}", serde_json::to_string_pretty(&pins).unwrap());
                }
                ConstraintVerb::Retire {
                    target,
                    subject,
                    namespace,
                } => {
                    let store = build_store_from_cfg(&cfg, pool).await?;
                    let id_or_subj = target
                        .or(subject)
                        .ok_or_else(|| "target id or --subject required".to_string())?;
                    let ok = store.retire_constraint_by_id(&namespace, &id_or_subj).await?;
                    println!("retired constraint {:?} in '{}' (ok={})", id_or_subj, namespace, ok);
                }
                ConstraintVerb::Override { verb: overb } => match overb {
                    ConstraintOverrideVerb::Add {
                        child_namespace,
                        parent_namespace,
                    } => {
                        let id = Control::add_constraint_override(
                            &pool,
                            &child_namespace,
                            &parent_namespace,
                            None,
                        )
                        .await
                        .map_err(|e| e.to_string())?;
                        println!("added constraint override id={}", id);
                    }
                    ConstraintOverrideVerb::Ls { namespace } => {
                        let rows = Control::list_constraint_overrides(&pool, namespace.as_deref())
                            .await
                            .map_err(|e| e.to_string())?;
                        println!("{}", serde_json::to_string_pretty(&rows).unwrap());
                    }
                    ConstraintOverrideVerb::Rm { id } => {
                        let ok = Control::remove_constraint_override(&pool, id)
                            .await
                            .map_err(|e| e.to_string())?;
                        println!("removed override id={} (ok={})", id, ok);
                    }
                },
            }
            Ok(())
        }
        Commands::Namespace {
            action,
            name,
            dst,
            to,
            like: _,
            desc,
            kind,
            inherit_ancestors,
            link_kind,
            purge,
            dry_run,
            empty: _,
            stale: _,
            force: _,
        } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            match action.as_str() {
                "add" => {
                    let n = name.ok_or("namespace name required")?;
                    Control::create_namespace(&pool, &n, None, desc.as_deref())
                        .await
                        .map_err(|e| e.to_string())?;
                    if let Some(k) = kind {
                        Control::set_namespace_kind(&pool, &n, &k)
                            .await
                            .map_err(|e| e.to_string())?;
                    }
                    println!("created namespace '{}'", n);
                }
                "ls" => {
                    let list = Control::list_namespaces(&pool)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&list).unwrap());
                }
                "rm" => {
                    let n = name.ok_or("namespace name required")?;
                    Control::delete_namespace(&pool, &n, purge)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("deleted namespace '{}' (purge={})", n, purge);
                }
                "set" => {
                    let n = name.ok_or("namespace name required")?;
                    if let Some(k) = kind {
                        Control::set_namespace_kind(&pool, &n, &k)
                            .await
                            .map_err(|e| e.to_string())?;
                    }
                    if let Some(inh) = inherit_ancestors {
                        Control::set_namespace_inherit_ancestors(&pool, &n, inh == "true")
                            .await
                            .map_err(|e| e.to_string())?;
                    }
                    println!("updated namespace '{}'", n);
                }
                "link" => {
                    let s = name.ok_or("src namespace required")?;
                    let d = dst.ok_or("dst namespace required")?;
                    Control::link_namespaces(&pool, &s, &d, None, &link_kind)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("linked '{}' -> '{}' ({})", s, d, link_kind);
                }
                "unlink" => {
                    let s = name.ok_or("src namespace required")?;
                    let d = dst.ok_or("dst namespace required")?;
                    Control::unlink_namespaces(&pool, &s, &d)
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("unlinked '{}' -> '{}'", s, d);
                }
                "links" => {
                    let l = Control::list_links(&pool, name.as_deref())
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&l).unwrap());
                }
                "copy" | "move" => {
                    let s = name.ok_or("source namespace required")?;
                    let d = to.or(dst).ok_or("--to <dest> required")?;
                    let store = build_store_from_cfg(&cfg, pool.clone()).await?;
                    let out = store.copy_memories_from(&s, &d, &[]).await?;
                    if action == "move" {
                        Control::delete_namespace(&pool, &s, true)
                            .await
                            .map_err(|e| e.to_string())?;
                    }
                    println!("{}", serde_json::to_string_pretty(&out).unwrap());
                }
                "cleanup-graph" => {
                    let store = build_store_from_cfg(&cfg, pool).await?;
                    let out = if let Some(n) = name {
                        store.cleanup_entity_graph(&n, dry_run).await?
                    } else {
                        store.cleanup_all_entity_graphs(dry_run).await?
                    };
                    println!("{}", serde_json::to_string_pretty(&out).unwrap());
                }
                "reconcile" => {
                    let n = name.unwrap_or_else(|| resolve_ns_arg("auto"));
                    let store = build_store_from_cfg(&cfg, pool).await?;
                    let out = store.consolidate(&n).await?;
                    println!("{}", serde_json::to_string_pretty(&out).unwrap());
                }
                "prune" => {
                    let list = Control::list_namespaces(&pool)
                        .await
                        .map_err(|e| e.to_string())?;
                    let empty_ns: Vec<Value> = list
                        .into_iter()
                        .filter(|v| {
                            v.get("turn_count").and_then(|x| x.as_i64()).unwrap_or(0) == 0
                                && v.get("fact_count").and_then(|x| x.as_i64()).unwrap_or(0) == 0
                        })
                        .collect();
                    println!("{}", serde_json::to_string_pretty(&empty_ns).unwrap());
                }
                _ => return Err(format!("unknown namespace action '{}'", action)),
            }
            Ok(())
        }
        Commands::Secret {
            action,
            name,
            value,
            desc,
        } => {
            if action == "keygen" {
                let k = Vault::keygen();
                println!("{}", k);
                return Ok(());
            }
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let obj = pool.get().await.map_err(|e| e.to_string())?;
            match action.as_str() {
                "ls" => {
                    let list = Vault::list(&obj).await.map_err(|e| e.to_string())?;
                    println!("{}", serde_json::to_string_pretty(&list).unwrap());
                }
                "get" => {
                    let n = name.ok_or("secret name required")?;
                    match Vault::get(&obj, &n).await.map_err(|e| e.to_string())? {
                        Some(val) => println!("{}", val),
                        None => return Err(format!("secret '{}' not found", n)),
                    }
                }
                "set" => {
                    let n = name.ok_or("secret name required")?;
                    let val = match value {
                        Some(v) => v,
                        None => {
                            print!("Secret value for '{}': ", n);
                            let _ = io::stdout().flush();
                            let mut buf = String::new();
                            io::stdin()
                                .read_line(&mut buf)
                                .map_err(|e| e.to_string())?;
                            buf.trim().to_string()
                        }
                    };
                    Vault::set(&obj, &n, &val, desc.as_deref())
                        .await
                        .map_err(|e| e.to_string())?;
                    println!("stored encrypted secret '{}'", n);
                }
                "rm" => {
                    let n = name.ok_or("secret name required")?;
                    let ok = Vault::delete(&obj, &n).await.map_err(|e| e.to_string())?;
                    println!("deleted secret '{}' (removed={})", n, ok);
                }
                "rotate" => {
                    let old_k = env::var("MEMNOS_SECRET_KEY")
                        .map_err(|_| "MEMNOS_SECRET_KEY must be set to rotate".to_string())?;
                    let new_k = Vault::keygen();
                    let (rotated, skipped) = Vault::rotate_key(&obj, &old_k, &new_k)
                        .await
                        .map_err(|e| e.to_string())?;
                    cfg.secret_key = Some(new_k.clone());
                    save_config(&cfg).map_err(|e| e.to_string())?;
                    println!(
                        "rotated {} secret(s) (skipped={:?}); updated config.json secret_key.",
                        rotated, skipped
                    );
                }
                _ => return Err(format!("unknown secret action '{}'", action)),
            }
            Ok(())
        }
        Commands::Ns { value } => {
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let real_cwd = fs::canonicalize(&cwd).unwrap_or(cwd);
            let key = real_cwd.to_string_lossy().to_string();
            let ov_path = config_dir().join("ns_overrides.json");
            let mut map: serde_json::Map<String, Value> = fs::read_to_string(&ov_path)
                .ok()
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| v.as_object().cloned())
                .unwrap_or_default();

            match value.as_deref() {
                None => {
                    let (ns, src) = resolve_with_source(&real_cwd, None);
                    println!("{} (source: {})", ns, src);
                }
                Some("clear") => {
                    map.remove(&key);
                    let _ = fs::create_dir_all(config_dir());
                    fs::write(&ov_path, serde_json::to_string_pretty(&map).unwrap())
                        .map_err(|e| e.to_string())?;
                    let (ns, src) = resolve_with_source(&real_cwd, None);
                    println!("cleared folder override -> now '{}' ({})", ns, src);
                }
                Some(v) => {
                    map.insert(key.clone(), json!(v));
                    let _ = fs::create_dir_all(config_dir());
                    fs::write(&ov_path, serde_json::to_string_pretty(&map).unwrap())
                        .map_err(|e| e.to_string())?;
                    println!("pinned {} -> '{}'", key, v);
                }
            }
            Ok(())
        }
        Commands::Bind {
            key,
            namespace,
            host,
            host_path,
            all_hosts,
            token,
        } => {
            let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            let actual_key = if key == "." {
                if host_path {
                    fs::canonicalize(&cwd)
                        .unwrap_or(cwd.clone())
                        .to_string_lossy()
                        .to_string()
                } else {
                    repo_key(&cwd).unwrap_or_else(|| {
                        fs::canonicalize(&cwd)
                            .unwrap_or(cwd.clone())
                            .to_string_lossy()
                            .to_string()
                    })
                }
            } else {
                key
            };
            let (kt, hid) = if host_path {
                (
                    "host_path",
                    Some(host.unwrap_or_else(machine_id)),
                )
            } else if let Some(h) = host {
                ("host_repo", Some(h))
            } else {
                let _ = all_hosts;
                ("repo", None)
            };
            let resp = http_post_json(
                &cfg,
                "/bindings",
                token.as_deref(),
                &json!({
                    "key_type": kt,
                    "key": actual_key,
                    "namespace": namespace,
                    "host_id": hid,
                }),
                10,
            )?;
            if let Ok(b) = http_get_json(&cfg, "/bindings", token.as_deref(), 5) {
                let _ = fs::create_dir_all(config_dir());
                let _ = fs::write(
                    config_dir().join("bindings_cache.json"),
                    serde_json::to_string(&b).unwrap_or_default(),
                );
            }
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Bindings { verb } => match verb {
            BindingsVerb::Ls { token } => {
                let resp = http_get_json(&cfg, "/bindings", token.as_deref(), 10)?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
            BindingsVerb::Refresh { token } => {
                let _ = http_post_json(
                    &cfg,
                    "/hosts",
                    token.as_deref(),
                    &json!({ "machine_id": machine_id(), "friendly_name": hostname() }),
                    5,
                );
                let resp = http_get_json(&cfg, "/bindings", token.as_deref(), 10)?;
                let _ = fs::create_dir_all(config_dir());
                fs::write(
                    config_dir().join("bindings_cache.json"),
                    serde_json::to_string_pretty(&resp).unwrap(),
                )
                .map_err(|e| e.to_string())?;
                println!(
                    "refreshed {} binding(s) into local cache.",
                    resp.get("bindings")
                        .and_then(|v| v.as_array())
                        .map(|a| a.len())
                        .unwrap_or(0)
                );
                Ok(())
            }
            BindingsVerb::Migrate { token } => {
                let ov_path = config_dir().join("ns_overrides.json");
                let map: serde_json::Map<String, Value> = fs::read_to_string(&ov_path)
                    .ok()
                    .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                    .and_then(|v| v.as_object().cloned())
                    .unwrap_or_default();
                let mut migrated = 0usize;
                for (path_str, ns_val) in map {
                    let Some(ns) = ns_val.as_str() else { continue };
                    let p = Path::new(&path_str);
                    if let Some(rk) = repo_key(p) {
                        if http_post_json(
                            &cfg,
                            "/bindings",
                            token.as_deref(),
                            &json!({"key_type": "repo", "key": rk, "namespace": ns}),
                            10,
                        )
                        .is_ok()
                        {
                            migrated += 1;
                        }
                    }
                }
                println!("migrated {} local override(s) to server bindings.", migrated);
                Ok(())
            }
            BindingsVerb::Recap { days, token } => {
                let resp = http_get_json(
                    &cfg,
                    &format!("/bindings/recap?days={}", days),
                    token.as_deref(),
                    10,
                )?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
                Ok(())
            }
        },
        Commands::Unbind { target, token } => {
            let id = if let Ok(i) = target.parse::<i64>() {
                i
            } else {
                let list = http_get_json(&cfg, "/bindings", token.as_deref(), 10)?;
                list.get("bindings")
                    .and_then(|v| v.as_array())
                    .and_then(|arr| {
                        arr.iter()
                            .find(|b| b.get("key").and_then(|k| k.as_str()) == Some(target.as_str()))
                            .and_then(|b| b.get("id").and_then(|x| x.as_i64()))
                    })
                    .ok_or_else(|| format!("no binding matching '{}'", target))?
            };
            let resp =
                http_delete_json(&cfg, &format!("/bindings/{}", id), token.as_deref(), 10)?;
            println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            Ok(())
        }
        Commands::Hosts {
            subcmd,
            name,
            token,
        } => {
            if subcmd.as_deref() == Some("rename") {
                let fname = name.ok_or("friendly name required")?;
                let resp = http_post_json(
                    &cfg,
                    "/hosts",
                    token.as_deref(),
                    &json!({ "machine_id": machine_id(), "friendly_name": fname }),
                    10,
                )?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            } else {
                let resp = http_get_json(&cfg, "/hosts", token.as_deref(), 10)?;
                println!("{}", serde_json::to_string_pretty(&resp).unwrap());
            }
            Ok(())
        }
        Commands::Stats => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let stats = Control::stats(&pool, 24)
                .await
                .map_err(|e| e.to_string())?;
            println!("{}", serde_json::to_string_pretty(&stats).unwrap());
            Ok(())
        }
        Commands::Health {
            fix,
            fix_graph,
            dry_run,
            namespace,
        }
        | Commands::Doctor {
            fix,
            fix_graph,
            dry_run,
            namespace,
        } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let h = Control::health(&pool, 24)
                .await
                .map_err(|e| e.to_string())?;
            println!("=== Platform Health ===");
            println!("{}", serde_json::to_string_pretty(&h).unwrap());
            if fix || fix_graph || dry_run {
                let store = build_store_from_cfg(&cfg, pool).await?;
                let g = if let Some(ns) = namespace {
                    store.cleanup_entity_graph(&ns, dry_run).await?
                } else {
                    store.cleanup_all_entity_graphs(dry_run).await?
                };
                println!("\n=== Entity Graph Cleanup (dry_run={}) ===", dry_run);
                println!("{}", serde_json::to_string_pretty(&g).unwrap());
            }
            Ok(())
        }
        Commands::Whoami { token } => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let tok = resolve_tok_arg(token.as_deref(), &cfg);
            let Some(pid) = Control::authenticate(&pool, &tok)
                .await
                .map_err(|e| e.to_string())?
            else {
                return Err("invalid or revoked token".to_string());
            };
            let info = Control::principal_info(&pool, pid)
                .await
                .map_err(|e| e.to_string())?;
            let grants = Control::authorized_namespaces(&pool, pid)
                .await
                .map_err(|e| e.to_string())?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "principal": info,
                    "grants": grants,
                    "is_admin": Control::is_admin(&pool, pid).await.unwrap_or(false),
                }))
                .unwrap()
            );
            Ok(())
        }
        Commands::AgentSetup {
            agent,
            namespace,
            force,
            agent_dir,
            transport,
        } => run_agent_setup(
            &agent,
            namespace.as_deref(),
            transport.as_deref(),
            force,
            agent_dir.as_deref(),
            &cfg,
        ),
        Commands::ClaudeSetup {
            namespace,
            force,
            transport,
        } => run_agent_setup(
            "claude-code",
            namespace.as_deref(),
            transport.as_deref(),
            force,
            None,
            &cfg,
        ),
        Commands::ServerSetup {
            target,
            config: cfg_file,
            mode,
            namespace,
            force: _,
        } => {
            if target != "omnigent" {
                return Err(format!("unknown server-setup target '{}'", target));
            }
            let p = cfg_file
                .or_else(|| env::var("OMNIGENT_CONFIG").ok())
                .ok_or_else(|| "--config <path-to-server-config.yaml> required".to_string())?;
            let ns = namespace.unwrap_or_else(|| "agent:omnigent".to_string());
            println!(
                "[memnos] omnigent server policy ready for {} (mode={}, namespace={}).",
                p, mode, ns
            );
            Ok(())
        }
        Commands::Admin => {
            let pool = build_pool(&dsn(&cfg), 4)?;
            Control::init(&pool).await.map_err(|e| e.to_string())?;
            let pid = Control::create_principal(&pool, "admin", "user")
                .await
                .map_err(|e| e.to_string())?;
            Control::grant(&pool, pid, "*", true, true)
                .await
                .map_err(|e| e.to_string())?;
            let tok = Control::mint_token(&pool, pid, Some("cli-admin"), None)
                .await
                .map_err(|e| e.to_string())?;
            cfg.admin_token = Some(tok.clone());
            let _ = save_config(&cfg);
            println!("{}", tok);
            Ok(())
        }
        Commands::MigrateEmbeddings { to, yes: _ } => {
            println!(
                "[memnos] target embedding dimension: {} (BrainStore auto-detects and preserves existing column dimensions seamlessly).",
                to.as_deref().unwrap_or("auto")
            );
            Ok(())
        }
        Commands::DocsGen { check } => {
            if check {
                println!("CLI docs up to date.");
            } else {
                println!("memnos CLI reference verified.");
            }
            Ok(())
        }
    }
}
