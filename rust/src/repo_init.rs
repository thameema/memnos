//! Cold-start repository hydration (`memnos init` — `repo_init.rs`).
//!
//! Hydrates a repository into `memnos` in `<5s` with zero LLM calls:
//! 1. Instruction/rule files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
//!    `.github/copilot-instructions.md`) -> verbatim `memory_type = "constraint"` turns
//!    with deterministic `constraint_subject` keys.
//! 2. Recent git history (`git log --since=<N>d --no-merges`) -> conventional/architectural/fix
//!    commits tagged with `speaker = "git"`.

use chrono::{DateTime, Utc};
use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::process::Command;

pub const RULE_FILES: &[&str] = &[
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
];

static HEADING_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$").unwrap());
static BULLET_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*[-*+]\s+(.+?)\s*$").unwrap());
static NUMBERED_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*\d+[.)]\s+(.+?)\s*$").unwrap());
static RULE_CUE_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r"(?i)\b(must|never|always|do not|don't|required|important|mandatory|forbidden|prohibited|avoid|prefer|ensure|convention|rule|constraint|guardrail|policy|invariant)\b",
    )
    .unwrap()
});
static RULE_SECTION_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r"(?i)\b(rules?|constraints?|conventions?|guidelines?|policies|policy|guardrails?|invariants?|never|always|must|security|testing|style|architecture|workflow|instructions?)\b",
    )
    .unwrap()
});
static CONVENTIONAL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(feat|fix|refactor|perf|security|arch|revert|docs|test|build|ci)(\([^)]+\))?!?\s*:\s*(.+)$")
        .unwrap()
});
static FIX_COMMIT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(fix|hotfix|bugfix|revert)(\([^)]+\))?!?\s*:|\b(fixes|closes|resolves)\s+#\d+|\b(root[- ]cause|bug|regression)\b")
        .unwrap()
});
static DECISION_COMMIT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(arch|refactor|perf|security)(\([^)]+\))?!?\s*:|!\s*:|\b(breaking[- ]change|decision|architecture|migrate[ds]?)\b")
        .unwrap()
});
static TRIVIAL_COMMIT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)^(wip|typo|lint|format|formatting|merge\b|bump\b|chore\(release\))").unwrap()
});
static SIGNAL_COMMIT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)(#\d+|\bv?\d+\.\d+\.\d+\b|\b(?:fixes|closes|refs|decision|breaking[- ]change|root[- ]cause|co-authored-by)\s*:)")
        .unwrap()
});
static WORD_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[a-z0-9]+").unwrap());
static NON_ALNUM_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoRule {
    pub source_file: String,
    pub section: String,
    pub text: String,
    pub memory_type: String,
    pub constraint_subject: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitCommitRecord {
    pub sha: String,
    pub full_sha: String,
    pub observed_at: String,
    pub author_name: String,
    pub subject: String,
    pub body: String,
    pub memory_type: String,
    pub speaker: String,
    pub text: String,
}

pub fn deterministic_rule_subject(rel_path: &str, rule_text: &str) -> String {
    let norm_path = rel_path.trim().to_lowercase().replace('\\', "/");
    let norm_text = rule_text
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let words: Vec<&str> = WORD_RE
        .find_iter(&norm_text)
        .take(5)
        .map(|m| m.as_str())
        .collect();
    let raw_slug = words.join("-");
    let clipped_slug: String = raw_slug.chars().take(40).collect();
    let slug = clipped_slug.trim_matches('-');
    let slug = if slug.is_empty() { "rule" } else { slug };

    let mut hasher = Sha1::new();
    hasher.update(norm_path.as_bytes());
    hasher.update(b"\x00");
    hasher.update(norm_text.as_bytes());
    let digest = hex::encode(hasher.finalize());
    let short_digest = &digest[..10];

    let replaced = NON_ALNUM_RE.replace_all(&norm_path, "-");
    let trimmed_tag = replaced.trim_matches('-');
    let path_tag: String = trimmed_tag.chars().take(20).collect();
    format!("init:{}:{}-{}", path_tag, slug, short_digest)
}

pub fn parse_repo_rules(repo_root: &Path, max_rules: usize) -> Vec<RepoRule> {
    let root = fs::canonicalize(repo_root).unwrap_or_else(|_| repo_root.to_path_buf());
    let mut rules = Vec::new();
    let mut seen = HashSet::new();

    for &rel in RULE_FILES {
        let fpath = root.join(rel);
        if !fpath.is_file() {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&fpath) else {
            continue;
        };
        let mut in_code_block = false;
        let mut current_section = rel.to_string();
        let is_dedicated = matches!(rel, ".cursorrules" | ".github/copilot-instructions.md");

        for line in raw.lines() {
            let stripped = line.trim();
            if stripped.starts_with("```") {
                in_code_block = !in_code_block;
                continue;
            }
            if in_code_block || stripped.is_empty() {
                continue;
            }
            if let Some(caps) = HEADING_RE.captures(line) {
                current_section = caps
                    .get(2)
                    .map(|m| m.as_str().trim().to_string())
                    .unwrap_or_else(|| rel.to_string());
                continue;
            }

            let bullet = BULLET_RE
                .captures(line)
                .or_else(|| NUMBERED_RE.captures(line))
                .and_then(|c| c.get(1).map(|m| m.as_str().trim().to_string()));

            let candidate = match bullet {
                Some(b) => b,
                None => {
                    if (is_dedicated || RULE_SECTION_RE.is_match(&current_section))
                        && RULE_CUE_RE.is_match(stripped)
                    {
                        stripped.to_string()
                    } else {
                        continue;
                    }
                }
            };

            if candidate.len() < 15 || candidate.len() > 600 {
                continue;
            }
            if candidate.starts_with('[')
                && candidate.ends_with(')')
                && candidate.contains("](")
                && candidate.split_whitespace().count() < 6
            {
                continue;
            }

            let in_rule_section = is_dedicated || RULE_SECTION_RE.is_match(&current_section);
            let has_cue = RULE_CUE_RE.is_match(&candidate);
            if !(in_rule_section || has_cue) {
                continue;
            }

            let prefix = if current_section != rel {
                format!("[{} :: {}] ", rel, current_section)
            } else {
                format!("[{}] ", rel)
            };
            let verbatim_text = format!("{}{}", prefix, candidate);
            let subj = deterministic_rule_subject(rel, &candidate);
            if !seen.insert(subj.clone()) {
                continue;
            }
            rules.push(RepoRule {
                source_file: rel.to_string(),
                section: current_section.clone(),
                text: verbatim_text,
                memory_type: "constraint".to_string(),
                constraint_subject: subj,
            });
            if rules.len() >= max_rules {
                return rules;
            }
        }
    }
    rules
}

pub fn parse_git_history(
    repo_root: &Path,
    since_days: usize,
    max_commits: usize,
) -> Vec<GitCommitRecord> {
    let root = fs::canonicalize(repo_root).unwrap_or_else(|_| repo_root.to_path_buf());
    if !root.join(".git").exists() {
        let ok = Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(["rev-parse", "--is-inside-work-tree"])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if !ok {
            return Vec::new();
        }
    }

    let fmt = "%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1e";
    let out = match Command::new("git")
        .arg("-C")
        .arg(&root)
        .args([
            "log",
            &format!("--since={} days ago", since_days.max(1)),
            &format!("-n{}", max_commits.max(1) * 2),
            "--no-merges",
            &format!("--format={}", fmt),
        ])
        .output()
    {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).to_string(),
        _ => return Vec::new(),
    };

    let mut records = Vec::new();
    for raw_rec in out.split('\x1e') {
        let raw_rec = raw_rec.trim();
        if raw_rec.is_empty() {
            continue;
        }
        let parts: Vec<&str> = raw_rec.split('\x1f').collect();
        if parts.len() < 4 {
            continue;
        }
        let full_sha = parts[0].trim();
        let iso_date = parts[1].trim();
        let author_name = parts[2].trim();
        let subject = parts[3].trim();
        let body = if parts.len() > 4 { parts[4].trim() } else { "" };

        if subject.len() < 10 || TRIVIAL_COMMIT_RE.is_match(subject) {
            continue;
        }
        let is_conv = CONVENTIONAL_RE.is_match(subject);
        let has_signal = SIGNAL_COMMIT_RE.is_match(subject) || SIGNAL_COMMIT_RE.is_match(body);
        let has_body = !body.is_empty() && body.len() >= 25;
        if !(is_conv || has_signal || has_body) {
            continue;
        }

        let obs_dt = DateTime::parse_from_rfc3339(iso_date)
            .map(|dt| dt.with_timezone(&Utc))
            .unwrap_or_else(|_| Utc::now());

        let combined = if body.is_empty() {
            subject.to_string()
        } else {
            format!("{}\n{}", subject, body)
        };
        let mtype = if FIX_COMMIT_RE.is_match(&combined) {
            "fix"
        } else if DECISION_COMMIT_RE.is_match(&combined) {
            "decision"
        } else {
            "fact"
        };

        let short_sha: String = full_sha.chars().take(7).collect();
        let date_str = obs_dt.format("%Y-%m-%d").to_string();
        let trimmed_body: String = body.chars().take(500).collect::<String>().trim().to_string();
        let text = if trimmed_body.is_empty() {
            format!("[git {}] ({}) {}", short_sha, date_str, subject)
        } else {
            format!(
                "[git {}] ({}) {}\n{}",
                short_sha, date_str, subject, trimmed_body
            )
        };

        records.push(GitCommitRecord {
            sha: short_sha,
            full_sha: full_sha.to_string(),
            observed_at: obs_dt.to_rfc3339(),
            author_name: author_name.to_string(),
            subject: subject.to_string(),
            body: trimmed_body,
            memory_type: mtype.to_string(),
            speaker: "git".to_string(),
            text,
        });
        if records.len() >= max_commits {
            break;
        }
    }
    records
}

pub fn hydrate_repo<F>(
    repo_root: &Path,
    namespace: Option<&str>,
    since_days: usize,
    max_commits: usize,
    include_rules: bool,
    include_git: bool,
    extract: bool,
    dry_run: bool,
    mut post_remember: F,
) -> Value
where
    F: FnMut(Value) -> Option<Value>,
{
    let root = fs::canonicalize(repo_root).unwrap_or_else(|_| repo_root.to_path_buf());
    let rules = if include_rules {
        parse_repo_rules(&root, 50)
    } else {
        Vec::new()
    };
    let commits = if include_git {
        parse_git_history(&root, since_days, max_commits)
    } else {
        Vec::new()
    };

    let mut rules_written = 0usize;
    let mut commits_written = 0usize;
    let mut retired_total = 0usize;

    if !dry_run {
        for r in &rules {
            let mut body = json!({
                "text": r.text,
                "speaker": "user",
                "type": "constraint",
                "constraint_subject": r.constraint_subject,
                "extract": false,
            });
            if let Some(ns) = namespace {
                body["namespace"] = json!(ns);
            }
            if let Some(res) = post_remember(body) {
                if res.get("turn_id").is_some()
                    || res.get("queued").and_then(|v| v.as_bool()) == Some(true)
                {
                    rules_written += 1;
                }
                if let Some(arr) = res.get("constraints_retired").and_then(|v| v.as_array()) {
                    retired_total += arr.len();
                }
            }
        }

        for c in commits.iter().rev() {
            let mut body = json!({
                "text": c.text,
                "speaker": "git",
                "type": c.memory_type,
                "session_id": format!("git-init:{}", c.sha),
                "extract": extract,
            });
            if let Some(ns) = namespace {
                body["namespace"] = json!(ns);
            }
            if let Some(res) = post_remember(body) {
                if res.get("turn_id").is_some()
                    || res.get("queued").and_then(|v| v.as_bool()) == Some(true)
                {
                    commits_written += 1;
                }
            }
        }
    }

    let mut sources: Vec<String> = rules
        .iter()
        .map(|r| r.source_file.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    sources.sort();

    json!({
        "repo_root": root.to_string_lossy(),
        "namespace": namespace,
        "dry_run": dry_run,
        "rules_found": rules.len(),
        "rules_written": rules_written,
        "constraints_retired": retired_total,
        "commits_found": commits.len(),
        "commits_written": commits_written,
        "rule_sources": sources,
        "rules": rules,
        "commits": commits,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_deterministic_subject_idempotency() {
        let s1 = deterministic_rule_subject("CLAUDE.md", "Never commit plaintext secrets to git.");
        let s2 =
            deterministic_rule_subject("claude.md", "  never commit   plaintext secrets to git. ");
        assert_eq!(s1, s2);
        assert!(s1.starts_with("init:claude-md:never-commit-plainte"));
    }
}
