//! Write-time secret & high-entropy token redaction (`core/redact.py` parity).

use once_cell::sync::Lazy;
use regex::{Captures, Regex};
use std::collections::HashMap;

static PATTERNS: Lazy<Vec<(&'static str, Regex)>> = Lazy::new(|| {
    vec![
        (
            "private_key",
            Regex::new(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----")
                .unwrap(),
        ),
        (
            "stripe_key",
            Regex::new(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b").unwrap(),
        ),
        (
            "openai_key",
            Regex::new(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b").unwrap(),
        ),
        (
            "anthropic_key",
            Regex::new(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b").unwrap(),
        ),
        ("aws_key", Regex::new(r"\bAKIA[0-9A-Z]{16}\b").unwrap()),
        (
            "google_key",
            Regex::new(r"\bAIza[0-9A-Za-z_-]{30,}\b").unwrap(),
        ),
        (
            "gh_token",
            Regex::new(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b").unwrap(),
        ),
        (
            "slack_token",
            Regex::new(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b").unwrap(),
        ),
        (
            "memnos_token",
            Regex::new(r"\bmnk_[A-Za-z0-9_-]{20,}\b").unwrap(),
        ),
        (
            "jwt",
            Regex::new(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
                .unwrap(),
        ),
        (
            "bearer",
            Regex::new(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b").unwrap(),
        ),
        (
            "credential",
            Regex::new(
                r#"(?i)(\b(?:api[_-]?key|secret|password|passwd|pwd|token|access[_-]?key)\b\s*[:=]\s*["']?)([^\s"';,]{8,})(["']?)"#,
            )
            .unwrap(),
        ),
    ]
});

static TOKEN_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b[A-Za-z0-9_\-+/=]{28,}\b").unwrap());

fn shannon_entropy(s: &str) -> f64 {
    if s.is_empty() {
        return 0.0;
    }
    let mut counts: HashMap<char, usize> = HashMap::new();
    let mut total = 0usize;
    for ch in s.chars() {
        *counts.entry(ch).or_insert(0) += 1;
        total += 1;
    }
    let n = total as f64;
    counts.values().fold(0.0, |acc, &c| {
        let p = (c as f64) / n;
        acc - p * p.log2()
    })
}

fn looks_secret(tok: &str) -> bool {
    if tok.len() < 28 {
        return false;
    }
    let has_alpha = tok.chars().any(|c| c.is_ascii_alphabetic());
    let has_digit = tok.chars().any(|c| c.is_ascii_digit());
    let has_sym = tok.chars().any(|c| "_-+/=".contains(c));
    if !(has_alpha && (has_digit || has_sym)) {
        return false;
    }
    shannon_entropy(tok) >= 3.5
}

pub fn redact(input: &str) -> (String, usize) {
    if input.is_empty() {
        return (String::new(), 0);
    }
    let mut text = input.to_string();
    let mut n = 0usize;

    for (label, pat) in PATTERNS.iter() {
        if *label == "credential" {
            text = pat
                .replace_all(&text, |caps: &Captures| {
                    let val = caps.get(2).map(|m| m.as_str()).unwrap_or("");
                    if val.starts_with("[REDACTED") {
                        return caps.get(0).unwrap().as_str().to_string();
                    }
                    n += 1;
                    format!(
                        "{}[REDACTED:credential]{}",
                        caps.get(1).map(|m| m.as_str()).unwrap_or(""),
                        caps.get(3).map(|m| m.as_str()).unwrap_or("")
                    )
                })
                .into_owned();
        } else {
            text = pat
                .replace_all(&text, |_caps: &Captures| {
                    n += 1;
                    format!("[REDACTED:{}]", label)
                })
                .into_owned();
        }
    }

    text = TOKEN_RE
        .replace_all(&text, |caps: &Captures| {
            let tok = caps.get(0).unwrap().as_str();
            if tok.starts_with("[REDACTED") || !looks_secret(tok) {
                tok.to_string()
            } else {
                n += 1;
                "[REDACTED:high_entropy]".to_string()
            }
        })
        .into_owned();

    (text, n)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_redact_known_tokens_and_high_entropy() {
        let (clean, count) = redact("My token is mnk_AbCdEfGhIjKlMnOpQrStUvWxYz1234 and api_key=super_secret_val_99");
        assert_eq!(count, 2);
        assert!(clean.contains("[REDACTED:memnos_token]"));
        assert!(clean.contains("[REDACTED:credential]"));
    }
}
