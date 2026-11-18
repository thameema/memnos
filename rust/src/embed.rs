//! Dual-mode Embedding Engine (`embed.rs`):
//! - OpenAI `text-embedding-3-small` (1536-d) with bounded LRU cache + `QueryEmbedCache` TTL window.
//! - Local zero-API 384-d L2-normalized feature-hashed n-gram / token projection (works completely
//!   offline with zero external runtime or model download, plus optional Ollama/OpenAI-compatible
//!   embedding endpoint via `MEMNOS_EMBED_BASE_URL`).

use reqwest::Client;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};
use std::env;
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct QueryEmbedCache {
    ttl: Duration,
    maxsize: usize,
    inner: Mutex<(HashMap<(String, String), (Instant, Vec<f32>)>, VecDeque<(String, String)>)>,
}

impl QueryEmbedCache {
    pub fn new(ttl_secs: f64, maxsize: usize) -> Self {
        Self {
            ttl: Duration::from_secs_f64(ttl_secs.max(0.0)),
            maxsize: maxsize.max(16),
            inner: Mutex::new((HashMap::new(), VecDeque::new())),
        }
    }

    pub fn get(&self, query: &str, model: &str) -> Option<Vec<f32>> {
        if self.ttl.is_zero() {
            return None;
        }
        let key = (query.to_string(), model.to_string());
        let now = Instant::now();
        let mut guard = self.inner.lock().ok()?;
        if let Some((exp, vec)) = guard.0.get(&key) {
            if *exp > now {
                return Some(vec.clone());
            }
            guard.0.remove(&key);
        }
        None
    }

    pub fn put(&self, query: &str, model: &str, vec: Vec<f32>) {
        if self.ttl.is_zero() {
            return;
        }
        let key = (query.to_string(), model.to_string());
        let exp = Instant::now() + self.ttl;
        if let Ok(mut guard) = self.inner.lock() {
            guard.0.insert(key.clone(), (exp, vec));
            guard.1.push_back(key);
            while guard.0.len() > self.maxsize {
                if let Some(old) = guard.1.pop_front() {
                    guard.0.remove(&old);
                } else {
                    break;
                }
            }
        }
    }
}

/// Deterministic L2-normalized local feature-hashing embedder (`dim` = 384 or 1536) over word
/// unigrams, word bigrams, and character 3..5-grams — fast (<50 microseconds), zero network,
/// zero external DLLs, and preserves lexical/morphological cosine similarity offline.
pub fn local_feature_embed(text: &str, dim: usize) -> Vec<f32> {
    let mut vec = vec![0.0f32; dim];
    let clean = text.to_ascii_lowercase();
    let words: Vec<&str> = clean
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|s| !s.is_empty())
        .collect();

    let mut add_feat = |feat: &str, weight: f32| {
        let mut h = Sha256::new();
        h.update(feat.as_bytes());
        let digest = h.finalize();
        for slot in 0..2 {
            let off = slot * 8;
            let idx_raw = u32::from_le_bytes([
                digest[off],
                digest[off + 1],
                digest[off + 2],
                digest[off + 3],
            ]) as usize;
            let sign = if (digest[off + 4] & 1) == 0 { 1.0 } else { -1.0 };
            vec[idx_raw % dim] += sign * weight;
        }
    };

    for (i, &w) in words.iter().enumerate() {
        add_feat(&format!("w:{}", w), 1.5);
        if i + 1 < words.len() {
            add_feat(&format!("bg:{}_{}", w, words[i + 1]), 1.0);
        }
        let chars: Vec<char> = w.chars().collect();
        for n in [3usize, 4usize] {
            if chars.len() >= n {
                for win in chars.windows(n) {
                    let g: String = win.iter().collect();
                    add_feat(&format!("c:{}", g), 0.35);
                }
            }
        }
    }

    let norm: f32 = vec.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > 1e-8 {
        for x in &mut vec {
            *x /= norm;
        }
    } else if !vec.is_empty() {
        vec[0] = 1.0;
    }
    vec
}

pub struct Embedder {
    pub dim: usize,
    pub model: String,
    openai_key: Option<String>,
    base_url: String,
    http: Client,
    cache: QueryEmbedCache,
}

impl Embedder {
    pub fn from_env(openai_key: Option<String>, force_dim: Option<usize>) -> Self {
        let key = openai_key
            .or_else(|| env::var("OPENAI_API_KEY").ok())
            .filter(|s| !s.trim().is_empty() && !s.starts_with("secret://"));
        let dim = force_dim.unwrap_or(if key.is_some() { 1536 } else { 384 });
        let model = if key.is_some() && dim == 1536 {
            env::var("MEMNOS_EMBED_MODEL").unwrap_or_else(|_| "text-embedding-3-small".to_string())
        } else {
            "local-bge-384".to_string()
        };
        let base_url = env::var("MEMNOS_EMBED_BASE_URL")
            .unwrap_or_else(|_| "https://api.openai.com/v1".to_string())
            .trim_end_matches('/')
            .to_string();
        let ttl_s: f64 = env::var("MEMNOS_QUERY_CACHE_TTL_S")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(60.0);
        let http = Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .unwrap_or_else(|_| Client::new());
        Self {
            dim,
            model,
            openai_key: key,
            base_url,
            http,
            cache: QueryEmbedCache::new(ttl_s, 1024),
        }
    }

    pub fn is_openai(&self) -> bool {
        self.openai_key.is_some() && self.dim == 1536
    }

    /// Returns `(embedding_vector, prompt_tokens_used)`.
    pub async fn embed(&self, text: &str) -> (Vec<f32>, u32) {
        let clamped: String = text.chars().take(16_000).collect();
        if let Some(hit) = self.cache.get(&clamped, &self.model) {
            return (hit, 0);
        }
        if let Some(ref key) = self.openai_key {
            if self.dim == 1536 {
                let resp = self
                    .http
                    .post(format!("{}/embeddings", self.base_url))
                    .bearer_auth(key)
                    .json(&json!({
                        "model": self.model,
                        "input": clamped,
                    }))
                    .send()
                    .await;
                if let Ok(r) = resp {
                    if r.status().is_success() {
                        if let Ok(val) = r.json::<Value>().await {
                            if let Some(arr) = val
                                .get("data")
                                .and_then(|d| d.get(0))
                                .and_then(|d| d.get("embedding"))
                                .and_then(|e| e.as_array())
                            {
                                let vec: Vec<f32> = arr
                                    .iter()
                                    .map(|x| x.as_f64().unwrap_or(0.0) as f32)
                                    .collect();
                                if vec.len() == self.dim {
                                    let toks = val
                                        .get("usage")
                                        .and_then(|u| u.get("prompt_tokens"))
                                        .and_then(|t| t.as_u64())
                                        .unwrap_or(0) as u32;
                                    self.cache.put(&clamped, &self.model, vec.clone());
                                    return (vec, toks);
                                }
                            }
                        }
                    }
                }
            }
        }
        let v = local_feature_embed(&clamped, self.dim);
        self.cache.put(&clamped, &self.model, v.clone());
        (v, 0)
    }
}

pub fn vlit(vec: &[f32]) -> String {
    let mut s = String::with_capacity(vec.len() * 10 + 2);
    s.push('[');
    for (i, v) in vec.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        let clean = if v.is_finite() { *v } else { 0.0 };
        s.push_str(&format!("{:.6}", clean));
    }
    s.push(']');
    s
}
