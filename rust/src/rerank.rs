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
use candle_nn::{linear, Linear, VarBuilder};
use candle_transformers::models::bert::{BertModel, Config as BertConfig};
use candle_transformers::models::xlm_roberta::{
    Config as XlmRobertaConfig, XLMRobertaForSequenceClassification,
};
use once_cell::sync::{Lazy, OnceCell};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{Read, Write};
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

static RERANK_READY: AtomicBool = AtomicBool::new(false);
static PREWARM_STARTED: AtomicBool = AtomicBool::new(false);
static NOTICE_PRINTED: AtomicBool = AtomicBool::new(false);
static ACTIVE_RERANK_MULTILINGUAL: AtomicBool = AtomicBool::new(true);
static CALIBRATED_RERANK_CAP: AtomicUsize = AtomicUsize::new(100);
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

pub fn set_active_reranker_for_embed_model(embed_model: &str) {
    let is_multi = embed_model.eq_ignore_ascii_case(E5_MULTI_REPO_ID)
        || embed_model.eq_ignore_ascii_case("multilingual-e5-small");
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
    if matches!(
        env::var("MEMNOS_OFFLINE_MODELS")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    ) {
        return Err(format!(
            "local model '{}' is not yet downloaded and MEMNOS_OFFLINE_MODELS=1 — run `memnos models pull` while online (or configure OPENAI_API_KEY)",
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
    let (emb_spec, rerank_spec) = specs_for_embed_model(embed_model);
    let needed_download = !is_model_cached(emb_spec)
        || (rerank_enabled() && !is_model_cached(rerank_spec));
    resolve_or_download_model(emb_spec)?;
    if rerank_enabled() {
        resolve_or_download_model(rerank_spec)?;
    }
    if needed_download {
        eprintln!("  ✓ Local neural models ready — memnos now runs embeddings & recall without external AI.\n");
    }
    Ok(())
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
// Pure-Rust SentencePiece Unigram Viterbi Tokenizer (XLM-RoBERTa / `multilingual-e5-small`)
// ---------------------------------------------------------------------------------------------

pub struct UnigramTokenizer {
    scores: HashMap<String, (u32, f64)>,
    max_piece_bytes: usize,
    bos_id: u32,
    pad_id: u32,
    eos_id: u32,
    unk_id: u32,
}

impl UnigramTokenizer {
    pub fn from_file(path: &Path) -> Result<Self, String> {
        let raw = fs::read_to_string(path)
            .map_err(|e| format!("read tokenizer {}: {}", path.display(), e))?;
        let doc: Value = serde_json::from_str(&raw)
            .map_err(|e| format!("parse tokenizer {}: {}", path.display(), e))?;
        let vocab_arr = doc
            .get("model")
            .and_then(|m| m.get("vocab"))
            .and_then(|v| v.as_array())
            .ok_or_else(|| format!("missing model.vocab array in {}", path.display()))?;

        let mut scores = HashMap::with_capacity(vocab_arr.len());
        let mut max_piece_bytes = 1usize;
        for (idx, item) in vocab_arr.iter().enumerate() {
            if let Some(pair) = item.as_array() {
                if let (Some(piece), Some(sc)) = (
                    pair.first().and_then(|x| x.as_str()),
                    pair.get(1).and_then(|x| x.as_f64()),
                ) {
                    max_piece_bytes = max_piece_bytes.max(piece.len());
                    scores.insert(piece.to_string(), (idx as u32, sc));
                }
            }
        }
        let unk_id = doc
            .get("model")
            .and_then(|m| m.get("unk_id"))
            .and_then(|v| v.as_u64())
            .unwrap_or(3) as u32;
        let bos_id = scores.get("<s>").map(|x| x.0).unwrap_or(0);
        let pad_id = scores.get("<pad>").map(|x| x.0).unwrap_or(1);
        let eos_id = scores.get("</s>").map(|x| x.0).unwrap_or(2);
        Ok(Self {
            scores,
            max_piece_bytes,
            bos_id,
            pad_id,
            eos_id,
            unk_id,
        })
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
                if let Some(&(tid, sc)) = self.scores.get(sub) {
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
        for piece in norm.split_whitespace() {
            let prefixed = format!("\u{2581}{}", piece);
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

pub struct WordPieceTokenizer {
    vocab: HashMap<String, u32>,
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
        let raw = fs::read_to_string(path)
            .map_err(|e| format!("read tokenizer {}: {}", path.display(), e))?;
        let doc: Value = serde_json::from_str(&raw)
            .map_err(|e| format!("parse tokenizer {}: {}", path.display(), e))?;
        let vocab_obj = doc
            .get("model")
            .and_then(|m| m.get("vocab"))
            .and_then(|v| v.as_object())
            .ok_or_else(|| format!("missing model.vocab in {}", path.display()))?;
        let mut vocab = HashMap::with_capacity(vocab_obj.len());
        for (tok, id_val) in vocab_obj {
            if let Some(id) = id_val.as_u64() {
                vocab.insert(tok.clone(), id as u32);
            }
        }
        let max_word_chars = doc
            .get("model")
            .and_then(|m| m.get("max_input_chars_per_word"))
            .and_then(|v| v.as_u64())
            .unwrap_or(100) as usize;
        let unk_id = vocab.get("[UNK]").copied().unwrap_or(100);
        let cls_id = vocab.get("[CLS]").copied().unwrap_or(101);
        let sep_id = vocab.get("[SEP]").copied().unwrap_or(102);
        Ok(Self {
            vocab,
            unk_id,
            cls_id,
            sep_id,
            max_word_chars,
        })
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
                        self.vocab.get(slice).copied()
                    } else {
                        let prefixed = format!("##{}", slice);
                        self.vocab.get(&prefixed).copied()
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
// Multilingual E5 Embedder (`intfloat/multilingual-e5-small`) & mMARCO Reranker
// ---------------------------------------------------------------------------------------------

pub struct E5MultilingualModel {
    bert: Mutex<BertModel>,
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
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(
                &[dir.join("model.safetensors")],
                DType::F32,
                &Device::Cpu,
            )
            .map_err(|e| format!("mmap e5 model.safetensors: {}", e))?
        };
        let bert = BertModel::load(vb.clone(), &cfg)
            .or_else(|_| BertModel::load(vb.pp("bert"), &cfg))
            .map_err(|e| format!("load E5MultilingualModel: {}", e))?;
        Ok(Self {
            bert: Mutex::new(bert),
            tokenizer,
            max_len,
        })
    }

    pub fn embed(&self, prefixed_text: &str) -> Result<Vec<f32>, String> {
        let (ids, type_ids) = self.tokenizer.encode_single(prefixed_text, self.max_len);
        let dev = Device::Cpu;
        let input_ids = Tensor::new(ids.as_slice(), &dev)
            .and_then(|t| t.unsqueeze(0))
            .map_err(|e| e.to_string())?;
        let token_type_ids = Tensor::new(type_ids.as_slice(), &dev)
            .and_then(|t| t.unsqueeze(0))
            .map_err(|e| e.to_string())?;
        let hidden = {
            let guard = self.bert.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&input_ids, &token_type_ids, None)
                .map_err(|e| format!("e5 forward: {}", e))?
        };
        // Mean-pool over sequence tokens `[1, seq_len, 384]` -> `[384]` + L2 normalize
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
        let mut encs = Vec::with_capacity(prefixed_texts.len());
        let mut max_seq = 2usize;
        for t in prefixed_texts {
            let (ids, tids) = self.tokenizer.encode_single(t, self.max_len);
            max_seq = max_seq.max(ids.len());
            encs.push((ids, tids));
        }
        let bsz = encs.len();
        let mut orig_lens = Vec::with_capacity(bsz);
        let mut flat_ids = Vec::with_capacity(bsz * max_seq);
        let mut flat_tids = Vec::with_capacity(bsz * max_seq);
        let mut flat_mask = Vec::with_capacity(bsz * max_seq);
        for (ids, tids) in encs {
            let n = ids.len();
            orig_lens.push(n);
            flat_ids.extend_from_slice(&ids);
            flat_tids.extend_from_slice(&tids);
            flat_mask.extend(std::iter::repeat(1u32).take(n));
            if n < max_seq {
                let pad = max_seq - n;
                flat_ids.extend(std::iter::repeat(pad_id).take(pad));
                flat_tids.extend(std::iter::repeat(0u32).take(pad));
                flat_mask.extend(std::iter::repeat(0u32).take(pad));
            }
        }
        let input_ids =
            Tensor::from_vec(flat_ids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let token_type_ids =
            Tensor::from_vec(flat_tids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let attn_mask =
            Tensor::from_vec(flat_mask, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let hidden = {
            let guard = self.bert.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&input_ids, &token_type_ids, Some(&attn_mask))
                .map_err(|e| format!("e5 batch forward: {}", e))?
        };
        let mut results = Vec::with_capacity(bsz);
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
            results.push(out);
        }
        Ok(results)
    }
}

pub struct MmarcoCrossEncoder {
    model: Mutex<XLMRobertaForSequenceClassification>,
    tokenizer: UnigramTokenizer,
    max_len: usize,
}

impl MmarcoCrossEncoder {
    pub fn load() -> Result<Self, String> {
        let dir = resolve_or_download_model(&MMARCO_RERANK_SPEC)?;
        let cfg_raw = fs::read_to_string(dir.join("config.json"))
            .map_err(|e| format!("read mmarco config.json: {}", e))?;
        let cfg: XlmRobertaConfig = serde_json::from_str(&cfg_raw)
            .map_err(|e| format!("parse mmarco config.json: {}", e))?;
        let max_len = cfg.max_position_embeddings.saturating_sub(2).min(512).max(32);
        let tokenizer = UnigramTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(
                &[dir.join("model.safetensors")],
                DType::F32,
                &Device::Cpu,
            )
            .map_err(|e| format!("mmap mmarco model.safetensors: {}", e))?
        };
        let model = XLMRobertaForSequenceClassification::new(1, &cfg, vb)
            .map_err(|e| format!("load MmarcoCrossEncoder: {}", e))?;
        Ok(Self {
            model: Mutex::new(model),
            tokenizer,
            max_len,
        })
    }

    pub fn score_pairs(&self, query: &str, candidates: &[&str]) -> Result<Vec<f64>, String> {
        if candidates.is_empty() {
            return Ok(vec![]);
        }
        let q_clamped: String = query.chars().take(1024).collect();
        let mut all_scores = Vec::with_capacity(candidates.len());
        let dev = Device::Cpu;
        let pad_id = self.tokenizer.pad_id();

        for chunk in candidates.chunks(16) {
            let mut encs = Vec::with_capacity(chunk.len());
            let mut max_seq = 4usize;
            for &doc in chunk {
                let d_clamped: String = doc.chars().take(2048).collect();
                let (ids, tids) = self.tokenizer.encode_pair(&q_clamped, &d_clamped, self.max_len);
                max_seq = max_seq.max(ids.len());
                encs.push((ids, tids));
            }
            let bsz = encs.len();
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (ids, tids) in encs {
                let n = ids.len();
                flat_ids.extend_from_slice(&ids);
                flat_tids.extend_from_slice(&tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(pad_id).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let input_ids =
                Tensor::from_vec(flat_ids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
            let token_type_ids =
                Tensor::from_vec(flat_tids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
            let attn_mask =
                Tensor::from_vec(flat_mask, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;

            let logits_tensor = {
                let guard = self.model.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&input_ids, &attn_mask, &token_type_ids)
                    .map_err(|e| format!("mmarco forward: {}", e))?
            };
            let logits = logits_tensor
                .squeeze(1)
                .and_then(|t| t.to_vec1::<f32>())
                .map_err(|e| format!("mmarco logits: {}", e))?;
            for logit in logits {
                let x = logit as f64;
                let sig = if x >= 0.0 {
                    1.0 / (1.0 + (-x).exp())
                } else {
                    let ex = x.exp();
                    ex / (1.0 + ex)
                };
                all_scores.push(sig);
            }
        }
        Ok(all_scores)
    }
}

// ---------------------------------------------------------------------------------------------
// Backward-Compatible English BGE Embedder (`BAAI/bge-small-en-v1.5`) & MiniLM Reranker
// ---------------------------------------------------------------------------------------------

pub struct BgeEmbedModel {
    bert: Mutex<BertModel>,
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
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(
                &[dir.join("model.safetensors")],
                DType::F32,
                &Device::Cpu,
            )
            .map_err(|e| format!("mmap bge model.safetensors: {}", e))?
        };
        let bert = BertModel::load(vb.clone(), &cfg)
            .or_else(|_| BertModel::load(vb.pp("bert"), &cfg))
            .map_err(|e| format!("load BgeEmbedModel: {}", e))?;
        Ok(Self {
            bert: Mutex::new(bert),
            tokenizer,
            max_len,
        })
    }

    pub fn embed(&self, text: &str) -> Result<Vec<f32>, String> {
        let (ids, type_ids) = self.tokenizer.encode_single(text, self.max_len);
        let dev = Device::Cpu;
        let input_ids = Tensor::new(ids.as_slice(), &dev)
            .and_then(|t| t.unsqueeze(0))
            .map_err(|e| e.to_string())?;
        let token_type_ids = Tensor::new(type_ids.as_slice(), &dev)
            .and_then(|t| t.unsqueeze(0))
            .map_err(|e| e.to_string())?;
        let hidden = {
            let guard = self.bert.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&input_ids, &token_type_ids, None)
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
        let mut encs = Vec::with_capacity(texts.len());
        let mut max_seq = 2usize;
        for t in texts {
            let (ids, tids) = self.tokenizer.encode_single(t, self.max_len);
            max_seq = max_seq.max(ids.len());
            encs.push((ids, tids));
        }
        let bsz = encs.len();
        let mut flat_ids = Vec::with_capacity(bsz * max_seq);
        let mut flat_tids = Vec::with_capacity(bsz * max_seq);
        let mut flat_mask = Vec::with_capacity(bsz * max_seq);
        for (ids, tids) in encs {
            let n = ids.len();
            flat_ids.extend_from_slice(&ids);
            flat_tids.extend_from_slice(&tids);
            flat_mask.extend(std::iter::repeat(1u32).take(n));
            if n < max_seq {
                let pad = max_seq - n;
                flat_ids.extend(std::iter::repeat(0u32).take(pad));
                flat_tids.extend(std::iter::repeat(0u32).take(pad));
                flat_mask.extend(std::iter::repeat(0u32).take(pad));
            }
        }
        let input_ids =
            Tensor::from_vec(flat_ids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let token_type_ids =
            Tensor::from_vec(flat_tids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let attn_mask =
            Tensor::from_vec(flat_mask, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
        let hidden = {
            let guard = self.bert.lock().map_err(|e| e.to_string())?;
            guard
                .forward(&input_ids, &token_type_ids, Some(&attn_mask))
                .map_err(|e| format!("bert batch forward: {}", e))?
        };
        let mut results = Vec::with_capacity(bsz);
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
            results.push(out);
        }
        Ok(results)
    }
}

pub struct MiniLmCrossEncoder {
    bert: Mutex<BertModel>,
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
        let max_len = cfg.max_position_embeddings.min(512).max(32);
        let hsz = cfg.hidden_size;
        let tokenizer = WordPieceTokenizer::from_file(&dir.join("tokenizer.json"))?;
        let vb = unsafe {
            VarBuilder::from_mmaped_safetensors(
                &[dir.join("model.safetensors")],
                DType::F32,
                &Device::Cpu,
            )
            .map_err(|e| format!("mmap rerank model.safetensors: {}", e))?
        };
        let bert = BertModel::load(vb.pp("bert"), &cfg)
            .or_else(|_| BertModel::load(vb.clone(), &cfg))
            .map_err(|e| format!("load MiniLM bert: {}", e))?;
        let pooler = linear(hsz, hsz, vb.pp("bert.pooler.dense"))
            .or_else(|_| linear(hsz, hsz, vb.pp("pooler.dense")))
            .map_err(|e| format!("load MiniLM pooler: {}", e))?;
        let classifier = linear(hsz, 1, vb.pp("classifier"))
            .map_err(|e| format!("load MiniLM classifier: {}", e))?;
        Ok(Self {
            bert: Mutex::new(bert),
            pooler,
            classifier,
            tokenizer,
            max_len,
        })
    }

    pub fn score_pairs(&self, query: &str, candidates: &[&str]) -> Result<Vec<f64>, String> {
        if candidates.is_empty() {
            return Ok(vec![]);
        }
        let q_clamped: String = query.chars().take(1024).collect();
        let mut all_scores = Vec::with_capacity(candidates.len());
        let dev = Device::Cpu;

        for chunk in candidates.chunks(16) {
            let mut encs = Vec::with_capacity(chunk.len());
            let mut max_seq = 2usize;
            for &doc in chunk {
                let d_clamped: String = doc.chars().take(2048).collect();
                let (ids, tids) = self.tokenizer.encode_pair(&q_clamped, &d_clamped, self.max_len);
                max_seq = max_seq.max(ids.len());
                encs.push((ids, tids));
            }
            let bsz = encs.len();
            let mut flat_ids = Vec::with_capacity(bsz * max_seq);
            let mut flat_tids = Vec::with_capacity(bsz * max_seq);
            let mut flat_mask = Vec::with_capacity(bsz * max_seq);
            for (ids, tids) in encs {
                let n = ids.len();
                flat_ids.extend_from_slice(&ids);
                flat_tids.extend_from_slice(&tids);
                flat_mask.extend(std::iter::repeat(1u32).take(n));
                if n < max_seq {
                    let pad = max_seq - n;
                    flat_ids.extend(std::iter::repeat(0u32).take(pad));
                    flat_tids.extend(std::iter::repeat(0u32).take(pad));
                    flat_mask.extend(std::iter::repeat(0u32).take(pad));
                }
            }
            let input_ids =
                Tensor::from_vec(flat_ids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
            let token_type_ids =
                Tensor::from_vec(flat_tids, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;
            let attn_mask =
                Tensor::from_vec(flat_mask, (bsz, max_seq), &dev).map_err(|e| e.to_string())?;

            let hidden = {
                let guard = self.bert.lock().map_err(|e| e.to_string())?;
                guard
                    .forward(&input_ids, &token_type_ids, Some(&attn_mask))
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
            for logit in logits {
                let x = logit as f64;
                let sig = if x >= 0.0 {
                    1.0 / (1.0 + (-x).exp())
                } else {
                    let ex = x.exp();
                    ex / (1.0 + ex)
                };
                all_scores.push(sig);
            }
        }
        Ok(all_scores)
    }
}

// ---------------------------------------------------------------------------------------------
// Global Model Dispatch & Background Prewarm
// ---------------------------------------------------------------------------------------------

pub fn rerank_enabled() -> bool {
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

fn score_with_active_reranker(query: &str, candidates: &[&str]) -> Result<Vec<f64>, String> {
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
                r.score_pairs(query, candidates)
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
                r.score_pairs(query, candidates)
            }
            Err(e) => Err(e.clone()),
        }
    }
}

pub fn rerank_candidates(query: &str, candidates: &[&str]) -> Result<Vec<(usize, f64)>, String> {
    if candidates.is_empty() {
        return Ok(vec![]);
    }
    let cap = rerank_cap().min(candidates.len());
    let head = &candidates[..cap];
    let head_scores = score_with_active_reranker(query, head)?;
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

pub fn spawn_background_prewarm_opts(embed_model: &str, warm_embedder: bool) {
    set_active_reranker_for_embed_model(embed_model);
    if PREWARM_STARTED.swap(true, Ordering::AcqRel) {
        return;
    }
    let model_owned = embed_model.to_string();
    std::thread::spawn(move || {
        if warm_embedder {
            let _ = global_local_embed(&model_owned, "warmup", true);
        }
        if rerank_enabled() {
            let _ = score_with_active_reranker("warmup query", &["warmup candidate passage"]);
            // Self-calibrate rerank cap against MEMNOS_RERANK_BUDGET_MS (default 1500ms, 8..=100)
            let sample = [
                "Service alpha deployed version 2.1.0 with PostgreSQL pgvector storage.",
                "Edge gateway enforces token-bucket rate limiting at 500 requests per second.",
                "Nightly WAL backups stream to encrypted object storage in us-east-2.",
                "Ada moved to Lisbon in May 2026 to lead the platform engineering team.",
            ];
            let t0 = Instant::now();
            if score_with_active_reranker("where are backups stored and which version is deployed?", &sample).is_ok() {
                let ms_per_pair = (t0.elapsed().as_secs_f64() * 1000.0 / sample.len() as f64).max(0.5);
                let budget_ms: f64 = env::var("MEMNOS_RERANK_BUDGET_MS")
                    .ok()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(1500.0);
                let derived = ((budget_ms / ms_per_pair).floor() as usize).clamp(8, 100);
                CALIBRATED_RERANK_CAP.store(derived, Ordering::Release);
            }
            RERANK_READY.store(true, Ordering::Release);
        }
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
