//! Offline write-behind queue & last-synced recall snapshot (`offline_queue`).

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::fs;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueuedWrite {
    pub namespace: String,
    pub text: String,
    pub speaker: String,
    pub queued_at: f64,
    #[serde(default)]
    pub token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecallSnapshot {
    pub namespace: String,
    pub context: String,
    pub mem_count: usize,
    pub saved_at: String,
    pub saved_epoch: f64,
}

fn safe_ns(ns: &str) -> String {
    ns.chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-') { c } else { '_' })
        .collect()
}

pub fn enqueue(base_dir: &Path, namespace: &str, text: &str, speaker: &str, token: &str) {
    let qdir = base_dir.join("offline_queue");
    if fs::create_dir_all(&qdir).is_err() {
        return;
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let fname = format!("{:.6}-{}.json", now, std::process::id());
    let item = QueuedWrite {
        namespace: namespace.to_string(),
        text: text.to_string(),
        speaker: speaker.to_string(),
        queued_at: now,
        token: token.to_string(),
    };
    if let Ok(raw) = serde_json::to_string(&item) {
        let _ = fs::write(qdir.join(fname), raw);
    }
}

pub fn save_snapshot(base_dir: &Path, namespace: &str, context: &str, mem_count: usize) {
    let sdir = base_dir.join("snapshots");
    if fs::create_dir_all(&sdir).is_err() {
        return;
    }
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let snap = RecallSnapshot {
        namespace: namespace.to_string(),
        context: context.to_string(),
        mem_count,
        saved_at: Utc::now().format("%Y-%m-%d %H:%M UTC").to_string(),
        saved_epoch: epoch,
    };
    if let Ok(raw) = serde_json::to_string(&snap) {
        let _ = fs::write(sdir.join(format!("{}.json", safe_ns(namespace))), raw);
    }
}

pub fn load_snapshot(base_dir: &Path, namespace: &str) -> Option<RecallSnapshot> {
    let p = base_dir.join("snapshots").join(format!("{}.json", safe_ns(namespace)));
    let raw = fs::read_to_string(p).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn drain(base_dir: &Path, url: &str, fallback_token: &str, timeout_secs: u64) -> (usize, usize) {
    let qdir = base_dir.join("offline_queue");
    let Ok(entries) = fs::read_dir(&qdir) else {
        return (0, 0);
    };
    let mut files: Vec<_> = entries
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
        .collect();
    files.sort();

    let mut drained = 0usize;
    let mut rejected = 0usize;

    for path in files {
        let claiming = path.with_extension("claiming");
        if fs::rename(&path, &claiming).is_err() {
            continue;
        }
        let Ok(raw) = fs::read_to_string(&claiming) else {
            let _ = fs::remove_file(&claiming);
            continue;
        };
        let Ok(item) = serde_json::from_str::<QueuedWrite>(&raw) else {
            let _ = fs::rename(&claiming, path.with_extension("rejected"));
            rejected += 1;
            continue;
        };
        let tok = if !item.token.is_empty() {
            item.token.as_str()
        } else {
            fallback_token
        };
        let mut req = ureq::post(&format!("{}/remember", url.trim_end_matches('/')))
            .timeout(Duration::from_secs(timeout_secs))
            .set("Content-Type", "application/json");
        if !tok.is_empty() {
            req = req.set("Authorization", &format!("Bearer {}", tok));
        }
        let body = json!({
            "namespace": item.namespace,
            "text": item.text,
            "speaker": item.speaker,
            "queued_at": item.queued_at,
            "async": true
        });
        match req.send_json(body) {
            Ok(_) => {
                let _ = fs::remove_file(&claiming);
                drained += 1;
            }
            Err(ureq::Error::Status(code, _)) if (400..500).contains(&code) && code != 429 => {
                let _ = fs::rename(&claiming, path.with_extension("rejected"));
                rejected += 1;
            }
            Err(_) => {
                let _ = fs::rename(&claiming, &path);
                break;
            }
        }
    }
    (drained, rejected)
}
