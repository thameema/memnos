//! Local Neural Models (`rerank.rs`):
//! - Default Multilingual Suite (100+ languages, out of the box for new keyless installs):
//!   * Embedder: `intfloat/multilingual-e5-small` (384-d BERT + SentencePiece Unigram Viterbi
//!     tokenizer, `"query: "` / `"passage: "` prefix + mean-pooling + L2 normalization).
//!   * Reranker: `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` (multilingual XLM-RoBERTa sequence
//!     classifier + sigmoid).
//! - Backward-Compatible English Suite (auto-preserved on pre-existing 384-d `bge-small` DBs):
//!   * Embedder: `BAAI/bge-small-en-v1.5` (384-d BERT + WordPiece, `[CLS]` + L2 normalization).
//!   * Reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` (6-layer BERT classifier + sigmoid).
//!
//! Zero C/C++ / ONNX Runtime `.dll`/`.dylib` dependencies: compiled statically via `candle`.
//! Post-install download UX: notifies the user clearly when downloading required local weights to
//! `~/.memnos/models/` with live streaming MB/% progress and pinned SHA-256 verification.

use crate::config::config_dir;
use candle_core::{DType, Device, IndexOp, Module, Tensor};
use candle_nn::{embedding, layer_norm, linear, Embedding, LayerNorm, Linear, VarBuilder};
use candle_transformers::models::bert::{BertEncoder, Config as BertConfig};
use memmap2::Mmap;
use once_cell::sync::{Lazy, OnceCell};
use serde::de::{DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::env;
use std::fmt;
use std::fs;
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub const E5_MULTI_REPO_ID: &str = "intfloat/multilingual-e5-small";
pub const E5_MULTI_SLUG: &str = "multilingual-e5-small";
pub const E5_MULTI_REVISION: &str = "614241f622f53c4eeff9890bdc4f31cfecc418b3";

pub const MMARCO_RERANK_REPO_ID: &str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1";
pub const MMARCO_RERANK_SLUG: &str = "mmarco-mMiniLMv2-L12-H384-v1";
pub const MMARCO_RERANK_REVISION: &str = "1427fd652930e4ba29e8149678df786c240d8825";

pub const BGE_REPO_ID: &str = "BAAI/bge-small-en-v1.5";
pub const BGE_SLUG: &str = "bge-small-en-v1.5";
pub const BGE_REVISION: &str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a";

pub const RERANK_REPO_ID: &str = "cross-encoder/ms-marco-MiniLM-L-6-v2";
pub const RERANK_SLUG: &str = "ms-marco-MiniLM-L-6-v2";
pub const RERANK_REVISION: &str = "c5ee24cb16019beea0893ab7796b1df96625c6b8";

pub struct PinnedModelSpec {
    pub repo_id: &'static str,
    pub slug: &'static str,
    pub revision: &'static str,
    pub approx_mb: u32,
    pub role_label: &'static str,
    pub files: &'static [(&'static str, &'static str)],
}

pub const E5_MULTI_SPEC: PinnedModelSpec = PinnedModelSpec {
    repo_id: E5_MULTI_REPO_ID,
    slug: E5_MULTI_SLUG,
    revision: E5_MULTI_REVISION,
    approx_mb: 465,
    role_label: "Multilingual Embeddings (100+ languages)",
    files: &[
        (
            "config.json",
            "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
        ),
        (
            "tokenizer.json",
            "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
        ),
        (
            "model.safetensors",
            "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
        ),
    ],
};

pub const MMARCO_RERANK_SPEC: PinnedModelSpec = PinnedModelSpec {
    repo_id: MMARCO_RERANK_REPO_ID,
    slug: MMARCO_RERANK_SLUG,
    revision: MMARCO_RERANK_REVISION,
    approx_mb: 465,
    role_label: "Multilingual Cross-Encoder Reranker",
    files: &[
        (
            "config.json",
            "cc2cfe51aa3fd759d21d21acf5dfd6994aa67a3c9210636d22e143699d336c77",
        ),
        (
            "tokenizer.json",
            "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
        ),
        (
            "model.safetensors",
            "5daeca2481a76b5976a2bdc32f0a78532b6716da4f8cd3ff59460ef8d2f359b4",
        ),
    ],
};

pub const BGE_SPEC: PinnedModelSpec = PinnedModelSpec {
    repo_id: BGE_REPO_ID,
    slug: BGE_SLUG,
    revision: BGE_REVISION,
    approx_mb: 133,
    role_label: "English Embeddings (legacy 384-d compatibility)",
    files: &[
        (
            "config.json",
            "094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750",
        ),
        (
            "tokenizer.json",
            "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        ),
        (
            "model.safetensors",
            "3c9f31665447c8911517620762200d2245a2518d6e7208acc78cd9db317e21ad",
        ),
    ],
};

pub const RERANK_SPEC: PinnedModelSpec = PinnedModelSpec {
    repo_id: RERANK_REPO_ID,
    slug: RERANK_SLUG,
    revision: RERANK_REVISION,
    approx_mb: 90,
    role_label: "English Cross-Encoder Reranker",
    files: &[
        (
            "config.json",
            "380e02c93f431831be65d99a4e7e5f67c133985bf2e77d9d4eba46847190bacc",
        ),
        (
            "tokenizer.json",
            "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        ),
        (
            "model.safetensors",
            "821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae",
        ),
    ],
};

static E5_INSTANCE: OnceCell<Result<E5MultilingualModel, String>> = OnceCell::new();
static BGE_INSTANCE: OnceCell<Result<BgeEmbedModel, String>> = OnceCell::new();
static MMARCO_RERANK_INSTANCE: OnceCell<Result<MmarcoCrossEncoder, String>> = OnceCell::new();
static MINILM_RERANK_INSTANCE: OnceCell<Result<MiniLmCrossEncoder, String>> = OnceCell::new();

static EMBED_READY: AtomicBool = AtomicBool::new(false);
static RERANK_READY: AtomicBool = AtomicBool::new(false);
static OPENAI_SKIP_UNCACHED_RERANK: AtomicBool = AtomicBool::new(false);
static ALLOW_MODEL_DOWNLOAD: AtomicBool = AtomicBool::new(false);
static PREWARM_STARTED: AtomicBool = AtomicBool::new(false);
static NOTICE_PRINTED: AtomicBool = AtomicBool::new(false);
static ACTIVE_RERANK_MULTILINGUAL: AtomicBool = AtomicBool::new(true);
static PREWARM_ERR: Lazy<Mutex<Option<String>>> = Lazy::new(|| Mutex::new(None));

pub fn set_allow_model_download(allow: bool) {
    ALLOW_MODEL_DOWNLOAD.store(allow, Ordering::Release);
}

pub fn offline_models_only() -> bool {
    matches!(
        env::var("MEMNOS_OFFLINE_MODELS")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub fn prewarm_error() -> Option<String> {
    PREWARM_ERR.lock().ok().and_then(|g| g.clone())
}

pub fn models_ready_for_server(is_openai: bool) -> bool {
    let emb_ok = is_openai || EMBED_READY.load(Ordering::Acquire);
    let rerank_ok = !rerank_enabled()
        || RERANK_READY.load(Ordering::Acquire)
        || (is_openai && OPENAI_SKIP_UNCACHED_RERANK.load(Ordering::Acquire));
    emb_ok && rerank_ok
}
static CALIBRATED_RERANK_CAP: AtomicUsize = AtomicUsize::new(12);
static DOWNLOAD_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));

pub fn default_local_embed_model_from_env() -> (String, bool) {
    if let Ok(raw) = env::var("MEMNOS_LOCAL_MODEL") {
        let v = raw.trim().to_ascii_lowercase();
        if !v.is_empty() {
            if matches!(
                v.as_str(),
                "bge" | "bge-small" | "english" | "en" | "baai/bge-small-en-v1.5"
            ) {
                return (BGE_REPO_ID.to_string(), true);
            }
            if matches!(
                v.as_str(),
                "multilingual" | "e5" | "multilingual-e5-small" | "intfloat/multilingual-e5-small"
            ) {
                return (E5_MULTI_REPO_ID.to_string(), true);
            }
        }
    }
    (E5_MULTI_REPO_ID.to_string(), false)
}

pub fn resolve_reranker_multilingual(embed_model: &str) -> bool {
    if let Ok(raw) = env::var("MEMNOS_RERANKER") {
        let v = raw.trim().to_ascii_lowercase();
        if !v.is_empty() {
            if v.contains("mmarco") || v.contains("multilingual") {
                return true;
            }
            if v.contains("minilm-l-6") || v.contains("ms-marco") || v == "english" || v == "en" {
                return false;
            }
        }
    }
    let is_local_e5 = embed_model.eq_ignore_ascii_case(E5_MULTI_REPO_ID)
        || embed_model.eq_ignore_ascii_case("multilingual-e5-small");
    if is_local_e5 {
        return true;
    }
    let is_local_bge = embed_model.eq_ignore_ascii_case(BGE_REPO_ID)
        || embed_model.eq_ignore_ascii_case("bge-small-en-v1.5")
        || embed_model.eq_ignore_ascii_case("local-384");
    if is_local_bge {
        return false;
    }
    // OpenAI 1536-d mode (`text-embedding-3-small`): default to `ms-marco-MiniLM-L-6-v2`
    // (LoCoMo 65% winner, 42 MB encoder, ~8x faster than 12-layer cross-encoders), falling back
    // seamlessly to cached `mmarco-mMiniLMv2` if only the multilingual bundle was pulled.
    if is_model_cached(&RERANK_SPEC) {
        false
    } else if is_model_cached(&MMARCO_RERANK_SPEC) {
        true
    } else {
        false
    }
}

pub fn active_reranker_spec(embed_model: &str) -> &'static PinnedModelSpec {
    if resolve_reranker_multilingual(embed_model) {
        &MMARCO_RERANK_SPEC
    } else {
        &RERANK_SPEC
    }
}

pub fn set_active_reranker_for_embed_model(embed_model: &str) {
    let is_multi = resolve_reranker_multilingual(embed_model);
    ACTIVE_RERANK_MULTILINGUAL.store(is_multi, Ordering::Release);
}

pub fn specs_for_embed_model(embed_model: &str) -> (&'static PinnedModelSpec, &'static PinnedModelSpec) {
    if embed_model.eq_ignore_ascii_case(BGE_REPO_ID)
        || embed_model.eq_ignore_ascii_case("bge-small-en-v1.5")
        || embed_model.eq_ignore_ascii_case("local-384")
    {
        (&BGE_SPEC, &RERANK_SPEC)
    } else {
        (&E5_MULTI_SPEC, &MMARCO_RERANK_SPEC)
    }
}

fn sha256_file_hex(path: &Path) -> Result<String, String> {
    let mut f = fs::File::open(path).map_err(|e| format!("open {}: {}", path.display(), e))?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut f, &mut hasher).map_err(|e| format!("read {}: {}", path.display(), e))?;
    Ok(hex::encode(hasher.finalize()))
}

fn verify_spec_dir(dir: &Path, spec: &PinnedModelSpec) -> Result<(), String> {
    for &(fname, expected_sha) in spec.files {
        let p = dir.join(fname);
        if !p.is_file() {
            return Err(format!("missing model file {}", p.display()));
        }
        let actual = sha256_file_hex(&p)?;
        if actual != expected_sha {
            return Err(format!(
                "SHA-256 mismatch for {} in {}: expected {}, got {}",
                fname,
                dir.display(),
                expected_sha,
                actual
            ));
        }
    }
    Ok(())
}

fn hf_cache_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Ok(hf_hub) = env::var("HF_HUB_CACHE") {
        if !hf_hub.trim().is_empty() {
            roots.push(PathBuf::from(hf_hub));
        }
    }
    if let Ok(hf_home) = env::var("HF_HOME") {
        if !hf_home.trim().is_empty() {
            roots.push(PathBuf::from(hf_home).join("hub"));
        }
    }
    if let Ok(home) = env::var("HOME").or_else(|_| env::var("USERPROFILE")) {
        if !home.trim().is_empty() {
            roots.push(PathBuf::from(home).join(".cache").join("huggingface").join("hub"));
        }
    }
    roots
}

fn has_all_files(dir: &Path, spec: &PinnedModelSpec) -> bool {
    spec.files.iter().all(|(f, _)| dir.join(f).is_file())
}

pub fn find_cached_model_dir(spec: &PinnedModelSpec) -> Option<PathBuf> {
    let local_dir = config_dir().join("models").join(spec.slug);
    if has_all_files(&local_dir, spec) {
        return Some(local_dir);
    }
    let folder_name = format!("models--{}", spec.repo_id.replace('/', "--"));
    for root in hf_cache_roots() {
        let model_root = root.join(&folder_name);
        if !model_root.is_dir() {
            continue;
        }
        let pinned_snap = model_root.join("snapshots").join(spec.revision);
        if has_all_files(&pinned_snap, spec) {
            return Some(pinned_snap);
        }
        if let Ok(entries) = fs::read_dir(model_root.join("snapshots")) {
            for ent in entries.flatten() {
                let p = ent.path();
                if p.is_dir() && has_all_files(&p, spec) {
                    return Some(p);
                }
            }
        }
    }
    None
}

pub fn is_model_cached(spec: &PinnedModelSpec) -> bool {
    find_cached_model_dir(spec).is_some()
}

fn print_download_banner_once(dest_root: &Path) {
    if NOTICE_PRINTED.swap(true, Ordering::AcqRel) {
        return;
    }
    eprintln!();
    eprintln!("╭──────────────────────────────────────────────────────────────────────────╮");
    eprintln!("│  memnos — One-Time Local Neural Model Download (No External AI Needed)   │");
    eprintln!("╰──────────────────────────────────────────────────────────────────────────╯");
    eprintln!("  No external AI API key is required: memnos runs embeddings and semantic");
    eprintln!("  reranking 100% locally on your machine so memory works offline at $0 cost.");
    eprintln!(
        "  Downloading required model weights to {} (one-time, SHA-256 verified):",
        dest_root.display()
    );
}

pub fn resolve_or_download_model(spec: &PinnedModelSpec) -> Result<PathBuf, String> {
    if let Some(found) = find_cached_model_dir(spec) {
        verify_spec_dir(&found, spec)?;
        return Ok(found);
    }
    let _guard = DOWNLOAD_LOCK.lock().map_err(|e| e.to_string())?;
    if let Some(found) = find_cached_model_dir(spec) {
        verify_spec_dir(&found, spec)?;
        return Ok(found);
    }
    if offline_models_only() {
        return Err(format!(
            "local model '{}' is not cached locally and MEMNOS_OFFLINE_MODELS=1 — run `memnos models pull --yes` while online or configure OPENAI_API_KEY",
            spec.repo_id
        ));
    }
    if !ALLOW_MODEL_DOWNLOAD.load(Ordering::Acquire) {
        return Err(format!(
            "local model '{}' is not downloaded yet — run `memnos setup embeddings` or `memnos models pull --yes`",
            spec.repo_id
        ));
    }

    let models_root = config_dir().join("models");
    let dest_dir = models_root.join(spec.slug);
    fs::create_dir_all(&dest_dir)
        .map_err(|e| format!("failed to create model dir {}: {}", dest_dir.display(), e))?;

    print_download_banner_once(&models_root);
    eprintln!(
        "  • {} — {} (~{} MB)",
        spec.role_label, spec.repo_id, spec.approx_mb
    );

    let base = env::var("HF_ENDPOINT")
        .unwrap_or_else(|_| "https://huggingface.co".to_string())
        .trim_end_matches('/')
        .to_string();

    for &(fname, expected_sha) in spec.files {
        let target = dest_dir.join(fname);
        if target.is_file() && sha256_file_hex(&target).ok().as_deref() == Some(expected_sha) {
            continue;
        }
        let url = format!("{}/{}/resolve/{}/{}", base, spec.repo_id, spec.revision, fname);
        let resp = ureq::get(&url)
            .timeout(Duration::from_secs(600))
            .call()
            .map_err(|e| format!("failed downloading {}: {}", url, e))?;
        let total_bytes: Option<u64> = resp
            .header("Content-Length")
            .and_then(|s| s.parse::<u64>().ok())
            .filter(|&n| n > 0);

        let tmp = dest_dir.join(format!("{}.tmp.{}", fname, std::process::id()));
        let mut out_file = fs::File::create(&tmp)
            .map_err(|e| format!("failed creating {}: {}", tmp.display(), e))?;
        let mut reader = resp.into_reader();
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 256 * 1024];
        let mut downloaded: u64 = 0;
        let show_progress = fname == "model.safetensors" || total_bytes.unwrap_or(0) > 5_000_000;
        let mut last_print = Instant::now();

        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| format!("network read error while downloading {}: {}", url, e))?;
            if n == 0 {
                break;
            }
            out_file
                .write_all(&buf[..n])
                .map_err(|e| format!("disk write error for {}: {}", tmp.display(), e))?;
            hasher.update(&buf[..n]);
            downloaded += n as u64;

            if show_progress && last_print.elapsed() >= Duration::from_millis(350) {
                last_print = Instant::now();
                if let Some(tot) = total_bytes {
                    let pct = ((downloaded as f64 / tot as f64) * 100.0).min(100.0);
                    eprint!(
                        "\r    ↓ {}/{}  {:.1} / {:.1} MB ({:.0}%)   ",
                        spec.slug,
                        fname,
                        downloaded as f64 / (1024.0 * 1024.0),
                        tot as f64 / (1024.0 * 1024.0),
                        pct
                    );
                    let _ = std::io::stderr().flush();
                }
            }
        }
        out_file.sync_all().ok();
        drop(out_file);

        let actual = hex::encode(hasher.finalize());
        if actual != expected_sha {
            let _ = fs::remove_file(&tmp);
            if show_progress {
                eprintln!();
            }
            return Err(format!(
                "SHA-256 verification failed for downloaded {}: expected {}, got {}",
                url, expected_sha, actual
            ));
        }
        fs::rename(&tmp, &target)
            .map_err(|e| format!("failed renaming {}: {}", target.display(), e))?;
        if show_progress {
            eprintln!(
                "\r    ✓ {}/{}  ({:.1} MB, SHA-256 verified)                    ",
                spec.slug,
                fname,
                downloaded as f64 / (1024.0 * 1024.0)
            );
        }
    }
    Ok(dest_dir)
}

/// Ensure the required local neural models (embedder + reranker) are downloaded and verified,
/// notifying the user upfront when a download is needed so `memnos` works without external AI.
pub fn ensure_local_models_ready(embed_model: &str) -> Result<(), String> {
    if offline_models_only() {
        let (emb_spec, rerank_spec) = specs_for_embed_model(embed_model);
        if !is_model_cached(emb_spec) || (rerank_enabled() && !is_model_cached(rerank_spec)) {
            return Err(format!(
                "local models for '{}' are not cached locally and MEMNOS_OFFLINE_MODELS=1 — disable MEMNOS_OFFLINE_MODELS to download (~930 MB) or configure OPENAI_API_KEY",
                embed_model
            ));
        }
    }
    let prev = ALLOW_MODEL_DOWNLOAD.swap(true, Ordering::AcqRel);
    let res = (|| {
        let (emb_spec, rerank_spec) = specs_for_embed_model(embed_model);
        let needed_download = !is_model_cached(emb_spec)
            || (rerank_enabled() && !is_model_cached(rerank_spec));
        resolve_or_download_model(emb_spec)?;
        if rerank_enabled() {
            resolve_or_download_model(rerank_spec)?;
        }
        if needed_download {
            eprintln!("  ✓ Local neural models ready — memnos now runs embeddings & recall on this computer.\n");
        }
        Ok(())
    })();
    ALLOW_MODEL_DOWNLOAD.store(prev, Ordering::Release);
    res
}

pub fn print_local_models_status(has_openai_key: bool, embed_model: &str) {
    let (emb_spec, rerank_spec) = specs_for_embed_model(embed_model);
    println!("memnos embedding & neural model status:");
    if has_openai_key {
        println!("  active mode:    OpenAI 1536-d (text-embedding-3-small via OPENAI_API_KEY)");
    } else {
        println!("  active mode:    Local neural (zero external AI required)");
    }
    println!(
        "  models cache:   {}",
        config_dir().join("models").display()
    );
    for spec in [emb_spec, rerank_spec] {
        let state = if let Some(dir) = find_cached_model_dir(spec) {
            format!("READY ({})", dir.display())
        } else {
            format!(
                "DOWNLOAD REQUIRED (~{} MB — run `memnos models pull`)",
                spec.approx_mb
            )
        };
        println!("  • {:<46} {}", spec.repo_id, state);
    }
}

// ---------------------------------------------------------------------------------------------
// Zero-DOM Flat-Arena Tokenizers (Unigram Viterbi & BERT WordPiece)
// ---------------------------------------------------------------------------------------------
// Multilingual `tokenizer.json` is ~16.3 MB with 250,002 pieces. Deserializing into an untyped
// `serde_json::Value` DOM + `HashMap<String, _>` allocates >500,000 tiny heap objects (~150 MB
// peak, ~55 MB retained in allocator small magazines). Streaming directly via `serde::de::Visitor`
// into a single contiguous UTF-8 `String` arena + flat `Vec` entry & open-addressed bucket tables
// reduces tokenizer memory to 3 contiguous allocations (~10.5 MB for 250k Unigram, <1 MB for 30k
// WordPiece) with zero small-object fragmentation and O(1) lookup.

#[inline]
fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

#[inline]
fn fnv1a64_subword(slice: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in b"##" {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    for &b in slice {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

struct ArenaStrSeed<'a>(&'a mut String);

impl<'de, 'a> DeserializeSeed<'de> for ArenaStrSeed<'a> {
    type Value = (u32, u32);

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        struct StrAppendVisitor<'b>(&'b mut String);
        impl<'de, 'b> Visitor<'de> for StrAppendVisitor<'b> {
            type Value = (u32, u32);
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a UTF-8 token piece string")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                let off = self.0.len() as u32;
                let len = v.len() as u32;
                self.0.push_str(v);
                Ok((off, len))
            }
        }
        deserializer.deserialize_str(StrAppendVisitor(self.0))
    }
}

#[derive(Clone, Copy)]
struct UnigramEntry {
    off: u32,
    len: u32,
    id: u32,
    score: f64,
}

struct PackedUnigramModel {
    arena: String,
    entries: Vec<UnigramEntry>,
    max_piece_bytes: usize,
    unk_id: Option<u32>,
}

impl<'de> serde::Deserialize<'de> for PackedUnigramModel {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ModelVisitor;
        impl<'de> Visitor<'de> for ModelVisitor {
            type Value = PackedUnigramModel;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("tokenizer model object")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut unk_id: Option<u32> = None;
                let mut vocab: Option<(String, Vec<UnigramEntry>, usize)> = None;
                while let Some(key) = map.next_key::<UnigramKeyField>()? {
                    match key {
                        UnigramKeyField::UnkId => {
                            unk_id = map.next_value::<Option<u32>>()?;
                        }
                        UnigramKeyField::Vocab => {
                            vocab = Some(map.next_value_seed(UnigramVocabSeed)?);
                        }
                        UnigramKeyField::Other => {
                            map.next_value::<IgnoredAny>()?;
                        }
                    }
                }
                let (arena, entries, max_piece_bytes) =
                    vocab.ok_or_else(|| serde::de::Error::missing_field("vocab"))?;
                Ok(PackedUnigramModel {
                    arena,
                    entries,
                    max_piece_bytes,
                    unk_id,
                })
            }
        }
        enum UnigramKeyField {
            UnkId,
            Vocab,
            Other,
        }
        impl<'de> serde::Deserialize<'de> for UnigramKeyField {
            fn deserialize<D2: Deserializer<'de>>(d: D2) -> Result<Self, D2::Error> {
                struct KV;
                impl<'de> Visitor<'de> for KV {
                    type Value = UnigramKeyField;
                    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                        f.write_str("field name")
                    }
                    fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                        Ok(match v {
                            "unk_id" => UnigramKeyField::UnkId,
                            "vocab" => UnigramKeyField::Vocab,
                            _ => UnigramKeyField::Other,
                        })
                    }
                }
                d.deserialize_identifier(KV)
            }
        }
        deserializer.deserialize_map(ModelVisitor)
    }
}

struct UnigramVocabSeed;

impl<'de> DeserializeSeed<'de> for UnigramVocabSeed {
    type Value = (String, Vec<UnigramEntry>, usize);

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        struct VocabSeqVisitor;
        impl<'de> Visitor<'de> for VocabSeqVisitor {
            type Value = (String, Vec<UnigramEntry>, usize);
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("unigram vocab array")
            }
            fn visit_seq<S: SeqAccess<'de>>(self, mut seq: S) -> Result<Self::Value, S::Error> {
                let cap_hint = seq.size_hint().unwrap_or(250_016);
                let mut arena = String::with_capacity(2_400_000);
                let mut entries = Vec::with_capacity(cap_hint);
                let mut max_piece_bytes = 1usize;
                let mut idx = 0u32;
                while seq
                    .next_element_seed(UnigramPairSeed {
                        arena: &mut arena,
                        entries: &mut entries,
                        max_piece_bytes: &mut max_piece_bytes,
                        id: idx,
                    })?
                    .is_some()
                {
                    idx = idx.saturating_add(1);
                }
                arena.shrink_to_fit();
                entries.shrink_to_fit();
                Ok((arena, entries, max_piece_bytes))
            }
        }
        deserializer.deserialize_seq(VocabSeqVisitor)
    }
}

struct UnigramPairSeed<'a> {
    arena: &'a mut String,
    entries: &'a mut Vec<UnigramEntry>,
    max_piece_bytes: &'a mut usize,
    id: u32,
}

impl<'de, 'a> DeserializeSeed<'de> for UnigramPairSeed<'a> {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        struct PairVisitor<'b>(UnigramPairSeed<'b>);
        impl<'de, 'b> Visitor<'de> for PairVisitor<'b> {
            type Value = ();
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("[piece, score] pair")
            }
            fn visit_seq<S: SeqAccess<'de>>(self, mut seq: S) -> Result<Self::Value, S::Error> {
                let Some((off, len)) = seq.next_element_seed(ArenaStrSeed(self.0.arena))? else {
                    return Ok(());
                };
                let Some(score) = seq.next_element::<f64>()? else {
                    return Ok(());
                };
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                *self.0.max_piece_bytes = (*self.0.max_piece_bytes).max(len as usize);
                self.0.entries.push(UnigramEntry {
                    off,
                    len,
                    id: self.0.id,
                    score,
                });
                Ok(())
            }
        }
        deserializer.deserialize_seq(PairVisitor(self))
    }
}

#[derive(serde::Deserialize)]
struct UnigramTokenizerDoc {
    model: PackedUnigramModel,
}

pub struct UnigramTokenizer {
    arena: String,
    entries: Vec<UnigramEntry>,
    buckets: Vec<u32>,
    mask: usize,
    max_piece_bytes: usize,
    bos_id: u32,
    pad_id: u32,
    eos_id: u32,
    unk_id: u32,
}

impl UnigramTokenizer {
    pub fn from_file(path: &Path) -> Result<Self, String> {
        let file = fs::File::open(path)
            .map_err(|e| format!("open tokenizer {}: {}", path.display(), e))?;
        let reader = BufReader::with_capacity(64 * 1024, file);
        let doc: UnigramTokenizerDoc = serde_json::from_reader(reader)
            .map_err(|e| format!("parse tokenizer {}: {}", path.display(), e))?;
        let m = doc.model;
        let table_cap = (m.entries.len().saturating_mul(2))
            .next_power_of_two()
            .max(16);
        let mask = table_cap - 1;
        let mut buckets = vec![u32::MAX; table_cap];
        for (slot_idx, ent) in m.entries.iter().enumerate() {
            let piece = &m.arena[ent.off as usize..(ent.off + ent.len) as usize];
            let mut pos = (fnv1a64(piece.as_bytes()) as usize) & mask;
            loop {
                let cur = buckets[pos];
                if cur == u32::MAX {
                    buckets[pos] = slot_idx as u32;
                    break;
                }
                let existing = &m.entries[cur as usize];
                let ex_str =
                    &m.arena[existing.off as usize..(existing.off + existing.len) as usize];
                if ex_str == piece {
                    buckets[pos] = slot_idx as u32;
                    break;
                }
                pos = (pos + 1) & mask;
            }
        }

        let mut tok = Self {
            arena: m.arena,
            entries: m.entries,
            buckets,
            mask,
            max_piece_bytes: m.max_piece_bytes,
            bos_id: 0,
            pad_id: 1,
            eos_id: 2,
            unk_id: m.unk_id.unwrap_or(3),
        };
        if let Some((id, _)) = tok.lookup("<s>") {
            tok.bos_id = id;
        }
        if let Some((id, _)) = tok.lookup("<pad>") {
            tok.pad_id = id;
        }
        if let Some((id, _)) = tok.lookup("</s>") {
            tok.eos_id = id;
        }
        Ok(tok)
    }

    #[inline]
    fn lookup(&self, piece: &str) -> Option<(u32, f64)> {
        let mut pos = (fnv1a64(piece.as_bytes()) as usize) & self.mask;
        loop {
            let cur = self.buckets[pos];
            if cur == u32::MAX {
                return None;
            }
            let ent = &self.entries[cur as usize];
            if ent.len as usize == piece.len()
                && &self.arena[ent.off as usize..(ent.off + ent.len) as usize] == piece
            {
                return Some((ent.id, ent.score));
            }
            pos = (pos + 1) & self.mask;
        }
    }

    pub fn pad_id(&self) -> u32 {
        self.pad_id
    }

    fn normalize_piece_input(text: &str) -> String {
        let mut out = String::with_capacity(text.len() + 8);
        for ch in text.chars() {
            let cp = ch as u32;
            if cp == 0 || (ch.is_control() && !matches!(ch, '\t' | '\n' | '\r')) {
                continue;
            }
            // Fold fullwidth ASCII forms (U+FF01..=U+FF5E) used in CJK text
            if (0xFF01..=0xFF5E).contains(&cp) {
                if let Some(ascii) = char::from_u32(cp - 0xFEE0) {
                    out.push(ascii);
                    continue;
                }
            }
            if cp == 0x3000 {
                out.push(' ');
            } else {
                out.push(ch);
            }
        }
        out
    }

    fn viterbi_word(&self, word: &str, out: &mut Vec<u32>) {
        if word.is_empty() {
            return;
        }
        let char_offsets: Vec<usize> = word
            .char_indices()
            .map(|(b, _)| b)
            .chain(std::iter::once(word.len()))
            .collect();
        let n = char_offsets.len() - 1;
        let mut best_score = vec![f64::NEG_INFINITY; n + 1];
        let mut best_prev = vec![0usize; n + 1];
        let mut best_id = vec![self.unk_id; n + 1];
        best_score[0] = 0.0;

        for i in 0..n {
            let base_sc = best_score[i];
            if base_sc == f64::NEG_INFINITY {
                continue;
            }
            let b_start = char_offsets[i];
            let mut matched_single = false;
            for j in (i + 1)..=n {
                let b_end = char_offsets[j];
                if b_end - b_start > self.max_piece_bytes {
                    break;
                }
                let sub = &word[b_start..b_end];
                if let Some((tid, sc)) = self.lookup(sub) {
                    if j == i + 1 {
                        matched_single = true;
                    }
                    let cand = base_sc + sc;
                    if cand > best_score[j] {
                        best_score[j] = cand;
                        best_prev[j] = i;
                        best_id[j] = tid;
                    }
                }
            }
            if !matched_single {
                let cand = base_sc - 100.0;
                if cand > best_score[i + 1] {
                    best_score[i + 1] = cand;
                    best_prev[i + 1] = i;
                    best_id[i + 1] = self.unk_id;
                }
            }
        }

        let mut rev = Vec::with_capacity(n);
        let mut cur = n;
        while cur > 0 {
            rev.push(best_id[cur]);
            cur = best_prev[cur];
        }
        for &tid in rev.iter().rev() {
            if tid == self.unk_id && out.last().copied() == Some(self.unk_id) {
                continue;
            }
            out.push(tid);
        }
    }

    pub fn tokenize_ids(&self, text: &str) -> Vec<u32> {
        let norm = Self::normalize_piece_input(text);
        let mut ids = Vec::new();
        let mut prefixed = String::with_capacity(64);
        for piece in norm.split_whitespace() {
            prefixed.clear();
            prefixed.push('\u{2581}');
            prefixed.push_str(piece);
            self.viterbi_word(&prefixed, &mut ids);
        }
        ids
    }

    pub fn encode_single(&self, text: &str, max_len: usize) -> (Vec<u32>, Vec<u32>) {
        let mut a = self.tokenize_ids(text);
        let budget = max_len.saturating_sub(2);
        if a.len() > budget {
            a.truncate(budget);
        }
        let mut ids = Vec::with_capacity(a.len() + 2);
        ids.push(self.bos_id);
        ids.extend(a);
        ids.push(self.eos_id);
        let type_ids = vec![0u32; ids.len()];
        (ids, type_ids)
    }

    /// XLM-RoBERTa pair template: `<s> A </s> </s> B </s>` (`type_ids` all `0`).
    pub fn encode_pair(&self, query: &str, doc: &str, max_len: usize) -> (Vec<u32>, Vec<u32>) {
        let mut a = self.tokenize_ids(query);
        let mut b = self.tokenize_ids(doc);
        let budget = max_len.saturating_sub(4);
        while a.len() + b.len() > budget {
            if a.len() > b.len() {
                a.pop();
            } else {
                b.pop();
            }
        }
        let mut ids = Vec::with_capacity(a.len() + b.len() + 4);
        ids.push(self.bos_id);
        ids.extend(a);
        ids.push(self.eos_id);
        ids.push(self.eos_id);
        ids.extend(b);
        ids.push(self.eos_id);
        let type_ids = vec![0u32; ids.len()];
        (ids, type_ids)
    }
}

// ---------------------------------------------------------------------------------------------
// Pure-Rust BERT WordPiece Tokenizer (`bert-base-uncased` / `BAAI/bge-small-en-v1.5`)
// ---------------------------------------------------------------------------------------------

#[derive(Clone, Copy)]
struct WordPieceEntry {
    off: u32,
    len: u32,
    id: u32,
}

struct PackedWordPieceModel {
    arena: String,
    entries: Vec<WordPieceEntry>,
    max_input_chars_per_word: Option<usize>,
}

impl<'de> serde::Deserialize<'de> for PackedWordPieceModel {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct ModelVisitor;
        impl<'de> Visitor<'de> for ModelVisitor {
            type Value = PackedWordPieceModel;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("wordpiece model object")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut max_chars: Option<usize> = None;
                let mut vocab: Option<(String, Vec<WordPieceEntry>)> = None;
                while let Some(key) = map.next_key::<KeyField>()? {
                    match key {
                        KeyField::MaxChars => {
                            max_chars = map.next_value::<Option<usize>>()?;
                        }
                        KeyField::Vocab => {
                            vocab = Some(map.next_value_seed(WordPieceVocabSeed)?);
                        }
                        KeyField::Other => {
                            map.next_value::<IgnoredAny>()?;
                        }
                    }
                }
                let (arena, entries) =
                    vocab.ok_or_else(|| serde::de::Error::missing_field("vocab"))?;
                Ok(PackedWordPieceModel {
                    arena,
                    entries,
                    max_input_chars_per_word: max_chars,
                })
            }
        }
        enum KeyField {
            MaxChars,
            Vocab,
            Other,
        }
        impl<'de> serde::Deserialize<'de> for KeyField {
            fn deserialize<D2: Deserializer<'de>>(d: D2) -> Result<Self, D2::Error> {
                struct KV;
                impl<'de> Visitor<'de> for KV {
                    type Value = KeyField;
                    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                        f.write_str("field name")
                    }
                    fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
                        Ok(match v {
                            "max_input_chars_per_word" => KeyField::MaxChars,
                            "vocab" => KeyField::Vocab,
                            _ => KeyField::Other,
                        })
                    }
                }
                d.deserialize_identifier(KV)
            }
        }
        deserializer.deserialize_map(ModelVisitor)
    }
}

struct WordPieceVocabSeed;

impl<'de> DeserializeSeed<'de> for WordPieceVocabSeed {
    type Value = (String, Vec<WordPieceEntry>);

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        struct VocabMapVisitor;
        impl<'de> Visitor<'de> for VocabMapVisitor {
            type Value = (String, Vec<WordPieceEntry>);
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("wordpiece vocab map")
            }
            fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                let mut arena = String::with_capacity(262_144);
                let mut entries = Vec::with_capacity(map.size_hint().unwrap_or(31_000));
                while let Some((off, len)) = map.next_key_seed(ArenaStrSeed(&mut arena))? {
                    let id = map.next_value::<u32>()?;
                    entries.push(WordPieceEntry { off, len, id });
                }
                arena.shrink_to_fit();
                entries.shrink_to_fit();
                Ok((arena, entries))
            }
        }
        deserializer.deserialize_map(VocabMapVisitor)
    }
}

#[derive(serde::Deserialize)]
struct WordPieceTokenizerDoc {
    model: PackedWordPieceModel,
}

pub struct WordPieceTokenizer {
    arena: String,
    entries: Vec<WordPieceEntry>,
    buckets: Vec<u32>,
    mask: usize,
    unk_id: u32,
    cls_id: u32,
    sep_id: u32,
    max_word_chars: usize,
}

fn is_bert_control(c: char) -> bool {
    if matches!(c, '\t' | '\n' | '\r') {
        return false;
    }
    let cp = c as u32;
    cp == 0 || cp == 0xFFFD || c.is_control()
}

fn is_bert_whitespace(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r') || c.is_whitespace()
}

fn is_cjk_char(c: char) -> bool {
    let cp = c as u32;
    (0x4E00..=0x9FFF).contains(&cp)
        || (0x3400..=0x4DBF).contains(&cp)
        || (0x20000..=0x2A6DF).contains(&cp)
        || (0x2A700..=0x2B73F).contains(&cp)
        || (0x2B740..=0x2B81F).contains(&cp)
        || (0x2B820..=0x2CEAF).contains(&cp)
        || (0xF900..=0xFAFF).contains(&cp)
        || (0x2F800..=0x2FA1F).contains(&cp)
}

fn is_bert_punctuation(c: char) -> bool {
    let cp = c as u32;
    if (33..=47).contains(&cp)
        || (58..=64).contains(&cp)
        || (91..=96).contains(&cp)
        || (123..=126).contains(&cp)
    {
        return true;
    }
    !c.is_alphanumeric() && !is_bert_whitespace(c)
}

fn fold_latin_accent(c: char) -> Option<char> {
    match c {
        'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => Some('a'),
        'ç' => Some('c'),
        'è' | 'é' | 'ê' | 'ë' => Some('e'),
        'ì' | 'í' | 'î' | 'ï' => Some('i'),
        'ñ' => Some('n'),
        'ò' | 'ó' | 'ô' | 'õ' | 'ö' => Some('o'),
        'ù' | 'ú' | 'û' | 'ü' => Some('u'),
        'ý' | 'ÿ' => Some('y'),
        _ if ('\u{0300}'..='\u{036f}').contains(&c) => None,
        _ => Some(c),
    }
}

impl WordPieceTokenizer {
    pub fn from_file(path: &Path) -> Result<Self, String> {
        let file = fs::File::open(path)
            .map_err(|e| format!("open tokenizer {}: {}", path.display(), e))?;
        let reader = BufReader::with_capacity(32 * 1024, file);
        let doc: WordPieceTokenizerDoc = serde_json::from_reader(reader)
            .map_err(|e| format!("parse tokenizer {}: {}", path.display(), e))?;
        let m = doc.model;
        let table_cap = (m.entries.len().saturating_mul(2))
            .next_power_of_two()
            .max(16);
        let mask = table_cap - 1;
        let mut buckets = vec![u32::MAX; table_cap];
        for (slot_idx, ent) in m.entries.iter().enumerate() {
            let tok_str = &m.arena[ent.off as usize..(ent.off + ent.len) as usize];
            let mut pos = (fnv1a64(tok_str.as_bytes()) as usize) & mask;
            loop {
                let cur = buckets[pos];
                if cur == u32::MAX {
                    buckets[pos] = slot_idx as u32;
                    break;
                }
                let existing = &m.entries[cur as usize];
                let ex_str =
                    &m.arena[existing.off as usize..(existing.off + existing.len) as usize];
                if ex_str == tok_str {
                    buckets[pos] = slot_idx as u32;
                    break;
                }
                pos = (pos + 1) & mask;
            }
        }
        let mut tok = Self {
            arena: m.arena,
            entries: m.entries,
            buckets,
            mask,
            unk_id: 100,
            cls_id: 101,
            sep_id: 102,
            max_word_chars: m.max_input_chars_per_word.unwrap_or(100),
        };
        if let Some(id) = tok.lookup("[UNK]") {
            tok.unk_id = id;
        }
        if let Some(id) = tok.lookup("[CLS]") {
            tok.cls_id = id;
        }
        if let Some(id) = tok.lookup("[SEP]") {
            tok.sep_id = id;
        }
        Ok(tok)
    }

    #[inline]
    fn lookup(&self, word: &str) -> Option<u32> {
        let mut pos = (fnv1a64(word.as_bytes()) as usize) & self.mask;
        loop {
            let cur = self.buckets[pos];
            if cur == u32::MAX {
                return None;
            }
            let ent = &self.entries[cur as usize];
            if ent.len as usize == word.len()
                && &self.arena[ent.off as usize..(ent.off + ent.len) as usize] == word
            {
                return Some(ent.id);
            }
            pos = (pos + 1) & self.mask;
        }
    }

    #[inline]
    fn lookup_subword(&self, slice: &str) -> Option<u32> {
        let want_len = slice.len() + 2;
        let mut pos = (fnv1a64_subword(slice.as_bytes()) as usize) & self.mask;
        loop {
            let cur = self.buckets[pos];
            if cur == u32::MAX {
                return None;
            }
            let ent = &self.entries[cur as usize];
            if ent.len as usize == want_len {
                let s = &self.arena[ent.off as usize..(ent.off + ent.len) as usize];
                if s.starts_with("##") && &s[2..] == slice {
                    return Some(ent.id);
                }
            }
            pos = (pos + 1) & self.mask;
        }
    }

    fn tokenize_ids(&self, text: &str) -> Vec<u32> {
        let mut cleaned = String::with_capacity(text.len() + 8);
        for ch in text.chars() {
            if is_bert_control(ch) {
                continue;
            }
            if is_bert_whitespace(ch) {
                cleaned.push(' ');
            } else if is_cjk_char(ch) {
                cleaned.push(' ');
                cleaned.push(ch);
                cleaned.push(' ');
            } else {
                for lc in ch.to_lowercase() {
                    if let Some(folded) = fold_latin_accent(lc) {
                        cleaned.push(folded);
                    }
                }
            }
        }

        let mut basic_tokens: Vec<String> = Vec::new();
        for ws_tok in cleaned.split_whitespace() {
            let mut cur = String::new();
            for ch in ws_tok.chars() {
                if is_bert_punctuation(ch) {
                    if !cur.is_empty() {
                        basic_tokens.push(std::mem::take(&mut cur));
                    }
                    basic_tokens.push(ch.to_string());
                } else {
                    cur.push(ch);
                }
            }
            if !cur.is_empty() {
                basic_tokens.push(cur);
            }
        }

        let mut ids = Vec::with_capacity(basic_tokens.len() + 4);
        for word in basic_tokens {
            let char_indices: Vec<(usize, char)> = word.char_indices().collect();
            if char_indices.len() > self.max_word_chars {
                ids.push(self.unk_id);
                continue;
            }
            let mut sub_ids = Vec::new();
            let mut start_ci = 0usize;
            let mut is_bad = false;
            while start_ci < char_indices.len() {
                let mut end_ci = char_indices.len();
                let mut matched: Option<u32> = None;
                let start_byte = char_indices[start_ci].0;
                while start_ci < end_ci {
                    let end_byte = if end_ci == char_indices.len() {
                        word.len()
                    } else {
                        char_indices[end_ci].0
                    };
                    let slice = &word[start_byte..end_byte];
                    let found = if start_ci == 0 {
                        self.lookup(slice)
                    } else {
                        self.lookup_subword(slice)
                    };
                    if let Some(id) = found {
                        matched = Some(id);
                        break;
                    }
                    end_ci -= 1;
                }
                if let Some(id) = matched {
                    sub_ids.push(id);
                    start_ci = end_ci;
                } else {
                    is_bad = true;
                    break;
                }
            }
            if is_bad {
                ids.push(self.unk_id);
            } else {
                ids.extend(sub_ids);
            }
        }
        ids
    }

    pub fn encode_single(&self, text: &str, max_len: usize) -> (Vec<u32>, Vec<u32>) {
        let mut a = self.tokenize_ids(text);
        let budget = max_len.saturating_sub(2);
        if a.len() > budget {
            a.truncate(budget);
        }
        let mut ids = Vec::with_capacity(a.len() + 2);
        ids.push(self.cls_id);
        ids.extend(a);
        ids.push(self.sep_id);
        let type_ids = vec![0u32; ids.len()];
        (ids, type_ids)
    }

    pub fn encode_pair(&self, query: &str, doc: &str, max_len: usize) -> (Vec<u32>, Vec<u32>) {
        let mut a = self.tokenize_ids(query);
        let mut b = self.tokenize_ids(doc);
        let budget = max_len.saturating_sub(3);
        while a.len() + b.len() > budget {
            if a.len() > b.len() {
                a.pop();
            } else {
                b.pop();
            }
        }
        let mut ids = Vec::with_capacity(a.len() + b.len() + 3);
        let mut type_ids = Vec::with_capacity(a.len() + b.len() + 3);
        ids.push(self.cls_id);
        type_ids.push(0);
        for id in a {
            ids.push(id);
            type_ids.push(0);
        }
        ids.push(self.sep_id);
        type_ids.push(0);
        for id in b {
            ids.push(id);
            type_ids.push(1);
        }
        ids.push(self.sep_id);
        type_ids.push(1);
        (ids, type_ids)
    }
}

// ---------------------------------------------------------------------------------------------
// Zero-Copy `mmap` Word Embeddings & Shared BERT/XLM-R Lite Backbone
// ---------------------------------------------------------------------------------------------
// Multilingual token tables (`[250002, 384]` / `[250037, 384]`) are 366 MB each on disk, yet a
// single query/rerank batch touches < 2,000 token IDs (~3 MB). Keeping `word_embeddings.weight`
// directly in a read-only `Mmap` instead of copying it into a `candle` heap `Tensor` saves
// ~366 MB of dirty RAM per multilingual model (~732 MB in local 384-d mode) with bit-identical
// IEEE-754 `f32` outputs.

struct MmapWordEmbeddings {
    mmap: Mmap,
    data_start: usize,
    vocab_size: usize,
    hidden_size: usize,
}

impl MmapWordEmbeddings {
    fn open(safetensors_path: &Path, candidate_keys: &[&str]) -> Result<Self, String> {
        let file = fs::File::open(safetensors_path)
            .map_err(|e| format!("open {}: {}", safetensors_path.display(), e))?;
        let mmap = unsafe {
            Mmap::map(&file).map_err(|e| format!("mmap {}: {}", safetensors_path.display(), e))?
        };
        if mmap.len() < 8 {
            return Err(format!("truncated safetensors: {}", safetensors_path.display()));
        }
        let mut hlen_bytes = [0u8; 8];
        hlen_bytes.copy_from_slice(&mmap[..8]);
        let hlen = u64::from_le_bytes(hlen_bytes) as usize;
        let payload_base = 8usize
            .checked_add(hlen)
            .ok_or_else(|| "safetensors header overflow".to_string())?;
        if mmap.len() < payload_base {
            return Err("safetensors header exceeds file length".to_string());
        }
        let hdr: Value = serde_json::from_slice(&mmap[8..payload_base])
            .map_err(|e| format!("parse safetensors header: {}", e))?;

        let mut meta_opt = None;
        for &k in candidate_keys {
            if let Some(v) = hdr.get(k) {
                meta_opt = Some(v);
                break;
            }
        }
        let meta = meta_opt.ok_or_else(|| {
            format!(
                "word_embeddings.weight not found in {}",
                safetensors_path.display()
            )
        })?;

        let dtype = meta.get("dtype").and_then(|v| v.as_str()).unwrap_or("");
        if dtype != "F32" {
            return Err(format!("expected F32 word_embeddings, got {}", dtype));
        }
        let shape = meta
            .get("shape")
            .and_then(|v| v.as_array())
            .ok_or_else(|| "missing word_embeddings shape".to_string())?;
        if shape.len() != 2 {
            return Err("expected 2D word_embeddings shape".to_string());
        }
        let vocab_size = shape[0].as_u64().unwrap_or(0) as usize;
        let hidden_size = shape[1].as_u64().unwrap_or(0) as usize;
        let offsets = meta
            .get("data_offsets")
            .and_then(|v| v.as_array())
            .ok_or_else(|| "missing word_embeddings data_offsets".to_string())?;
        let rel_start = offsets[0].as_u64().unwrap_or(0) as usize;
        let rel_end = offsets[1].as_u64().unwrap_or(0) as usize;
        let expected_bytes = vocab_size
            .checked_mul(hidden_size)
            .and_then(|n| n.checked_mul(4))
            .ok_or_else(|| "word_embeddings byte size overflow".to_string())?;
        let data_start = payload_base + rel_start;
        let data_end = payload_base + rel_end;
        if rel_end.saturating_sub(rel_start) != expected_bytes || data_end > mmap.len() {
            return Err("word_embeddings data_offsets out of bounds".to_string());
        }
        Ok(Self {
            mmap,
            data_start,
            vocab_size,
            hidden_size,
        })
    }

    fn gather(&self, flat_ids: &[u32], bsz: usize, seq_len: usize, dev: &Device) -> Result<Tensor, String> {
        let row_bytes = self.hidden_size * 4;
        let mut out = Vec::with_capacity(flat_ids.len() * self.hidden_size);
        for &tok_id in flat_ids {
            let idx = (tok_id as usize).min(self.vocab_size.saturating_sub(1));
            let off = self.data_start + idx * row_bytes;
            let row_slice = &self.mmap[off..off + row_bytes];
            for chunk in row_slice.chunks_exact(4) {
                out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
            }
        }
        Tensor::from_vec(out, (bsz, seq_len, self.hidden_size), dev).map_err(|e| e.to_string())
    }
}

struct MmapLiteEmbeddings {
    word_embeddings: MmapWordEmbeddings,
    position_embeddings: Embedding,
    token_type_embeddings: Embedding,
    layer_norm: LayerNorm,
    roberta_pad_idx: Option<u32>,
}

impl MmapLiteEmbeddings {
    fn load(
        safetensors_path: &Path,
        emb_vb: VarBuilder,
        cfg: &BertConfig,
        word_keys: &[&str],
        roberta_pad_idx: Option<u32>,
    ) -> Result<Self, String> {
        let word_embeddings = MmapWordEmbeddings::open(safetensors_path, word_keys)?;
        let position_embeddings = embedding(
            cfg.max_position_embeddings,
            cfg.hidden_size,
            emb_vb.pp("position_embeddings"),
        )
        .map_err(|e| format!("load position_embeddings: {}", e))?;
        let token_type_embeddings = embedding(
            cfg.type_vocab_size,
            cfg.hidden_size,
            emb_vb.pp("token_type_embeddings"),
        )
        .map_err(|e| format!("load token_type_embeddings: {}", e))?;
        let layer_norm = layer_norm(cfg.hidden_size, cfg.layer_norm_eps, emb_vb.pp("LayerNorm"))
            .map_err(|e| format!("load embeddings.LayerNorm: {}", e))?;
        Ok(Self {
            word_embeddings,
            position_embeddings,
            token_type_embeddings,
            layer_norm,
            roberta_pad_idx,
        })
    }

    fn forward(
        &self,
        flat_ids: &[u32],
        flat_tids: &[u32],
        bsz: usize,
        seq_len: usize,
        dev: &Device,
    ) -> Result<Tensor, String> {
        let word_emb = self.word_embeddings.gather(flat_ids, bsz, seq_len, dev)?;
        let tids_t =
            Tensor::from_vec(flat_tids.to_vec(), (bsz, seq_len), dev).map_err(|e| e.to_string())?;
        let type_emb = self
            .token_type_embeddings
            .forward(&tids_t)
            .map_err(|e| e.to_string())?;
        let mut sum = (&word_emb + &type_emb).map_err(|e| e.to_string())?;

        if let Some(pad_id) = self.roberta_pad_idx {
            let mut pos_flat = Vec::with_capacity(bsz * seq_len);
            for row in flat_ids.chunks_exact(seq_len) {
                let mut cum = 0u32;
                for &id in row {
                    if id != pad_id {
                        cum += 1;
                        pos_flat.push(pad_id + cum);
                    } else {
                        pos_flat.push(pad_id);
                    }
                }
            }
            let pos_ids =
                Tensor::from_vec(pos_flat, (bsz, seq_len), dev).map_err(|e| e.to_string())?;
            let pos_emb = self
                .position_embeddings
                .forward(&pos_ids)
                .map_err(|e| e.to_string())?;
            sum = sum.broadcast_add(&pos_emb).map_err(|e| e.to_string())?;
        } else {
            let pos_vec: Vec<u32> = (0..seq_len as u32).collect();
            let pos_ids = Tensor::new(pos_vec.as_slice(), dev).map_err(|e| e.to_string())?;
            let pos_emb = self
                .position_embeddings
                .forward(&pos_ids)
                .map_err(|e| e.to_string())?;
            sum = sum.broadcast_add(&pos_emb).map_err(|e| e.to_string())?;
        }

        self.layer_norm.forward(&sum).map_err(|e| e.to_string())
    }
}

fn build_extended_attn_mask(
    flat_mask: &[u32],
    bsz: usize,
    seq_len: usize,
    dev: &Device,
) -> Result<Tensor, String> {
    let vals: Vec<f32> = flat_mask
        .iter()
        .map(|&m| if m != 0 { 0.0f32 } else { f32::MIN })
        .collect();
    Tensor::from_vec(vals, (bsz, 1, 1, seq_len), dev).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------------------------
// Multilingual E5 Embedder (`intfloat/multilingual-e5-small`) & mMARCO Reranker
// ---------------------------------------------------------------------------------------------

pub struct E5MultilingualModel {
    embeddings: MmapLiteEmbeddings,
    encoder: Mutex<BertEncoder>,
    tokenizer: UnigramTokenizer,
    max_len: usize,
}

impl E5MultilingualModel {
    pub fn load() -> Result<Self, String> {
        let dir = resolve_or_download_model(&E5_MULTI_SPEC)?;
        let cfg_raw = fs::read_to_string(dir.join("config.json"))
            .map_err(|e| format!("read e5 config.json: {}", e))?;
        let cfg: BertConfig = serde_json::from_str(&cfg_raw)
            .map_err(|e| format!("parse e5 config.json: {}", e))?;
        let max_len = cfg.max_position_embeddings.min(512).max(16);
        let tokenizer = UnigramTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let st_path = dir.join("model.safetensors");
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[st_path.as_path()], DType::F32, &Device::Cpu)
                .map_err(|e| format!("mmap e5 model.safetensors: {}", e))?
        };
        let embeddings = MmapLiteEmbeddings::load(
            &st_path,
            vb.pp("embeddings"),
            &cfg,
            &["embeddings.word_embeddings.weight", "bert.embeddings.word_embeddings.weight"],
            None,
        )
        .or_else(|_| {
            MmapLiteEmbeddings::load(
                &st_path,
                vb.pp("bert.embeddings"),
                &cfg,
                &["bert.embeddings.word_embeddings.weight", "embeddings.word_embeddings.weight"],
                None,
            )
        })?;
        let encoder = BertEncoder::load(vb.pp("encoder"), &cfg)
            .or_else(|_| BertEncoder::load(vb.pp("bert.encoder"), &cfg))
            .map_err(|e| format!("load E5MultilingualModel encoder: {}", e))?;
        Ok(Self {
            embeddings,
            encoder: Mutex::new(encoder),
            tokenizer,
            max_len,
        })
    }

    pub fn embed(&self, prefixed_text: &str) -> Result<Vec<f32>, String> {
        let (ids, type_ids) = self.tokenizer.encode_single(prefixed_text, self.max_len);
        let seq_len = ids.len();
        let dev = Device::Cpu;
        let emb_out = self.embeddings.forward(&ids, &type_ids, 1, seq_len, &dev)?;
        let mask_ones = vec![1u32; seq_len];
        let attn_mask = build_extended_attn_mask(&mask_ones, 1, seq_len, &dev)?;
        let hidden = {
            let guard = self.encoder.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&emb_out, &attn_mask)
                .map_err(|e| format!("e5 forward: {}", e))?
        };
        let pooled = hidden
            .mean(1)
            .and_then(|t| t.i(0))
            .and_then(|t| t.to_vec1::<f32>())
            .map_err(|e| format!("e5 mean pool: {}", e))?;
        let norm: f32 = pooled.iter().map(|x| x * x).sum::<f32>().sqrt();
        let mut out = pooled;
        if norm > 1e-12 {
            for x in &mut out {
                *x /= norm;
            }
        }
        Ok(out)
    }

    pub fn embed_batch(&self, prefixed_texts: &[String]) -> Result<Vec<Vec<f32>>, String> {
        if prefixed_texts.is_empty() {
            return Ok(vec![]);
        }
        let dev = Device::Cpu;
        let pad_id = self.tokenizer.pad_id();
        let mut results = vec![Vec::new(); prefixed_texts.len()];
        let mut indexed: Vec<(usize, Vec<u32>, Vec<u32>)> = prefixed_texts
            .iter()
            .enumerate()
            .map(|(i, t)| {
                let (ids, tids) = self.tokenizer.encode_single(t, self.max_len);
                (i, ids, tids)
            })
            .collect();
        indexed.sort_by_key(|(_, ids, _)| ids.len());

        for chunk in indexed.chunks(16) {
            let bsz = chunk.len();
            let max_seq = chunk.iter().map(|(_, ids, _)| ids.len()).max().unwrap_or(2).max(2);
            let mut orig_lens = Vec::with_capacity(bsz);
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (_, ids, tids) in chunk {
                let n = ids.len();
                orig_lens.push(n);
                flat_ids.extend_from_slice(ids);
                flat_tids.extend_from_slice(tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(pad_id).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let emb_out = self.embeddings.forward(&flat_ids, &flat_tids, bsz, max_seq, &dev)?;
            let attn_mask = build_extended_attn_mask(&flat_mask, bsz, max_seq, &dev)?;
            let hidden = {
                let guard = self.encoder.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&emb_out, &attn_mask)
                    .map_err(|e| format!("e5 batch forward: {}", e))?
            };
            for (i, &seq_n) in orig_lens.iter().enumerate() {
                let pooled = hidden
                    .i((i, 0..seq_n, ..))
                    .and_then(|t| t.mean(0))
                    .and_then(|t| t.to_vec1::<f32>())
                    .map_err(|e| format!("e5 batch pool: {}", e))?;
                let norm: f32 = pooled.iter().map(|x| x * x).sum::<f32>().sqrt();
                let mut out = pooled;
                if norm > 1e-12 {
                    for x in &mut out {
                        *x /= norm;
                    }
                }
                results[chunk[i].0] = out;
            }
        }
        Ok(results)
    }
}

pub struct MmarcoCrossEncoder {
    embeddings: MmapLiteEmbeddings,
    encoder: Mutex<BertEncoder>,
    cls_dense: Linear,
    cls_out_proj: Linear,
    tokenizer: UnigramTokenizer,
    max_len: usize,
}

impl MmarcoCrossEncoder {
    pub fn load() -> Result<Self, String> {
        let dir = resolve_or_download_model(&MMARCO_RERANK_SPEC)?;
        let cfg_raw = fs::read_to_string(dir.join("config.json"))
            .map_err(|e| format!("read mmarco config.json: {}", e))?;
        let cfg: BertConfig = serde_json::from_str(&cfg_raw)
            .map_err(|e| format!("parse mmarco config.json: {}", e))?;
        let max_len = cfg
            .max_position_embeddings
            .saturating_sub(2)
            .min(rerank_max_tokens())
            .max(32);
        let hsz = cfg.hidden_size;
        let tokenizer = UnigramTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let st_path = dir.join("model.safetensors");
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[st_path.as_path()], DType::F32, &Device::Cpu)
                .map_err(|e| format!("mmap mmarco model.safetensors: {}", e))?
        };
        let embeddings = MmapLiteEmbeddings::load(
            &st_path,
            vb.pp("roberta.embeddings"),
            &cfg,
            &["roberta.embeddings.word_embeddings.weight"],
            Some(cfg.pad_token_id as u32),
        )?;
        let encoder = BertEncoder::load(vb.pp("roberta.encoder"), &cfg)
            .map_err(|e| format!("load MmarcoCrossEncoder encoder: {}", e))?;
        let cls_dense = linear(hsz, hsz, vb.pp("classifier.dense"))
            .map_err(|e| format!("load mmarco classifier.dense: {}", e))?;
        let cls_out_proj = linear(hsz, 1, vb.pp("classifier.out_proj"))
            .map_err(|e| format!("load mmarco classifier.out_proj: {}", e))?;
        Ok(Self {
            embeddings,
            encoder: Mutex::new(encoder),
            cls_dense,
            cls_out_proj,
            tokenizer,
            max_len,
        })
    }

    pub fn score_pairs(&self, query: &str, candidates: &[&str]) -> Result<Vec<f64>, String> {
        self.score_pairs_until(query, candidates, None)
    }

    pub fn score_pairs_until(
        &self,
        query: &str,
        candidates: &[&str],
        deadline: Option<Instant>,
    ) -> Result<Vec<f64>, String> {
        if candidates.is_empty() {
            return Ok(vec![]);
        }
        if deadline.map_or(false, |dl| Instant::now() >= dl) {
            return Err("rerank_budget_exceeded".to_string());
        }
        let q_clamped: String = query.chars().take(1024).collect();
        let mut all_scores = vec![0.0f64; candidates.len()];
        let dev = Device::Cpu;
        let pad_id = self.tokenizer.pad_id();

        let mut indexed: Vec<(usize, Vec<u32>, Vec<u32>)> = candidates
            .iter()
            .enumerate()
            .map(|(idx, &doc)| {
                let d_clamped: String = doc.chars().take(2048).collect();
                let (ids, tids) = self.tokenizer.encode_pair(&q_clamped, &d_clamped, self.max_len);
                (idx, ids, tids)
            })
            .collect();
        indexed.sort_by_key(|(_, ids, _)| ids.len());

        for chunk in indexed.chunks(4) {
            if deadline.map_or(false, |dl| Instant::now() >= dl) {
                return Err("rerank_budget_exceeded".to_string());
            }
            let bsz = chunk.len();
            let max_seq = chunk.iter().map(|(_, ids, _)| ids.len()).max().unwrap_or(4).max(4);
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (_, ids, tids) in chunk {
                let n = ids.len();
                flat_ids.extend_from_slice(ids);
                flat_tids.extend_from_slice(tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(pad_id).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let emb_out = self.embeddings.forward(&flat_ids, &flat_tids, bsz, max_seq, &dev)?;
            let attn_mask = build_extended_attn_mask(&flat_mask, bsz, max_seq, &dev)?;
            let hidden = {
                let guard = self.encoder.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&emb_out, &attn_mask)
                    .map_err(|e| format!("mmarco forward: {}", e))?
            };
            let cls_states = hidden
                .i((.., 0, ..))
                .and_then(|t| t.contiguous())
                .map_err(|e| e.to_string())?;
            let logits = self
                .cls_dense
                .forward(&cls_states)
                .and_then(|t| t.tanh())
                .and_then(|t| self.cls_out_proj.forward(&t))
                .and_then(|t| t.squeeze(1))
                .and_then(|t| t.to_vec1::<f32>())
                .map_err(|e| format!("mmarco logits: {}", e))?;
            for (local_i, logit) in logits.into_iter().enumerate() {
                let x = logit as f64;
                let sig = if x >= 0.0 {
                    1.0 / (1.0 + (-x).exp())
                } else {
                    let ex = x.exp();
                    ex / (1.0 + ex)
                };
                all_scores[chunk[local_i].0] = sig;
            }
        }
        Ok(all_scores)
    }
}

// ---------------------------------------------------------------------------------------------
// Backward-Compatible English BGE Embedder (`BAAI/bge-small-en-v1.5`) & MiniLM Reranker
// ---------------------------------------------------------------------------------------------

pub struct BgeEmbedModel {
    embeddings: MmapLiteEmbeddings,
    encoder: Mutex<BertEncoder>,
    tokenizer: WordPieceTokenizer,
    max_len: usize,
}

impl BgeEmbedModel {
    pub fn load() -> Result<Self, String> {
        let dir = resolve_or_download_model(&BGE_SPEC)?;
        let cfg_raw = fs::read_to_string(dir.join("config.json"))
            .map_err(|e| format!("read bge config.json: {}", e))?;
        let cfg: BertConfig = serde_json::from_str(&cfg_raw)
            .map_err(|e| format!("parse bge config.json: {}", e))?;
        let max_len = cfg.max_position_embeddings.min(512).max(16);
        let tokenizer = WordPieceTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let st_path = dir.join("model.safetensors");
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[st_path.as_path()], DType::F32, &Device::Cpu)
                .map_err(|e| format!("mmap bge model.safetensors: {}", e))?
        };
        let embeddings = MmapLiteEmbeddings::load(
            &st_path,
            vb.pp("embeddings"),
            &cfg,
            &["embeddings.word_embeddings.weight", "bert.embeddings.word_embeddings.weight"],
            None,
        )
        .or_else(|_| {
            MmapLiteEmbeddings::load(
                &st_path,
                vb.pp("bert.embeddings"),
                &cfg,
                &["bert.embeddings.word_embeddings.weight", "embeddings.word_embeddings.weight"],
                None,
            )
        })?;
        let encoder = BertEncoder::load(vb.pp("encoder"), &cfg)
            .or_else(|_| BertEncoder::load(vb.pp("bert.encoder"), &cfg))
            .map_err(|e| format!("load BgeEmbedModel encoder: {}", e))?;
        Ok(Self {
            embeddings,
            encoder: Mutex::new(encoder),
            tokenizer,
            max_len,
        })
    }

    pub fn embed(&self, text: &str) -> Result<Vec<f32>, String> {
        let (ids, type_ids) = self.tokenizer.encode_single(text, self.max_len);
        let seq_len = ids.len();
        let dev = Device::Cpu;
        let emb_out = self.embeddings.forward(&ids, &type_ids, 1, seq_len, &dev)?;
        let mask_ones = vec![1u32; seq_len];
        let attn_mask = build_extended_attn_mask(&mask_ones, 1, seq_len, &dev)?;
        let hidden = {
            let guard = self.encoder.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&emb_out, &attn_mask)
                .map_err(|e| format!("bert forward: {}", e))?
        };
        let cls = hidden
            .i((0, 0, ..))
            .and_then(|t| t.to_vec1::<f32>())
            .map_err(|e| format!("cls slice: {}", e))?;
        let norm: f32 = cls.iter().map(|x| x * x).sum::<f32>().sqrt();
        let mut out = cls;
        if norm > 1e-12 {
            for x in &mut out {
                *x /= norm;
            }
        }
        Ok(out)
    }

    pub fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String> {
        if texts.is_empty() {
            return Ok(vec![]);
        }
        let dev = Device::Cpu;
        let mut results = vec![Vec::new(); texts.len()];
        let mut indexed: Vec<(usize, Vec<u32>, Vec<u32>)> = texts
            .iter()
            .enumerate()
            .map(|(i, t)| {
                let (ids, tids) = self.tokenizer.encode_single(t, self.max_len);
                (i, ids, tids)
            })
            .collect();
        indexed.sort_by_key(|(_, ids, _)| ids.len());

        for chunk in indexed.chunks(16) {
            let bsz = chunk.len();
            let max_seq = chunk.iter().map(|(_, ids, _)| ids.len()).max().unwrap_or(2).max(2);
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (_, ids, tids) in chunk {
                let n = ids.len();
                flat_ids.extend_from_slice(ids);
                flat_tids.extend_from_slice(tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(0u32).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let emb_out = self.embeddings.forward(&flat_ids, &flat_tids, bsz, max_seq, &dev)?;
            let attn_mask = build_extended_attn_mask(&flat_mask, bsz, max_seq, &dev)?;
            let hidden = {
                let guard = self.encoder.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&emb_out, &attn_mask)
                    .map_err(|e| format!("bert batch forward: {}", e))?
            };
            for i in 0..bsz {
                let cls = hidden
                    .i((i, 0, ..))
                    .and_then(|t| t.to_vec1::<f32>())
                    .map_err(|e| format!("bert batch cls: {}", e))?;
                let norm: f32 = cls.iter().map(|x| x * x).sum::<f32>().sqrt();
                let mut out = cls;
                if norm > 1e-12 {
                    for x in &mut out {
                        *x /= norm;
                    }
                }
                results[chunk[i].0] = out;
            }
        }
        Ok(results)
    }
}

pub struct MiniLmCrossEncoder {
    embeddings: MmapLiteEmbeddings,
    encoder: Mutex<BertEncoder>,
    pooler: Linear,
    classifier: Linear,
    tokenizer: WordPieceTokenizer,
    max_len: usize,
}

impl MiniLmCrossEncoder {
    pub fn load() -> Result<Self, String> {
        let dir = resolve_or_download_model(&RERANK_SPEC)?;
        let cfg_raw = fs::read_to_string(dir.join("config.json"))
            .map_err(|e| format!("read rerank config.json: {}", e))?;
        let cfg: BertConfig = serde_json::from_str(&cfg_raw)
            .map_err(|e| format!("parse rerank config.json: {}", e))?;
        let max_len = cfg
            .max_position_embeddings
            .min(rerank_max_tokens())
            .max(32);
        let hsz = cfg.hidden_size;
        let tokenizer = WordPieceTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let st_path = dir.join("model.safetensors");
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(&[st_path.as_path()], DType::F32, &Device::Cpu)
                .map_err(|e| format!("mmap rerank model.safetensors: {}", e))?
        };
        let embeddings = MmapLiteEmbeddings::load(
            &st_path,
            vb.pp("bert.embeddings"),
            &cfg,
            &["bert.embeddings.word_embeddings.weight", "embeddings.word_embeddings.weight"],
            None,
        )
        .or_else(|_| {
            MmapLiteEmbeddings::load(
                &st_path,
                vb.pp("embeddings"),
                &cfg,
                &["embeddings.word_embeddings.weight", "bert.embeddings.word_embeddings.weight"],
                None,
            )
        })?;
        let encoder = BertEncoder::load(vb.pp("bert.encoder"), &cfg)
            .or_else(|_| BertEncoder::load(vb.pp("encoder"), &cfg))
            .map_err(|e| format!("load MiniLM encoder: {}", e))?;
        let pooler = linear(hsz, hsz, vb.pp("bert.pooler.dense"))
            .or_else(|_| linear(hsz, hsz, vb.pp("pooler.dense")))
            .map_err(|e| format!("load MiniLM pooler: {}", e))?;
        let classifier = linear(hsz, 1, vb.pp("classifier"))
            .map_err(|e| format!("load MiniLM classifier: {}", e))?;
        Ok(Self {
            embeddings,
            encoder: Mutex::new(encoder),
            pooler,
            classifier,
            tokenizer,
            max_len,
        })
    }

    pub fn score_pairs(&self, query: &str, candidates: &[&str]) -> Result<Vec<f64>, String> {
        self.score_pairs_until(query, candidates, None)
    }

    pub fn score_pairs_until(
        &self,
        query: &str,
        candidates: &[&str],
        deadline: Option<Instant>,
    ) -> Result<Vec<f64>, String> {
        if candidates.is_empty() {
            return Ok(vec![]);
        }
        if deadline.map_or(false, |dl| Instant::now() >= dl) {
            return Err("rerank_budget_exceeded".to_string());
        }
        let q_clamped: String = query.chars().take(1024).collect();
        let mut all_scores = vec![0.0f64; candidates.len()];
        let dev = Device::Cpu;

        let mut indexed: Vec<(usize, Vec<u32>, Vec<u32>)> = candidates
            .iter()
            .enumerate()
            .map(|(idx, &doc)| {
                let d_clamped: String = doc.chars().take(2048).collect();
                let (ids, tids) = self.tokenizer.encode_pair(&q_clamped, &d_clamped, self.max_len);
                (idx, ids, tids)
            })
            .collect();
        indexed.sort_by_key(|(_, ids, _)| ids.len());

        for chunk in indexed.chunks(4) {
            if deadline.map_or(false, |dl| Instant::now() >= dl) {
                return Err("rerank_budget_exceeded".to_string());
            }
            let bsz = chunk.len();
            let max_seq = chunk.iter().map(|(_, ids, _)| ids.len()).max().unwrap_or(2).max(2);
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (_, ids, tids) in chunk {
                let n = ids.len();
                flat_ids.extend_from_slice(ids);
                flat_tids.extend_from_slice(tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(0u32).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let emb_out = self.embeddings.forward(&flat_ids, &flat_tids, bsz, max_seq, &dev)?;
            let attn_mask = build_extended_attn_mask(&flat_mask, bsz, max_seq, &dev)?;
            let hidden = {
                let guard = self.encoder.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&emb_out, &attn_mask)
                    .map_err(|e| format!("rerank forward: {}", e))?
            };
            let cls = hidden
                .i((.., 0, ..))
                .and_then(|t| t.contiguous())
                .map_err(|e| e.to_string())?;
            let pooled = self
                .pooler
                .forward(&cls)
                .and_then(|t| t.tanh())
                .map_err(|e| e.to_string())?;
            let logits = self
                .classifier
                .forward(&pooled)
                .and_then(|t| t.squeeze(1))
                .and_then(|t| t.to_vec1::<f32>())
                .map_err(|e| e.to_string())?;
            for (local_i, logit) in logits.into_iter().enumerate() {
                let x = logit as f64;
                let sig = if x >= 0.0 {
                    1.0 / (1.0 + (-x).exp())
                } else {
                    let ex = x.exp();
                    ex / (1.0 + ex)
                };
                all_scores[chunk[local_i].0] = sig;
            }
        }
        Ok(all_scores)
    }
}

// ---------------------------------------------------------------------------------------------
// Global Model Dispatch & Background Prewarm
// ---------------------------------------------------------------------------------------------

pub fn rerank_enabled() -> bool {
    if OPENAI_SKIP_UNCACHED_RERANK.load(Ordering::Acquire) {
        return false;
    }
    !matches!(
        env::var("MEMNOS_RERANK")
            .unwrap_or_else(|_| "1".to_string())
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "0" | "false" | "no" | "off"
    )
}

pub fn rerank_should_degrade_warming() -> bool {
    if !rerank_enabled() {
        return false;
    }
    PREWARM_STARTED.load(Ordering::Acquire) && !RERANK_READY.load(Ordering::Acquire)
}

pub fn rerank_max_tokens() -> usize {
    env::var("MEMNOS_RERANK_MAX_TOKENS")
        .ok()
        .and_then(|s| s.trim().parse::<usize>().ok())
        .unwrap_or(160)
        .clamp(32, 512)
}

pub fn rerank_budget_ms() -> f64 {
    env::var("MEMNOS_RERANK_BUDGET_MS")
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|&v| v > 0.0)
        .unwrap_or(600.0)
}

pub fn rerank_budget_duration() -> Duration {
    Duration::from_secs_f64((rerank_budget_ms() / 1000.0).max(0.01))
}

pub fn rerank_cap() -> usize {
    if let Ok(raw) = env::var("MEMNOS_RERANK_CAP") {
        if let Ok(n) = raw.trim().parse::<usize>() {
            return if n == 0 { usize::MAX } else { n };
        }
    }
    CALIBRATED_RERANK_CAP.load(Ordering::Acquire)
}

pub fn global_e5_embed(text: &str, is_query: bool) -> Result<Vec<f32>, String> {
    let prefixed = if is_query {
        format!("query: {}", text)
    } else {
        format!("passage: {}", text)
    };
    let res = E5_INSTANCE.get_or_init(E5MultilingualModel::load);
    match res {
        Ok(m) => m.embed(&prefixed),
        Err(e) => Err(e.clone()),
    }
}

pub fn global_bge_embed(text: &str) -> Result<Vec<f32>, String> {
    let res = BGE_INSTANCE.get_or_init(BgeEmbedModel::load);
    match res {
        Ok(m) => m.embed(text),
        Err(e) => Err(e.clone()),
    }
}

pub fn global_local_embed(model_id: &str, text: &str, is_query: bool) -> Result<Vec<f32>, String> {
    if model_id.eq_ignore_ascii_case(BGE_REPO_ID)
        || model_id.eq_ignore_ascii_case("bge-small-en-v1.5")
        || model_id.eq_ignore_ascii_case("local-384")
    {
        global_bge_embed(text)
    } else {
        global_e5_embed(text, is_query)
    }
}

pub fn global_local_embed_batch(
    model_id: &str,
    texts: &[String],
    is_query: bool,
) -> Result<Vec<Vec<f32>>, String> {
    if model_id.eq_ignore_ascii_case(BGE_REPO_ID)
        || model_id.eq_ignore_ascii_case("bge-small-en-v1.5")
        || model_id.eq_ignore_ascii_case("local-384")
    {
        let res = BGE_INSTANCE.get_or_init(BgeEmbedModel::load);
        match res {
            Ok(m) => m.embed_batch(texts),
            Err(e) => Err(e.clone()),
        }
    } else {
        let prefixed: Vec<String> = texts
            .iter()
            .map(|t| {
                if is_query {
                    format!("query: {}", t)
                } else {
                    format!("passage: {}", t)
                }
            })
            .collect();
        let res = E5_INSTANCE.get_or_init(E5MultilingualModel::load);
        match res {
            Ok(m) => m.embed_batch(&prefixed),
            Err(e) => Err(e.clone()),
        }
    }
}

fn score_with_active_reranker(
    query: &str,
    candidates: &[&str],
    deadline: Option<Instant>,
) -> Result<Vec<f64>, String> {
    if ACTIVE_RERANK_MULTILINGUAL.load(Ordering::Acquire) {
        let res = MMARCO_RERANK_INSTANCE.get_or_init(|| {
            let loaded = MmarcoCrossEncoder::load();
            if loaded.is_ok() {
                RERANK_READY.store(true, Ordering::Release);
            }
            loaded
        });
        match res {
            Ok(r) => {
                RERANK_READY.store(true, Ordering::Release);
                r.score_pairs_until(query, candidates, deadline)
            }
            Err(e) => Err(e.clone()),
        }
    } else {
        let res = MINILM_RERANK_INSTANCE.get_or_init(|| {
            let loaded = MiniLmCrossEncoder::load();
            if loaded.is_ok() {
                RERANK_READY.store(true, Ordering::Release);
            }
            loaded
        });
        match res {
            Ok(r) => {
                RERANK_READY.store(true, Ordering::Release);
                r.score_pairs_until(query, candidates, deadline)
            }
            Err(e) => Err(e.clone()),
        }
    }
}

enum InferJob {
    Prewarm {
        model_id: String,
        warm_embedder: bool,
    },
    Rerank {
        query: String,
        texts: Vec<String>,
        deadline: Option<Instant>,
        reply: tokio::sync::oneshot::Sender<Result<Vec<(usize, f64)>, String>>,
    },
}

static INFER_WORKER_TX: Lazy<std::sync::mpsc::Sender<InferJob>> = Lazy::new(|| {
    let (tx, rx) = std::sync::mpsc::channel::<InferJob>();
    let _ = std::thread::Builder::new()
        .name("memnos-infer".to_string())
        .spawn(move || {
            while let Ok(job) = rx.recv() {
                match job {
                    InferJob::Prewarm {
                        model_id,
                        warm_embedder,
                    } => {
                        run_prewarm_on_worker(&model_id, warm_embedder);
                    }
                    InferJob::Rerank {
                        query,
                        texts,
                        deadline,
                        reply,
                    } => {
                        if deadline.map_or(false, |dl| Instant::now() >= dl) {
                            let _ = reply.send(Err("rerank_budget_exceeded".to_string()));
                            continue;
                        }
                        let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
                        let res = rerank_candidates_until(&query, &refs, deadline);
                        let _ = reply.send(res);
                    }
                }
            }
        });
    tx
});

pub async fn rerank_async(
    query: String,
    texts: Vec<String>,
    deadline: Option<Instant>,
) -> Result<Vec<(usize, f64)>, String> {
    if texts.is_empty() {
        return Ok(vec![]);
    }
    if deadline.map_or(false, |dl| Instant::now() >= dl) {
        return Err("rerank_budget_exceeded".to_string());
    }
    let (tx, rx) = tokio::sync::oneshot::channel();
    INFER_WORKER_TX
        .send(InferJob::Rerank {
            query,
            texts,
            deadline,
            reply: tx,
        })
        .map_err(|_| "inference worker unavailable".to_string())?;
    rx.await.map_err(|_| "inference worker dropped".to_string())?
}

pub fn rerank_candidates(query: &str, candidates: &[&str]) -> Result<Vec<(usize, f64)>, String> {
    rerank_candidates_until(query, candidates, None)
}

pub fn rerank_candidates_until(
    query: &str,
    candidates: &[&str],
    deadline: Option<Instant>,
) -> Result<Vec<(usize, f64)>, String> {
    if candidates.is_empty() {
        return Ok(vec![]);
    }
    let cap = rerank_cap().min(candidates.len());
    let head = &candidates[..cap];
    let head_scores = score_with_active_reranker(query, head, deadline)?;
    let min_score = head_scores
        .iter()
        .copied()
        .fold(f64::INFINITY, f64::min)
        .min(1.0);
    let mut out: Vec<(usize, f64)> = head_scores.into_iter().enumerate().collect();
    out.sort_by(|a, b| {
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.0.cmp(&b.0))
    });
    if cap < candidates.len() {
        for i in cap..candidates.len() {
            let tail_score = (min_score - 1e-4 * ((i - cap + 1) as f64)).max(0.0);
            out.push((i, tail_score));
        }
    }
    Ok(out)
}

pub fn spawn_background_prewarm_for(embed_model: &str) {
    spawn_background_prewarm_opts(embed_model, true);
}

fn run_prewarm_on_worker(model_owned: &str, warm_embedder: bool) {
    if warm_embedder {
        match global_local_embed(model_owned, "warmup", true) {
            Ok(_) => {
                EMBED_READY.store(true, Ordering::Release);
            }
            Err(e) => {
                if let Ok(mut g) = PREWARM_ERR.lock() {
                    *g = Some(e);
                }
                return;
            }
        }
    }
    if rerank_enabled() {
        match score_with_active_reranker("warmup query", &["warmup candidate passage"], None) {
            Ok(_) => {
                // Calibrate on realistic multi-sentence ~200-token turns matching production
                // `raw_turns` lengths so `ms_per_pair` reflects full cross-encoder sequence cost.
                let sample = [
                    "During the quarterly reliability review we migrated the primary billing and ledger service onto PostgreSQL 16 with pgvector halfvec HNSW indexes, configured streaming WAL replication across two availability zones, and verified zero-downtime blue-green HTTP gateway cutovers under sustained load. Before any schema migration or binary swap runs on a developer workstation or production node, the operator tool creates a 0600-permissioned pg_dump custom archive and validates its table of contents via pg_restore --list so rollback is deterministic.",
                    "The local edge gateway enforces per-token sliding-window rate limits at 500 requests per second while preserving strict namespace RBAC grants,Constant-Time HMAC-SHA256 webhook signatures, and automatic orphan child-server shutdown if the parent gateway process exits unexpectedly. Session identity resolution scopes active terminal bindings by controlling TTY device and process lineage so two concurrent agent sessions in separate worktrees never collide on shared project namespaces.",
                    "Nightly encrypted backups stream to object storage in us-east-2 with 35-day point-in-time recovery retention, automated Thursday restore drills against an ephemeral PostgreSQL instance, and SHA-256 verification of all pinned local neural model artifacts cached under the user profile directory. Offline air-gapped deployments can set MEMNOS_OFFLINE_MODELS=1 after pre-populating the verified cache.",
                    "Ada relocated to Lisbon in May 2026 to lead the distributed storage engineering group responsible for bi-temporal fact supersession, single-valued predicate conflict detection, cross-lingual semantic recall across 100 languages, and bounded CPU cross-encoder reranking with strict per-request wall-clock latency budgets.",
                ];
                let t0 = Instant::now();
                if score_with_active_reranker(
                    "how are pre-upgrade database backups verified before a binary cutover and where are nightly archives stored?",
                    &sample,
                    None,
                )
                .is_ok()
                {
                    let ms_per_pair =
                        (t0.elapsed().as_secs_f64() * 1000.0 / sample.len() as f64).max(0.5);
                    let budget_ms = rerank_budget_ms();
                    let min_cap: usize = env::var("MEMNOS_RERANK_MIN_CAP")
                        .ok()
                        .and_then(|s| s.parse().ok())
                        .unwrap_or(8)
                        .max(1);
                    let max_cap: usize = env::var("MEMNOS_RERANK_MAX_CAP")
                        .ok()
                        .and_then(|s| s.parse().ok())
                        .unwrap_or(16)
                        .max(min_cap);
                    // Budget is shared across both fact and raw-turn rerank passes (~half each).
                    let derived = (((budget_ms * 0.5) / ms_per_pair).floor() as usize)
                        .clamp(min_cap, max_cap);
                    CALIBRATED_RERANK_CAP.store(derived, Ordering::Release);
                }
                RERANK_READY.store(true, Ordering::Release);
            }
            Err(e) => {
                if let Ok(mut g) = PREWARM_ERR.lock() {
                    *g = Some(e);
                }
            }
        }
    } else {
        RERANK_READY.store(true, Ordering::Release);
    }
}

pub fn spawn_background_prewarm_opts(embed_model: &str, warm_embedder: bool) {
    set_active_reranker_for_embed_model(embed_model);
    if PREWARM_STARTED.swap(true, Ordering::AcqRel) {
        return;
    }
    if !warm_embedder {
        EMBED_READY.store(true, Ordering::Release);
        let r_spec = active_reranker_spec(embed_model);
        if !is_model_cached(r_spec) {
            OPENAI_SKIP_UNCACHED_RERANK.store(true, Ordering::Release);
            return;
        }
    }
    let _ = INFER_WORKER_TX.send(InferJob::Prewarm {
        model_id: embed_model.to_string(),
        warm_embedder,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bge_small_and_minilm_parity_with_fastembed() {
        if find_cached_model_dir(&BGE_SPEC).is_none() || find_cached_model_dir(&RERANK_SPEC).is_none() {
            eprintln!("BGE/MiniLM cache not pre-populated in this runner; skipping");
            return;
        }
        let bge = BgeEmbedModel::load().expect("load BgeEmbedModel");
        let v = bge
            .embed("Ada moved to Lisbon in May 2026.")
            .expect("embed sentence");
        assert_eq!(v.len(), 384);
        let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 1e-4, "expected unit norm, got {norm}");
        let py_head = [0.022462f32, -0.016924, 0.070885, -0.024353, -0.014506, 0.009009];
        for (i, &expected) in py_head.iter().enumerate() {
            assert!(
                (v[i] - expected).abs() < 0.002,
                "dim {i} drifted: actual={} expected={expected}",
                v[i]
            );
        }
        let qv = bge
            .embed("Which European coastal city did Ada relocate to?")
            .unwrap();
        let irrelevant = bge
            .embed("Configure PostgreSQL autovacuum scale factor.")
            .unwrap();
        let cos_rel: f32 = qv.iter().zip(&v).map(|(a, b)| a * b).sum();
        let cos_irr: f32 = qv.iter().zip(&irrelevant).map(|(a, b)| a * b).sum();
        assert!(
            cos_rel > cos_irr + 0.20,
            "expected semantic match > irrelevant ({cos_rel} vs {cos_irr})"
        );

        let ce = MiniLmCrossEncoder::load().expect("load MiniLmCrossEncoder");
        let scores = ce
            .score_pairs(
                "where does Ada live?",
                &[
                    "Ada moved to Lisbon in May 2026.",
                    "Bob prefers tea over coffee.",
                ],
            )
            .expect("score pairs");
        assert!(
            (scores[0] - 0.972169).abs() < 0.005,
            "expected ~0.972169 for relevant doc, got {}",
            scores[0]
        );
        assert!(
            scores[1] < 0.0005,
            "expected ~0.000013 for irrelevant doc, got {}",
            scores[1]
        );
    }

    #[test]
    fn multilingual_e5_and_mmarco_cross_lingual_recall() {
        if find_cached_model_dir(&E5_MULTI_SPEC).is_none()
            || find_cached_model_dir(&MMARCO_RERANK_SPEC).is_none()
        {
            eprintln!("Multilingual E5/mMARCO cache not yet downloaded; skipping in offline unit run");
            return;
        }
        let e5 = E5MultilingualModel::load().expect("load E5MultilingualModel");
        // Cross-lingual test: Spanish + Arabic + Japanese passages queried in English!
        let doc_es = e5
            .embed("passage: Ada se mudó a Lisboa en mayo de 2026 para liderar el equipo europeo.")
            .unwrap();
        let doc_ar = e5
            .embed("passage: قاعدة البيانات الرئيسية تستخدم PostgreSQL مع فهارس المتجهات.")
            .unwrap();
        let q_en = e5
            .embed("query: Which city did Ada move to in May 2026?")
            .unwrap();
        let cos_es: f32 = q_en.iter().zip(&doc_es).map(|(a, b)| a * b).sum();
        let cos_ar: f32 = q_en.iter().zip(&doc_ar).map(|(a, b)| a * b).sum();
        assert!(
            cos_es > cos_ar + 0.08,
            "expected cross-lingual EN->ES match > unrelated AR doc ({cos_es} vs {cos_ar})"
        );

        let mm = MmarcoCrossEncoder::load().expect("load MmarcoCrossEncoder");
        let scores = mm
            .score_pairs(
                "¿En qué ciudad vive Ada?",
                &[
                    "Ada se mudó a Lisboa en mayo de 2026.",
                    "El servidor utiliza PostgreSQL para almacenamiento.",
                ],
            )
            .unwrap();
        assert!(
            scores[0] > scores[1] + 0.5,
            "expected multilingual reranker to rank relevant Spanish doc #1: {:?}",
            scores
        );
    }
}
