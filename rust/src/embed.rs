//! Multi-mode Embedding Engine (`embed.rs`):
//! - Default keyless mode (no external AI): Local neural `intfloat/multilingual-e5-small` (384-d,
//!   100+ languages), with automatic backward-compatible preservation of `BAAI/bge-small-en-v1.5`
//!   when opening pre-existing 384-d English databases.
//! - Option B cloud mode (`OPENAI_API_KEY` set): OpenAI `text-embedding-3-small` (1536-d).

use reqwest::Client;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::env;
use std::sync::{Mutex, RwLock};
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

pub fn canonical_embed_family(model: &str) -> &str {
    let m = model.trim();
    if m.eq_ignore_ascii_case("BAAI/bge-small-en-v1.5")
        || m.eq_ignore_ascii_case("bge-small-en-v1.5")
        || m.eq_ignore_ascii_case("local-384")
    {
        "BAAI/bge-small-en-v1.5"
    } else if m.eq_ignore_ascii_case("intfloat/multilingual-e5-small")
        || m.eq_ignore_ascii_case("multilingual-e5-small")
    {
        "intfloat/multilingual-e5-small"
    } else {
        m
    }
}

pub struct Embedder {
    pub dim: usize,
    model_inner: RwLock<String>,
    pub explicit_local_override: bool,
    openai_key: Option<String>,
    base_url: String,
    http: Client,
    cache: QueryEmbedCache,
    #[cfg(test)]
    test_synthetic: bool,
}

impl Embedder {
    pub fn from_env(openai_key: Option<String>, force_dim: Option<usize>) -> Self {
        let key = openai_key
            .or_else(|| env::var("OPENAI_API_KEY").ok())
            .filter(|s| !s.trim().is_empty() && !s.starts_with("secret://"));
        let dim = force_dim.unwrap_or(if key.is_some() { 1536 } else { 384 });
        let (local_default, explicit_local) = crate::rerank::default_local_embed_model_from_env();
        let model = if key.is_some() && dim == 1536 {
            env::var("MEMNOS_OPENAI_EMBEDDING")
                .unwrap_or_else(|_| "text-embedding-3-small".to_string())
        } else if dim == 384 {
            crate::rerank::set_active_reranker_for_embed_model(&local_default);
            local_default
        } else {
            format!("unsupported-dim-{}", dim)
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
            model_inner: RwLock::new(model),
            explicit_local_override: explicit_local,
            openai_key: key,
            base_url,
            http,
            cache: QueryEmbedCache::new(ttl_s, 1024),
            #[cfg(test)]
            test_synthetic: false,
        }
    }

    #[cfg(test)]
    pub fn synthetic_for_test(dim: usize, model_tag: &str) -> Self {
        Self {
            dim,
            model_inner: RwLock::new(model_tag.to_string()),
            explicit_local_override: true,
            openai_key: None,
            base_url: String::new(),
            http: Client::new(),
            cache: QueryEmbedCache::new(60.0, 256),
            test_synthetic: true,
        }
    }

    pub fn model_name(&self) -> String {
        self.model_inner
            .read()
            .map(|g| g.clone())
            .unwrap_or_else(|_| crate::rerank::E5_MULTI_REPO_ID.to_string())
    }

    pub fn switch_local_model(&self, new_model: &str) {
        if let Ok(mut g) = self.model_inner.write() {
            *g = new_model.to_string();
        }
        crate::rerank::set_active_reranker_for_embed_model(new_model);
    }

    pub fn is_openai(&self) -> bool {
        self.openai_key.is_some() && self.dim == 1536
    }

    pub async fn try_embed(&self, text: &str) -> Result<(Vec<f32>, u32), String> {
        self.try_embed_with_role(text, false).await
    }

    pub async fn try_embed_query(&self, text: &str) -> Result<(Vec<f32>, u32), String> {
        self.try_embed_with_role(text, true).await
    }

    async fn try_embed_with_role(
        &self,
        text: &str,
        is_query: bool,
    ) -> Result<(Vec<f32>, u32), String> {
        let clamped: String = text.chars().take(16_000).collect();
        let active_model = self.model_name();
        let cache_tag = if is_query {
            format!("{}:q", active_model)
        } else {
            format!("{}:p", active_model)
        };
        if let Some(hit) = self.cache.get(&clamped, &cache_tag) {
            return Ok((hit, 0));
        }
        #[cfg(test)]
        if self.test_synthetic {
            use sha2::{Digest, Sha256};
            let mut vec = vec![0.0f32; self.dim];
            for (i, w) in clamped
                .to_ascii_lowercase()
                .split(|c: char| !c.is_ascii_alphanumeric())
                .filter(|s| !s.is_empty())
                .enumerate()
            {
                let d = Sha256::digest(format!("{}:{}", i, w).as_bytes());
                let idx = u32::from_le_bytes([d[0], d[1], d[2], d[3]]) as usize % self.dim;
                vec[idx] += 1.0;
            }
            let norm: f32 = vec.iter().map(|x| x * x).sum::<f32>().sqrt();
            if norm > 1e-8 {
                for x in &mut vec {
                    *x /= norm;
                }
            } else if !vec.is_empty() {
                vec[0] = 1.0;
            }
            return Ok((vec, 0));
        }
        if let Some(ref key) = self.openai_key {
            if self.dim == 1536 {
                let resp = self
                    .http
                    .post(format!("{}/embeddings", self.base_url))
                    .bearer_auth(key)
                    .json(&json!({
                        "model": active_model,
                        "input": clamped,
                    }))
                    .send()
                    .await
                    .map_err(|e| format!("OpenAI embeddings request failed: {}", e))?;
                let status = resp.status();
                if !status.is_success() {
                    let body = resp.text().await.unwrap_or_default();
                    return Err(format!(
                        "OpenAI embeddings returned HTTP {}: {}",
                        status,
                        body.chars().take(300).collect::<String>()
                    ));
                }
                let val = resp
                    .json::<Value>()
                    .await
                    .map_err(|e| format!("OpenAI embeddings JSON parse error: {}", e))?;
                let arr = val
                    .get("data")
                    .and_then(|d| d.get(0))
                    .and_then(|d| d.get("embedding"))
                    .and_then(|e| e.as_array())
                    .ok_or_else(|| "OpenAI embeddings response missing data[0].embedding".to_string())?;
                let vec: Vec<f32> = arr
                    .iter()
                    .map(|x| x.as_f64().unwrap_or(0.0) as f32)
                    .collect();
                if vec.len() != self.dim {
                    return Err(format!(
                        "OpenAI embeddings dimension mismatch: expected {}, got {}",
                        self.dim,
                        vec.len()
                    ));
                }
                let toks = val
                    .get("usage")
                    .and_then(|u| u.get("prompt_tokens"))
                    .and_then(|t| t.as_u64())
                    .unwrap_or(0) as u32;
                self.cache.put(&clamped, &cache_tag, vec.clone());
                return Ok((vec, toks));
            }
        }
        if self.dim == 384 {
            let clamped_owned = clamped.clone();
            let model_for_task = active_model.clone();
            let v = tokio::task::spawn_blocking(move || {
                crate::rerank::global_local_embed(&model_for_task, &clamped_owned, is_query)
            })
            .await
            .map_err(|e| format!("local embedding task join error: {}", e))?
            .map_err(|e| {
                format!(
                    "local embedding model '{}' failed: {} (run `memnos models pull` while online to download required weights, or configure OPENAI_API_KEY)",
                    active_model, e
                )
            })?;
            if v.len() != 384 {
                return Err(format!(
                    "local embedding model '{}' returned {}-d vector (expected 384)",
                    active_model,
                    v.len()
                ));
            }
            self.cache.put(&clamped, &cache_tag, v.clone());
            return Ok((v, 0));
        }
        Err(format!(
            "no embedder configured for {}-d vectors (provide OPENAI_API_KEY for 1536-d or use 384-d local models)",
            self.dim
        ))
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
