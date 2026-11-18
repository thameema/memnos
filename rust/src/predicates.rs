//! Predicate classification for bi-temporal supersession & contradiction detection (`core/predicates.py`).

use once_cell::sync::Lazy;
use regex::Regex;
use std::collections::HashSet;

const SINGLE_VALUED_CUES: &[&str] = &[
    "live",
    "reside",
    "home",
    "based",
    "located",
    "current_",
    "work_at",
    "works_at",
    "employ",
    "employer",
    "job_title",
    "occupation",
    "role_at",
    "age",
    "marital",
    "married",
    "spouse",
    "status",
    "blocked",
    "runs_on",
    "can_handle",
    "capacity",
    "deployed_",
    "version",
    "recommended_action",
    "quota",
    "threshold",
    "timeout",
    "max_",
    "min_",
    "count_of",
];

const SINGLE_VALUED_EXACT: &[&str] = &["uses", "recommended", "recommendation"];
const SINGLE_VALUED_TOKENS: &[&str] = &["rate", "limit"];

const MULTI_VALUED_TOKENS: &[&str] = &[
    "visited",
    "visit",
    "did",
    "met",
    "meets",
    "likes",
    "liked",
    "like",
    "owns",
    "own",
    "attended",
    "attends",
    "watched",
    "read",
    "tried",
    "went",
    "ate",
    "played",
    "bought",
    "experienced",
    "activity",
    "hobby",
    "enjoys",
];

static QUANTIFIED_OBJ_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^\s*(?:~|≈|<=|>=|<|>)?\s*[$€£]?\d[\d,.]*\s*(?:[a-zA-Z%/][\w\s/%.-]*)?$").unwrap()
});

static SPLIT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

pub fn pred_tokens(pred: &str) -> HashSet<String> {
    SPLIT_RE
        .split(&pred.to_ascii_lowercase())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

pub fn is_single_valued(pred: &str) -> bool {
    if pred.is_empty() {
        return false;
    }
    let p = pred.to_ascii_lowercase();
    if SINGLE_VALUED_EXACT.contains(&p.as_str()) {
        return true;
    }
    if SINGLE_VALUED_CUES.iter().any(|cue| p.contains(cue)) {
        return true;
    }
    let toks = pred_tokens(&p);
    SINGLE_VALUED_TOKENS.iter().any(|t| toks.contains(*t))
}

pub fn supersedable(pred: &str, obj: Option<&str>) -> bool {
    if pred.is_empty() {
        return false;
    }
    if is_single_valued(pred) {
        return true;
    }
    if let Some(o) = obj {
        if QUANTIFIED_OBJ_RE.is_match(o) {
            let toks = pred_tokens(pred);
            return !MULTI_VALUED_TOKENS.iter().any(|m| toks.contains(*m));
        }
    }
    false
}
