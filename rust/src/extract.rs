//! Fact extraction, regex NER, developer signal extraction, and usage pricing (`extract.rs`).

use once_cell::sync::Lazy;
use regex::Regex;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::env;
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractedFact {
    #[serde(default)]
    pub subject: String,
    #[serde(default)]
    pub predicate: String,
    #[serde(default)]
    pub object: String,
    pub statement: String,
}

static PROPER_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b[A-Z][a-zA-Z]{2,}\b").unwrap());
static PR_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(PR|MR|pull request|issue)\s*#?(\d{1,7})\b").unwrap());
static SHA_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(?:commit|sha|rev(?:ision)?)[ :@]+([0-9a-f]{7,40})\b").unwrap());
static VER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\bv(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)\b|\b(?:version|released?|tagged?)\s+v?(\d+\.\d+(?:\.\d+)?)\b")
        .unwrap()
});

const STOP_ENTITIES: &[&str] = &[
    "The", "This", "That", "These", "Those", "There", "Then", "They", "Their", "Them", "And",
    "But", "For", "Not", "You", "Your", "Yours", "Our", "Out", "Are", "Was", "Were", "What",
    "When", "Where", "Which", "Who", "Why", "How", "Have", "Has", "Had", "Will", "Would", "Could",
    "Should", "Yeah", "Yes", "Hey", "Hi", "Hello", "Okay", "Sure", "Well", "Just", "Now", "Got",
    "Get", "Let", "Some", "Any", "All", "One", "Two", "Day", "Days", "Week", "Month", "Year",
    "Time", "Thing", "Things", "Really", "Maybe", "Did", "Does", "Can", "May", "Might", "Must",
    "Also", "Even", "Still", "Here", "Great", "Cool", "Thanks", "Thank", "Nice", "Update", "See",
    "Good", "Right", "Sorry",
];

pub fn extract_entities(text: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for m in PROPER_RE.find_iter(text) {
        let s = m.as_str();
        if STOP_ENTITIES.contains(&s) || !seen.insert(s.to_ascii_lowercase()) {
            continue;
        }
        out.push(s.to_string());
    }
    out
}

pub fn extract_developer_signals(text: &str) -> Vec<ExtractedFact> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut push = |stmt: String| {
        if out.len() < 12 && seen.insert(stmt.clone()) {
            out.push(ExtractedFact {
                subject: String::new(),
                predicate: String::new(),
                object: String::new(),
                statement: stmt,
            });
        }
    };
    for cap in PR_RE.captures_iter(text) {
        let kind = &cap[1];
        let label = if kind.eq_ignore_ascii_case("pr") || kind.eq_ignore_ascii_case("mr") {
            kind.to_ascii_uppercase()
        } else {
            kind.to_ascii_lowercase()
        };
        push(format!("References {} #{}", label, &cap[2]));
    }
    for cap in SHA_RE.captures_iter(text) {
        push(format!("References commit {}", cap[1].to_ascii_lowercase()));
    }
    for cap in VER_RE.captures_iter(text) {
        let v = cap
            .get(1)
            .or_else(|| cap.get(2))
            .map(|m| m.as_str())
            .unwrap_or("");
        if !v.is_empty() {
            push(format!("Mentions version {}", v));
        }
    }
    out
}

pub fn fake_extract(text: &str) -> Vec<ExtractedFact> {
    extract_entities(text)
        .into_iter()
        .map(|e| ExtractedFact {
            statement: format!("{} was mentioned.", e),
            subject: e,
            predicate: String::new(),
            object: String::new(),
        })
        .collect()
}

pub fn estimate_cost_usd(model: &str, prompt_tokens: u32, completion_tokens: u32) -> f64 {
    let (pin, pout) = match model {
        "text-embedding-3-small" => (0.02, 0.0),
        "gpt-4o-mini" => (0.15, 0.60),
        "gpt-4o" => (2.50, 10.00),
        _ => (0.0, 0.0),
    };
    (prompt_tokens as f64 / 1_000_000.0) * pin + (completion_tokens as f64 / 1_000_000.0) * pout
}

pub struct Extractor {
    pub model: String,
    api_key: Option<String>,
    base_url: String,
    fake_mode: bool,
    http: Client,
}

impl Extractor {
    pub fn from_env(openai_key: Option<String>) -> Self {
        let fake_mode = matches!(
            env::var("MEMNOS_FAKE_EXTRACT")
                .unwrap_or_default()
                .trim()
                .to_ascii_lowercase()
                .as_str(),
            "1" | "true" | "yes" | "on"
        );
        let extract_base = env::var("MEMNOS_EXTRACT_BASE_URL").ok().filter(|s| !s.is_empty());
        let key = openai_key
            .or_else(|| env::var("OPENAI_API_KEY").ok())
            .filter(|s| !s.trim().is_empty() && !s.starts_with("secret://"))
            .or_else(|| extract_base.as_ref().map(|_| "local".to_string()));
        let base_url = extract_base
            .unwrap_or_else(|| "https://api.openai.com/v1".to_string())
            .trim_end_matches('/')
            .to_string();
        let model = env::var("MEMNOS_EXTRACT_MODEL").unwrap_or_else(|_| "gpt-4o-mini".to_string());
        let http = Client::builder()
            .timeout(Duration::from_secs(45))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self {
            model,
            api_key: key,
            base_url,
            fake_mode,
            http,
        }
    }

    pub fn enabled(&self) -> bool {
        self.fake_mode || self.api_key.is_some()
    }

    pub async fn extract_facts(&self, text: &str, date_iso: &str) -> (Vec<ExtractedFact>, u32, u32) {
        if self.fake_mode {
            return (fake_extract(text), 0, 0);
        }
        let Some(ref key) = self.api_key else {
            return (Vec::new(), 0, 0);
        };
        let sys_prompt = format!(
            "Extract atomic, self-contained facts from the user text (reference date: {}). \
             Return strict JSON: {{\"facts\": [{{\"subject\": \"...\", \"predicate\": \"snake_case\", \
             \"object\": \"...\", \"statement\": \"Complete standalone sentence.\"}}]}}. \
             Keep identifiers, versions, numbers, and dates verbatim.",
            date_iso
        );
        let resp = self
            .http
            .post(format!("{}/chat/completions", self.base_url))
            .bearer_auth(key)
            .json(&json!({
                "model": self.model,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": text}
                ]
            }))
            .send()
            .await;

        let Ok(r) = resp else {
            return (Vec::new(), 0, 0);
        };
        if !r.status().is_success() {
            return (Vec::new(), 0, 0);
        }
        let Ok(val) = r.json::<Value>().await else {
            return (Vec::new(), 0, 0);
        };
        let tin = val
            .get("usage")
            .and_then(|u| u.get("prompt_tokens"))
            .and_then(|t| t.as_u64())
            .unwrap_or(0) as u32;
        let tout = val
            .get("usage")
            .and_then(|u| u.get("completion_tokens"))
            .and_then(|t| t.as_u64())
            .unwrap_or(0) as u32;
        let content = val
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("message"))
            .and_then(|m| m.get("content"))
            .and_then(|s| s.as_str())
            .unwrap_or("{}");
        let parsed: Vec<ExtractedFact> = serde_json::from_str::<Value>(content)
            .ok()
            .and_then(|v| v.get("facts").cloned())
            .and_then(|f| serde_json::from_value(f).ok())
            .unwrap_or_default();
        (parsed, tin, tout)
    }
}
