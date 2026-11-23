//! Temporal query analysis & event-date parsing (`core/temporal.py` parity).

use chrono::{DateTime, Datelike, NaiveDate, TimeZone, Utc};
use once_cell::sync::Lazy;
use regex::Regex;
use std::collections::HashSet;
use std::env;

#[derive(Debug, Clone, Default)]
pub struct TemporalIntent {
    pub temporal: bool,
    pub current: bool,
    pub order: Option<&'static str>,
    pub start: Option<DateTime<Utc>>,
    pub end: Option<DateTime<Utc>>,
    pub matched_pattern: Option<String>,
}

static IS_TEMPORAL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(when|what year|what month|what day|how long|how many (days|weeks|months|years)|before|after|first|last|earliest|latest|recent|recently|ago|date|since|until|current(ly)?|now|these days|nowadays|january|february|march|april|may|june|july|august|september|october|november|december|anymore|previous(ly)?|earlier|still|sequence|timeline|progression|outdated|deprecated|newest|evolve[ds]?|evolving|used to|no longer|what changed|history of|over time|back when|back in|at the time|prior to|subsequent to|when did|latest version|out of date)\b|\bchronolog").unwrap()
});

static CURRENT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(current(ly)?|now|these days|nowadays|latest|most recent|still)\b").unwrap()
});

static ORDER_FIRST_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(first|earliest|initial(ly)?|originally|begin)\b").unwrap());
static ORDER_LAST_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)\b(last|latest|most recent|final|recently)\b").unwrap());
static YEAR_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b(19|20)\d\d\b").unwrap());
static ISO_DATE_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b(\d{4})-(\d{2})-(\d{2})\b").unwrap());
static PROPER_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\b[A-Z][a-zA-Z]{2,}\b").unwrap());

const QSTOP: &[&str] = &[
    "What", "When", "Where", "Which", "Who", "Why", "How", "Did", "Does", "Has", "Have", "Was",
    "Were", "Is", "Are", "The", "Tell", "Give", "List",
];

const MONTHS: &[(&str, u32)] = &[
    ("january", 1),
    ("february", 2),
    ("march", 3),
    ("april", 4),
    ("may", 5),
    ("june", 6),
    ("july", 7),
    ("august", 8),
    ("september", 9),
    ("october", 10),
    ("november", 11),
    ("december", 12),
];

pub fn parse_event_date(text: &str, fallback: Option<DateTime<Utc>>) -> Option<DateTime<Utc>> {
    if let Some(cap) = ISO_DATE_RE.captures(text) {
        let y: i32 = cap[1].parse().ok()?;
        let m: u32 = cap[2].parse().ok()?;
        let d: u32 = cap[3].parse().ok()?;
        if let Some(nd) = NaiveDate::from_ymd_opt(y, m, d) {
            if let Some(ndt) = nd.and_hms_opt(0, 0, 0) {
                return Some(Utc.from_utc_datetime(&ndt));
            }
        }
    }
    fallback
}

pub fn query_entities(query: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for m in PROPER_RE.find_iter(query) {
        let s = m.as_str();
        if QSTOP.contains(&s) || !seen.insert(s.to_ascii_lowercase()) {
            continue;
        }
        out.push(s.to_string());
    }
    out
}

pub fn analyze(query: &str, now: DateTime<Utc>) -> TemporalIntent {
    let mut t = TemporalIntent::default();
    let low = query.to_ascii_lowercase();
    let year: Option<i32> = YEAR_RE
        .find(query)
        .and_then(|m| m.as_str().parse::<i32>().ok());
    let month: Option<u32> = MONTHS.iter().find_map(|(name, num)| {
        let re = Regex::new(&format!(r"\b{}\b", name)).ok()?;
        if re.is_match(&low) {
            Some(*num)
        } else {
            None
        }
    });

    let auto_arm = env::var("MEMNOS_TEMPORAL_AUTO_ARM").unwrap_or_else(|_| "1".to_string()) != "0";
    if auto_arm {
        if let Some(m) = IS_TEMPORAL_RE.find(query) {
            t.temporal = true;
            t.matched_pattern = Some(m.as_str().to_ascii_lowercase());
        }
    }
    if year.is_some() || month.is_some() {
        t.temporal = true;
        if t.matched_pattern.is_none() {
            t.matched_pattern = Some(match (year, month) {
                (Some(y), _) => format!("year={}", y),
                (_, Some(m)) => format!("month={}", m),
                _ => String::new(),
            });
        }
    }
    if !t.temporal {
        return t;
    }
    t.current = CURRENT_RE.is_match(query);
    if ORDER_FIRST_RE.is_match(query) {
        t.order = Some("asc");
    } else if ORDER_LAST_RE.is_match(query) {
        t.order = Some("desc");
    }
    if let (Some(y), Some(m)) = (year, month) {
        if let Some(nd) = NaiveDate::from_ymd_opt(y, m, 1) {
            t.start = Some(Utc.from_utc_datetime(&nd.and_hms_opt(0, 0, 0).unwrap()));
            let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
            if let Some(end_nd) = NaiveDate::from_ymd_opt(ny, nm, 1) {
                t.end = Some(Utc.from_utc_datetime(&end_nd.and_hms_opt(0, 0, 0).unwrap()));
            }
        }
    } else if let Some(y) = year {
        if let (Some(s), Some(e)) = (
            NaiveDate::from_ymd_opt(y, 1, 1),
            NaiveDate::from_ymd_opt(y + 1, 1, 1),
        ) {
            t.start = Some(Utc.from_utc_datetime(&s.and_hms_opt(0, 0, 0).unwrap()));
            t.end = Some(Utc.from_utc_datetime(&e.and_hms_opt(0, 0, 0).unwrap()));
        }
    } else if let Some(m) = month {
        let y = if m <= now.month() {
            now.year()
        } else {
            now.year() - 1
        };
        if let Some(nd) = NaiveDate::from_ymd_opt(y, m, 1) {
            t.start = Some(Utc.from_utc_datetime(&nd.and_hms_opt(0, 0, 0).unwrap()));
            let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
            if let Some(end_nd) = NaiveDate::from_ymd_opt(ny, nm, 1) {
                t.end = Some(Utc.from_utc_datetime(&end_nd.and_hms_opt(0, 0, 0).unwrap()));
            }
        }
    }
    t
}

/// Semver parser `(major, minor, patch)` for `/corpus/check` & `/corpus/deviation` version bounds.
pub fn parse_semver(raw: &str) -> Result<(u32, u32, u32), String> {
    let s = raw.trim().trim_start_matches('v').trim_start_matches('V');
    let core = s.split(['-', '+']).next().unwrap_or("");
    let parts: Vec<&str> = core.split('.').collect();
    if parts.len() < 2 || parts.len() > 3 {
        return Err(format!("invalid semver {:?} (expected X.Y or X.Y.Z)", raw));
    }
    let maj: u32 = parts[0]
        .parse()
        .map_err(|_| format!("invalid major version in {:?}", raw))?;
    let min: u32 = parts[1]
        .parse()
        .map_err(|_| format!("invalid minor version in {:?}", raw))?;
    let pat: u32 = if parts.len() == 3 {
        parts[2]
            .parse()
            .map_err(|_| format!("invalid patch version in {:?}", raw))?
    } else {
        0
    };
    Ok((maj, min, pat))
}
