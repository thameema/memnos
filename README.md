# memnos

[![CI](https://github.com/thameema/memnos/actions/workflows/ci.yml/badge.svg)](https://github.com/thameema/memnos/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Engine: PostgreSQL + pgvector](https://img.shields.io/badge/engine-PostgreSQL%20%2B%20pgvector-336791.svg)](#)
[![Query-time LLM: none](https://img.shields.io/badge/query--time%20LLM-none-success.svg)](#)
[![LoCoMo](https://img.shields.io/badge/LoCoMo%20full--10-64–65%25-success.svg)](benchmarks/README.md)
[![PyPI](https://img.shields.io/pypi/v/memnos.svg)](https://pypi.org/project/memnos/)

**Self-hostable, governed, vendor-neutral memory for AI agents — on one PostgreSQL.**

memnos gives AI agents long-term memory that persists across sessions, with **governance
built in** (token auth, namespace ACL, audit, an encrypted secret vault) and **no vendor
lock-in**. It runs on a single **PostgreSQL + pgvector** database — no second vector store,
no graph database — and uses **no LLM at query time** (retrieval is hybrid search + a local
cross-encoder reranker). Works with **Claude Code, Cursor, Windsurf** and any MCP client,
plus a REST API and a cross-platform CLI.

> **Released on PyPI** (`uv tool install memnos`). Apache-2.0 · self-hostable · single-org · local-first.

```
Claude Code ─┐
Cursor       ├─ MCP (stdio) ─┐
Windsurf     ─┘              │
hooks (auto) ───────────────┼─► memnos server ──► PostgreSQL + pgvector  (ONE engine)
REST / CLI ─────────────────┘     ├─ hybrid retrieve: pgvector (HNSW) + BM25 (tsvector) → RRF
                                   │   → cross-encoder rerank → quota + timeline + entity arms
                                   │   (NO LLM at query time)
                                   ├─ bi-temporal facts + belief-change supersession
                                   ├─ governance: token auth · namespace ACL · audit · usage
                                   └─ encrypted secret vault (AES-256-GCM) + ingest redaction
```

---

## What makes memnos different

- **One engine.** Everything lives in a single PostgreSQL + pgvector — no second vector
  store, no graph database to run, scale, secure, or back up.
- **Deterministic memory.** Conflicting facts are resolved by rule (bi-temporal,
  single-valued supersession), not by asking an LLM at write time — so writes are predictable
  and reproducible.
- **No LLM at query time.** Recall makes no generative-LLM call — one embedding lookup
  (fully on-device in local 384-d mode), then hybrid search (pgvector HNSW + BM25, RRF) →
  a local ONNX cross-encoder rerank → quota / timeline / entity arms. Fast, cheap,
  deterministic.
- **Governed by default.** Token auth, namespace ACL, audit log, usage/cost ledger, and an
  encrypted secret vault with ingest redaction — in the open-source build.
- **Vendor-neutral, self-hosted.** Apache-2.0, your Postgres, your data, your LLM keys (never
  stored in plaintext).

memnos is a *governed memory engine*, not an agent runtime. A detailed, version-pinned
comparison with other memory systems lives at **[memnos.net/compare](https://memnos.net/compare.html)**.

---

## Quickstart (local)

**Prerequisite:** PostgreSQL **13+** with the **pgvector ≥ 0.7** extension available. memnos does **not**
install Postgres — it connects to yours. (For local dev: `docker compose -f
docker-compose.dev.yml up -d`.)

Install the `memnos` command into its **own isolated environment** (recommended — `uv` is
fastest; `pipx` also works). **Don't `pip install` into your system Python** — a polluted or
half-upgraded system interpreter will fail to load native deps like `psycopg`.

```bash
uv tool install memnos        # recommended  (no uv? `brew install uv`  or
                              #  curl -LsSf https://astral.sh/uv/install.sh | sh)
# or:  pipx install memnos
# or run ./install.sh (macOS/Linux) / .\install.ps1 (Windows) — picks uv→pipx for you

memnos setup                  # enter your Postgres connection → creates schema + admin token
memnos start                  # start the server (background) → open http://127.0.0.1:8900/admin
```

Operate it like any daemon: `memnos status` / `stop` / `restart` (`memnos serve` runs it in
the **foreground** for systemd/launchd/Docker), and `memnos upgrade` updates in place.
**`memnos autostart`** installs a login service (launchd/systemd) so the server survives
reboots and waits for Postgres if it isn't up yet — setup offers it automatically.

> **Alternative (needs Docker):** `memnos setup --docker` spins up a pgvector Postgres for you
> — no Postgres install or pgvector version-matching. Then `memnos start`.

> Inside your own virtualenv, plain `pip install memnos` is fine too —
> `python -m venv .venv && .venv/bin/pip install memnos`.

`memnos --help` covers everything: `setup start stop restart status token grant principal
namespace secret stats health whoami ns remember recall migrate-embeddings upgrade`. Config
(DSN, vault key, port) lives in `~/.memnos/config.json`. Full walkthrough:
[`QUICKSTART.md`](QUICKSTART.md) — on Windows, see [`docs/guides/windows.md`](docs/guides/windows.md).

`memnos setup` asks for an **optional OpenAI key** (validated live, stored AES-256-GCM
encrypted in the vault): with one, you get 1536-d embeddings + bi-temporal fact extraction;
without one, memnos runs in free **local 384-d** mode (embeddings only, no extraction —
nothing leaves your machine). Changed your mind later? `memnos migrate-embeddings` re-embeds
every stored memory between the two, losslessly. memnos **never holds your LLM key in
plaintext** — it lives in the encrypted vault (or your `.env`).

---

## Integrations

One command wires memnos into your agent — no manual config editing:

```bash
memnos agent-setup claude-code     # Claude Code: MCP + hooks (auto recall/save) + /memnos + CLAUDE.md
memnos agent-setup claude-desktop  # Claude Desktop
memnos agent-setup codex           # Codex CLI (MCP via ~/.codex/config.toml + AGENTS.md)
memnos agent-setup cursor          # Cursor
memnos agent-setup windsurf        # Windsurf
memnos agent-setup openclaw        # OpenClaw (personal AI assistant gateway)
memnos agent-setup hermes          # Hermes Agent (Nous Research)
```

Each mints a scoped token, is idempotent, and backs up edited files; `memnos setup` wires
Claude Code automatically when it detects it (`memnos claude-setup` still works as an
alias). **Claude Code** is the only agent with lifecycle **hooks** (auto-recall before each
prompt, auto-save after — both the user's message and the assistant's reply); every other
agent gets the memnos MCP **tools** (`recall`, `recall_wide`, `remember`,
`reconcile_claim`, …).

**Guaranteed capture for everything else — `memnos proxy`:** MCP tools are called at the
model's discretion, so for deterministic capture point any OpenAI- or Anthropic-compatible
client's base URL at the proxy (`ANTHROPIC_BASE_URL=http://127.0.0.1:8910`) — it relays
every request untouched (streaming included, keys forwarded, never stored) and captures
**both sides** of each completed exchange into governed memory, with agent-loop noise
filtered out. Works with Claude Code, Hermes, OpenClaw, Open WebUI, and any SDK app.
[Full guide + capability matrix](docs/guides/proxy.md).

- **REST** — `POST /remember`, `POST /recall` (Bearer token, namespace-scoped).
- **CLI / SDK** — `memnos remember/recall`, or `uv pip install memnos-sdk` (LangChain / LangGraph
  / LlamaIndex adapters).
- Full client guides: [`docs/guides/clients/`](docs/guides/clients/README.md).

REST, MCP, hooks and the benchmark all run the **same engine** (`MemnosMemory`) — there is
one codebase, not a benchmarked copy and a shipped copy.

---

## Management console + governance

A zero-build web console ships in the open-source build at **`/admin`** (create namespaces,
mint/revoke tokens, manage grants, view the dashboard, store secrets). Every call is
token-authenticated, namespace-ACL'd, and audited. (SSO/OIDC, advanced RBAC, multi-tenant
control plane, and the richer enterprise UI are the commercial layer.)

```bash
memnos admin          # bootstrap an admin token → paste into /admin
```

---

## Benchmarks — LoCoMo (and how we report it)

**57–61% under the gpt-4o judge / 58% under an independent cross-provider judge** on the full
LoCoMo benchmark (10 conversations, 1,542 QA). The gpt-4o-judge band is **reproduced from
scratch** — `benchmarks/locomo_eval.py` on a fresh clone + DB scored 57%, 58% and 61% across
independent ingests (the spread is non-deterministic extraction, not the engine), with every
prediction published under [`benchmarks/results/`](benchmarks/results/).

We care more about *credibility* than a big headline:

- **Setup:** full 10 conversations. Ingest → bi-temporal SPO fact extraction (gpt-4o-mini) +
  consolidation; retrieve via hybrid (pgvector + BM25, RRF) + cross-encoder rerank +
  timeline / entity-guarantee arms — **no LLM at query time**; answer with the calling agent
  (GPT-5-mini in our run); judge with an LLM.
- **Independent judging:** most published numbers are *self-judged* (the same vendor's model
  grades its own answers). We additionally score under an **independent provider's judge**
  (Claude grading GPT answers) to remove self-preference bias.
- **Judge transparency:** the score is judge-sensitive. On the *same answers* we measure a
  **strict ~44% / standard 57-61% / lenient 85-88%** band — so you can see how much the
  judge prompt moves any number.
- **On comparisons:** headlines elsewhere (~66% Mem0, ~73% Mnemory, 90%+ others) are
  typically self-judged and sometimes on a *different* benchmark (e.g. DMR, not LoCoMo). We
  don't claim parity — we publish a reproducible harness.

**Reproduce:** `python benchmarks/locomo_eval.py --sample-ids 0,1,2,3,4,5,6,7,8,9`
(see [`benchmarks/`](benchmarks/README.md)).

*We'd rather report a credible 58% under an independent judge than an inflated 85% under a
lenient one.*

---

## How it works

**Write** (LLM at ingest only): a message becomes a verbatim raw turn **and** structured
bi-temporal SPO facts. Single-valued attributes (`lives_in`, `works_at`) supersede on
change; multi-valued ones (`did`, `visited`) accumulate. Secrets are redacted before
storage. An offline "sleep" pass consolidates facts into entity dossiers (multi-hop
pre-join).

**Read** (no LLM): the query runs hybrid retrieval (pgvector HNSW + BM25 via tsvector, fused
with RRF), a cross-encoder reranks, and quota retrieval guarantees raw-turn + fact coverage.
Temporal questions add a guaranteed entity **timeline**; entity questions add an
**entity-guarantee** arm so list/aggregation answers are complete.

---

## Security & operations

- **Auth:** opaque bearer tokens (SHA-256 hashed at rest; instantly revocable — not JWTs).
- **ACL:** every read/write is clamped to the principal's namespace grants.
- **Audit + usage ledger:** who/what/when + per-op LLM cost.
- **Secret vault:** AES-256-GCM, value-refs (`secret://name`), key rotation.
- **Redaction:** secret-shaped text is stripped from remembered messages before storage.
- **Health heuristic:** `memnos health` turns metrics into actionable findings.

> Local-first: the server binds `127.0.0.1`. Put a TLS reverse proxy in front for remote use.

---

## License

Apache-2.0. The open-source build is the engine + single-org self-host + the basic
management console. SSO/advanced RBAC, encrypted-vault key management (KMS/HSM, rotation
policies), the multi-tenant control plane, the richer enterprise UI, and managed cloud are
the commercial layer.
