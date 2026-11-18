//! Git-anchored provenance (`code_anchors`) & recall-time code drift detection (`core/code_anchors.py`).

use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use std::collections::HashSet;
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodeAnchor {
    pub file: String,
    pub blob_sha: String,
    #[serde(default)]
    pub identifiers: Vec<String>,
}

static FILE_PATH_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r#"(?:^|[\s`'"(\[{])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|go|rs|sql|sh|md|json|yaml|yml|toml|html|css|c|cc|cpp|h|hpp|java|rb))"#,
    )
    .unwrap()
});

static BACKTICK_IDENT_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"`([A-Za-z_][A-Za-z0-9_.]{2,60})`").unwrap());
static FUNC_CALL_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"\b([A-Za-z_][A-Za-z0-9_]{2,50})\s*\(").unwrap());
static SNAKE_OR_CAMEL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b").unwrap()
});

const STOP_IDENTS: &[&str] = &[
    "true", "false", "none", "null", "self", "cls", "return", "import", "from", "class", "def",
    "async", "await", "function", "const", "let", "var", "print", "dict", "list", "tuple", "set",
    "str", "int", "float", "bool", "bytes",
];

pub fn compute_git_blob_sha(path: &Path) -> Option<String> {
    let data = fs::read(path).ok()?;
    let mut hasher = Sha1::new();
    hasher.update(format!("blob {}\0", data.len()).as_bytes());
    hasher.update(&data);
    Some(hex::encode(hasher.finalize()))
}

pub fn clean_rel_path(raw: &str) -> Option<String> {
    let mut p = raw.trim().replace('\\', "/");
    while let Some(rest) = p.strip_prefix("./") {
        p = rest.to_string();
    }
    if p.is_empty() || p.starts_with('/') {
        return None;
    }
    let parts: Vec<&str> = p.split('/').filter(|s| !s.is_empty()).collect();
    if parts.is_empty() || parts.iter().any(|&seg| seg == "." || seg == "..") {
        return None;
    }
    Some(parts.join("/"))
}

pub fn extract_identifiers_from_text(text: &str, max_identifiers: usize) -> Vec<String> {
    let mut found = Vec::new();
    let mut seen = HashSet::new();
    for pat in [&*BACKTICK_IDENT_RE, &*FUNC_CALL_RE, &*SNAKE_OR_CAMEL_RE] {
        for cap in pat.captures_iter(text) {
            let ident = cap.get(1).map(|m| m.as_str().trim_matches('.')).unwrap_or("");
            if ident.len() < 3 || STOP_IDENTS.contains(&ident.to_ascii_lowercase().as_str()) {
                continue;
            }
            let leaf = ident.rsplit('.').next().unwrap_or(ident);
            for cand in [ident, leaf] {
                if cand.len() >= 3
                    && !STOP_IDENTS.contains(&cand.to_ascii_lowercase().as_str())
                    && seen.insert(cand.to_string())
                {
                    found.push(cand.to_string());
                    if found.len() >= max_identifiers {
                        return found;
                    }
                }
            }
        }
    }
    found
}

pub fn extract_code_anchors(text: &str, repo_root: &Path, max_anchors: usize) -> Vec<CodeAnchor> {
    if text.is_empty() {
        return Vec::new();
    }
    let root = fs::canonicalize(repo_root).unwrap_or_else(|_| repo_root.to_path_buf());
    let mut seen_paths = HashSet::new();
    let mut candidates = Vec::new();
    for cap in FILE_PATH_RE.captures_iter(text) {
        if let Some(m) = cap.get(1) {
            let after = text[m.end()..].chars().next();
            let boundary_ok = match after {
                None => true,
                Some(c) => {
                    c.is_whitespace()
                        || matches!(
                            c,
                            '`' | '\'' | '"' | ':' | ')' | ',' | ']' | '}' | '.' | ';'
                        )
                }
            };
            if !boundary_ok {
                continue;
            }
            if let Some(raw) = clean_rel_path(m.as_str()) {
                if seen_paths.insert(raw.clone()) {
                    candidates.push(raw);
                }
            }
        }
    }
    if candidates.is_empty() {
        return Vec::new();
    }
    let idents = extract_identifiers_from_text(text, 12);
    let mut anchors = Vec::new();
    for rel in candidates {
        let fpath = root.join(&rel);
        let Ok(canon) = fs::canonicalize(&fpath) else {
            continue;
        };
        if !canon.starts_with(&root) || !canon.is_file() {
            continue;
        }
        let Some(blob_sha) = compute_git_blob_sha(&canon) else {
            continue;
        };
        let file_text = fs::read_to_string(&canon).unwrap_or_default();
        let matched: Vec<String> = idents
            .iter()
            .filter(|id| !id.contains('.') && file_text.contains(id.as_str()))
            .take(8)
            .cloned()
            .collect();
        anchors.push(CodeAnchor {
            file: rel,
            blob_sha,
            identifiers: matched,
        });
        if anchors.len() >= max_anchors {
            break;
        }
    }
    anchors
}

pub fn annotate_row_with_code_drift(row: &mut Value, repo_root: &Path) {
    let Some(anchors_val) = row.get("code_anchors").and_then(|v| v.as_array()).cloned() else {
        return;
    };
    let mut notices = Vec::new();
    for a in anchors_val {
        let Some(file) = a.get("file").and_then(|v| v.as_str()).and_then(clean_rel_path) else {
            continue;
        };
        let Some(old_sha) = a.get("blob_sha").and_then(|v| v.as_str()) else {
            continue;
        };
        let fpath = repo_root.join(&file);
        if !fpath.is_file() {
            notices.push(format!("{} was deleted since this memory was recorded", file));
            continue;
        }
        if let Some(cur_sha) = compute_git_blob_sha(&fpath) {
            if cur_sha != old_sha {
                let idents: Vec<String> = a
                    .get("identifiers")
                    .and_then(|v| v.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|x| x.as_str().map(|s| s.to_string()))
                            .collect()
                    })
                    .unwrap_or_default();
                if idents.is_empty() {
                    notices.push(format!("{} modified since recorded", file));
                } else {
                    let cur_text = fs::read_to_string(&fpath).unwrap_or_default();
                    let missing: Vec<_> = idents
                        .into_iter()
                        .filter(|id| !cur_text.contains(id.as_str()))
                        .collect();
                    if !missing.is_empty() {
                        notices.push(format!(
                            "{} modified (symbol(s) {} removed/renamed)",
                            file,
                            missing.join(", ")
                        ));
                    }
                }
            }
        }
    }
    if !notices.is_empty() {
        if let Some(obj) = row.as_object_mut() {
            obj.insert("code_drift_notice".to_string(), json!(notices.join("; ")));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_code_anchors_linear_regex() {
        let anchors = extract_code_anchors(
            "Updated `Cargo.toml` and rust/src/lib.rs for single binary.",
            Path::new("."),
            8,
        );
        assert!(anchors.iter().any(|a| a.file == "Cargo.toml"));
    }
}
