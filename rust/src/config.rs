//! Configuration & `.env` / `~/.memnos/config.json` loader (cross-platform macOS, Linux, Windows).

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const BUILD_SHA: &str = env!("MEMNOS_BUILD_SHA");

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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embed_choice: Option<String>,
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
    if let Ok(d) = env::var("MEMNOS_CONFIG_DIR") {
        if !d.trim().is_empty() {
            return PathBuf::from(d.trim());
        }
    }
    home_dir().join(".memnos")
}

pub fn config_path() -> PathBuf {
    config_dir().join("config.json")
}

fn env_truthy(name: &str) -> bool {
    matches!(
        env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub fn load_dotenv(path: &Path) {
    if env_truthy("MEMNOS_NO_DOTENV") {
        return;
    }
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
    if let Some(ref ec) = cfg.embed_choice {
        if env::var_os("MEMNOS_EMBED_CHOICE").is_none() {
            env::set_var("MEMNOS_EMBED_CHOICE", ec);
        }
    }
    cfg
}

/// Returns `"openai"`, `"local"`, or `None` when no explicit embedding choice has been made yet.
pub fn explicit_embed_choice(cfg: &Config) -> Option<String> {
    if let Ok(raw_env) = env::var("MEMNOS_EMBED_CHOICE") {
        match raw_env.trim().to_ascii_lowercase().as_str() {
            "local" | "computer" | "offline" | "384" => return Some("local".to_string()),
            "openai" | "cloud" | "1536" => return Some("openai".to_string()),
            _ => {}
        }
    }
    if let Ok(oa) = env::var("OPENAI_API_KEY") {
        if !oa.trim().is_empty() {
            return Some("openai".to_string());
        }
    }
    if let Some(ref oa) = cfg.openai {
        if !oa.trim().is_empty() {
            return Some("openai".to_string());
        }
    }
    let raw = cfg.embed_choice.clone().filter(|s| !s.trim().is_empty())?;
    match raw.trim().to_ascii_lowercase().as_str() {
        "openai" | "cloud" | "1536" => Some("openai".to_string()),
        "local" | "computer" | "offline" | "384" => Some("local".to_string()),
        _ => None,
    }
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

/// Formats a PostgreSQL DSN as `host:port/dbname` with usernames, passwords, and query secrets stripped.
pub fn redact_dsn_target(raw_dsn: &str) -> String {
    let trimmed = raw_dsn.trim();
    if let Some(rest) = trimmed
        .strip_prefix("postgresql://")
        .or_else(|| trimmed.strip_prefix("postgres://"))
    {
        let without_query = rest.split('?').next().unwrap_or(rest);
        let (authority, db_part) = match without_query.split_once('/') {
            Some((a, d)) => (a, d),
            None => (without_query, ""),
        };
        let host_raw = match authority.rsplit_once('@') {
            Some((_, hp)) => hp,
            None => authority,
        };
        let host_port = if host_raw.is_empty() {
            "localhost:5432".to_string()
        } else if host_raw.starts_with('[') {
            if host_raw.contains("]:") {
                host_raw.to_string()
            } else {
                format!("{}:5432", host_raw)
            }
        } else if host_raw.contains(':') {
            host_raw.to_string()
        } else {
            format!("{}:5432", host_raw)
        };
        let dbname = if db_part.trim().is_empty() {
            "postgres"
        } else {
            db_part.trim()
        };
        return format!("{}/{}", host_port, dbname);
    }

    let mut host = "localhost".to_string();
    let mut port = "5432".to_string();
    let mut dbname = "postgres".to_string();
    for tok in trimmed.split_whitespace() {
        if let Some((k, v)) = tok.split_once('=') {
            let val = v.trim_matches('\'').trim_matches('"');
            match k.trim().to_ascii_lowercase().as_str() {
                "host" | "hostaddr" => host = val.to_string(),
                "port" => port = val.to_string(),
                "dbname" => dbname = val.to_string(),
                _ => {}
            }
        }
    }
    format!("{}:{}/{}", host, port, dbname)
}

#[cfg(test)]
mod tests {
    use super::redact_dsn_target;

    #[test]
    fn test_redact_dsn_strips_credentials() {
        assert_eq!(
            redact_dsn_target("postgresql://memnos:s3cr3t@p@ss@localhost:5434/memnos_trial?sslmode=disable"),
            "localhost:5434/memnos_trial"
        );
        assert_eq!(
            redact_dsn_target("postgres://admin@127.0.0.1/memnos"),
            "127.0.0.1:5432/memnos"
        );
        assert_eq!(
            redact_dsn_target("host=db.internal port=5433 user=memnos password=topsecret dbname=trial"),
            "db.internal:5433/trial"
        );
    }
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
