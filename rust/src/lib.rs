//! `memnos_core` — Unified Rust library backing the single `memnos` binary.

pub mod agent_setup;
pub mod code_anchors;
pub mod config;
pub mod control;
pub mod db;
pub mod embed;
pub mod extract;
pub mod gateway;
pub mod hooks;
pub mod mcp;
pub mod nsresolve;
pub mod offline_queue;
pub mod predicates;
pub mod proxy;
pub mod redact;
pub mod repo_init;
pub mod rerank;
pub mod server;
pub mod store;
pub mod temporal;
pub mod vault;
