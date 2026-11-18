//! Configuration & `.env` / `~/.memnos/config.json` loader (cross-platform macOS, Linux, Windows).

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Default, Deserialize, Serialize, Clone)]
pub struct ProxyConfig {
    #[serde(default)]
    pub port: Option<u16>,
    #[serde(default)]
    pub capture: Option<bool>,
}

#[derive(Debug, Default, Deserialize, Serialize, Clone)]
pub struct Config {
    #[serde(default)]
    pub dsn: Option<String>,
    #[serde(default)]
    pub port: Option<u16>,
    #[serde(default)]
    pub secret_key: Option<String>,
    #[serde(default)]
    pub openai: Option<String>,
    #[serde(default)]
    pub admin_token: Option<String>,
    #[serde(default)]
    pub proxy_token: Option<String>,
    #[serde(default)]
    pub proxy: Option<ProxyConfig>,
    #[serde(default)]
    pub auto_update: Option<bool>,
    #[serde(default)]
    pub latest_known: Option<String>,
    #[serde(default)]
    pub agent_tokens: HashMap<String, String>,
}

pub fn home_dir() -> PathBuf {
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

pub fn config_dir() -> PathBuf {
    home_dir().join(".memnos")
}

pub fn config_path() -> PathBuf {
    config_dir().join("config.json")
}

pub fn load_dotenv(path: &Path) {
    let Ok(raw) = fs::read_to_string(path) else {
        return;
    };
    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = trimmed.split_once('=') {
            let key = k.trim();
            let val = v.trim().trim_matches('"').trim_matches('\'');
            if !key.is_empty() && env::var_os(key).is_none() {
                env::set_var(key, val);
            }
        }
    }
}

pub fn load_config() -> Config {
    load_dotenv(Path::new(".env"));
    let p = config_path();
    let cfg: Config = fs::read_to_string(&p)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    if let Some(ref dsn) = cfg.dsn {
        if env::var_os("MEMNOS_DSN").is_none() {
            env::set_var("MEMNOS_DSN", dsn);
        }
    }
    if let Some(ref sk) = cfg.secret_key {
        if env::var_os("MEMNOS_SECRET_KEY").is_none() {
            env::set_var("MEMNOS_SECRET_KEY", sk);
        }
    }
    if let Some(port) = cfg.port {
        if env::var_os("MEMNOS_PORT").is_none() {
            env::set_var("MEMNOS_PORT", port.to_string());
        }
    }
    if let Some(ref oa) = cfg.openai {
        if env::var_os("OPENAI_API_KEY").is_none() {
            env::set_var("OPENAI_API_KEY", oa);
        }
    }
    cfg
}

pub fn save_config(cfg: &Config) -> std::io::Result<()> {
    let dir = config_dir();
    fs::create_dir_all(&dir)?;
    let p = config_path();
    let tmp = dir.join(".config.json.tmp");
    let body = serde_json::to_string_pretty(cfg)?;
    fs::write(&tmp, body)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600));
    }
    fs::rename(&tmp, &p)?;
    Ok(())
}

pub fn dsn(cfg: &Config) -> String {
    env::var("MEMNOS_DSN")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| cfg.dsn.clone())
        .unwrap_or_else(|| "postgresql://memnos:memnos@localhost:5432/memnos".to_string())
}

pub fn server_port(cfg: &Config) -> u16 {
    env::var("MEMNOS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .or(cfg.port)
        .unwrap_or(8900)
}

pub fn server_url(cfg: &Config) -> String {
    if let Ok(u) = env::var("MEMNOS_URL") {
        if !u.trim().is_empty() {
            return u.trim_end_matches('/').to_string();
        }
    }
    format!("http://127.0.0.1:{}", server_port(cfg))
}

pub fn session_token(cfg: &Config) -> String {
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
