# MEMNOS 2.0: Groundbreaking Developer Architecture & GitHub Traction Blueprint

**Authors:** Gemini (`@gemini-critic`) & Claude (`@tommy-memnos`) — Joint Architectural Review (`thread: memnos-2.0-arch-traction`, `Inbox #262–#264`, `#arch-review #661–#663`)  
**Status:** Converged Blueprint (`v0.1.56` baseline)

---

## 1. Honest Diagnosis: Why MEMNOS Has ~5 Stars Today (Despite Deep Engineering)

After auditing the codebase ([`core/service.py`](../core/service.py), [`core/store.py`](../core/store.py), [`core/schema.sql`](../core/schema.sql), [`memnos_cli.py`](../memnos_cli.py), [`README.md`](../README.md)) and debating between Gemini and Claude, our joint verdict is clear: **MEMNOS has a distribution and first-60-second activation problem, not a core-engine quality problem.**

1. **The "Empty Database" Cold-Start Trap (Zero Payoff in Minute 1):**
   A developer runs `uv tool install memnos && memnos setup && memnos start`. That part is great (embedded PostgreSQL + pgvector, zero Docker required). **Then they run their first prompt or `memnos recall` — and the database is 100% empty.** Value only appears after days of chatting. In open-source dev tools, if a developer doesn't experience an "aha!" moment within 60 seconds of installing, they close the terminal and never star or return.
2. **The "Invisible Plumbing" Problem:**
   When `memnos hook recall` and `memnos hook remember` work well, they are completely silent. Developers cannot see what memories were injected, why they ranked at the top, which stale turns were demoted, or which `PreToolUse` constraints protected their repo. Because they don't *feel* MEMNOS working every hour, they don't talk about it.
3. **README Above-the-Fold Overload:**
   The 359-line [`README.md`](../README.md) opens with 8 badges, a paragraph of architecture, and **three large GIFs before the user even reaches the Quickstart install command on line 74**. Drive-by GitHub visitors decide in 10 seconds: *"What pain does this kill for me right now, and how do I try it in one command?"*
4. **Generic Category Framing vs. Sharp Developer Wedge:**
   The AI memory space is crowded (`Mem0`, `Zep`, `Graphiti`, `Letta`, `Supermemory`, `ai-memory`). Positioning as *"a self-hosted memory server for AI agents"* sounds like every other vector store. Meanwhile, the features that make MEMNOS genuinely groundbreaking for software engineers — **PreToolUse hard constraint enforcement, cross-vendor agent continuity (Claude Code ↔ Gemini/Antigravity ↔ Codex live pub/sub + shared memory), server-stamped team attribution, and bi-temporal contradiction close-out on one Postgres** — are buried or split across docs.
5. **Local `$0` Mode Drops the Best Feature (Bi-Temporal Facts):**
   In [`README.md`](../README.md#L100-L104), running without an OpenAI key (`local-384` mode) stores raw verbatim turns only — **no fact extraction, no supersession, no timelines** unless the user manually configures `MEMNOS_EXTRACT_BASE_URL`. A developer testing MEMNOS for free on their laptop never experiences bi-temporal supersession unless they wire up an LLM endpoint.

---

## 2. How Gemini & Claude Converged (Where We Agreed & Where We Pushed Back)

| Proposal Pillar | Gemini's Initial Proposal | Claude's (`@tommy-memnos`) Pushback & Field Reality | Final Converged MEMNOS 2.0 Design |
| :--- | :--- | :--- | :--- |
| **1. Code & Git-Anchored Bi-Temporality** | Anchor facts to Git commits, file paths, and AST symbol hashes; auto-invalidate on commit. | **Agree on concept, cut AST scope.** Tree-sitter per language is a maintenance trap, and auto-superseding on any file edit will cause ~90% false positives (like issue `#156`). Use soft `suspect` flags only. | **File + Blob SHA + Identifier-Diff Anchors:** Anchor facts to `(file_path, blob_sha, identifier_tokens)`. Only flag `(code drifted: <file> changed)` at recall when `git diff old_blob..HEAD` modifies lines containing the fact's code identifiers. Never auto-supersede. |
| **2. Zero-LLM Tier-1 Write Extractor** | Run local GLiNER/ONNX span extractor + regex rules + git diff parser for `<15ms` `$0` SPO supersession. | **Reject GLiNER & regex supersession; keep deterministic slice.** We measured GLiNER at `2–35s/doc` on CPU with zero retrieval gain, and regexes fail on subtle state reversals (`#154`/`#156`). | **Deterministic Git/CI/PR Extractor (`$0`, `<5ms`):** In `local-384` mode, deterministically extract structured facts from git commits, conventional trailers (`Decided:`, `Constraint:`, `Fixes #N`), PR titles, and pytest/CI pass/fail summaries. Leave prose SPO extraction to LLM/local-LLM. |
| **3. Untrusted Recall Envelopes & Governance** | Wrap peer recalls in `<memnos:memory trust="peer">` envelopes + cryptographic commit signing + quorum promotion. | **Keep envelopes; drop crypto & quorum bloat.** Envelopes solve prompt-injection hygiene cleanly (just escape XML carefully, per `#233`). Single-org teams don't need quorum voting. | **Hardened `<memnos:peer_memory>` Data Envelopes + PreToolUse Constraint Guardrails:** Frame non-self recalls as untrusted data envelopes with server-stamped `author` and `namespace`, and elevate **PreToolUse Constraint Blocking** to a first-class headline feature. |
| **4. Developer Visibility & Telemetry** | Show *"MEMNOS saved 14,200 tokens & 4 file searches"* on every session start. | **Reject "tokens saved" claims.** Without a counterfactual, "tokens saved" is a fabricated number that Hacker News will roast. Ship strict, verifiable counts + an **Injection Audit View**. | **`memnos explain` (Injection Audit CLI/UI) + Honest Session Counters:** Show exact facts injected, cross-encoder scores, why stale turns were demoted, constraints enforced, and contradictions closed. |
| **5. Procedural Bug-to-Skill Crystallization** | Auto-detect 20-turn debugging arcs ending in green tests and LLM-crystallize a `skill` memory. | **Defer autonomous crystallization.** A bad auto-crystallized skill becomes a permanent poisoned instruction. Ship a structured `fix` memory type + red-to-green hook prompt instead. | **Structured `memory_type='fix'` (`Symptom → Root Cause → Fix Commit → Verify Cmd`):** Triggered explicitly via `memnos fix` or suggested when the `Stop` hook sees a test suite flip from fail to pass. |
| **6. `memnos init` Cold-Start Repo Hydration** | Ingest 90 days of `git log`, `CLAUDE.md`, `.cursorrules`, `README.md`, and PRs in 5 seconds on first run. | **Agree — make this Priority #1.** Must obey `#29` (store `CLAUDE.md` rules verbatim as pinned constraints, never LLM-paraphrased) and `#10/#11` (tag git-log facts `source=git` with lower base weight so they don't drown live turns). | **`memnos init` 5-Second Repo Hydration:** Automatically populates pinned constraints from `CLAUDE.md`/`.cursorrules` verbatim and hydrates 90 days of git decisions/commits (`source=git`) so the very first recall after setup is magical. |

---

## 3. MEMNOS 2.0 Groundbreaking Architecture

**Theme:** *"Memory that is anchored to your code, enforced at tool-call time, shared live across every AI harness, and never stalls your agent."*

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                     DEVELOPER HARNESSES (LIVE MULTI-AGENT MESH)                        │
│   Claude Code (Hooks+MCP)  │  Antigravity / Gemini  │  Cursor / Windsurf  │  Codex CLI │
└─────────────┬──────────────────────────┬───────────────────────┬───────────────────────┘
              │                          │                       │
              ▼                          ▼                       ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  [1] DETERMINISTIC CAPTURE & ENFORCEMENT LAYER (Zero Model Discretion)                 │
│   • PreToolUse Constraint Gate: blocks banned commands/files BEFORE execution (#83/84) │
│   • UserPromptSubmit Recall Hook: <250ms hard SLO, XML-escaped <memnos:peer_memory>    │
│   • Stop / Post-Commit Hook: speaker-aware capture (#154) + Git SHA drift detector     │
└─────────────┬──────────────────────────────────────────────────┬───────────────────────┘
              │                                                  │
              ▼ (Write Path)                                     ▼ (Read Path — 0 LLM)
┌─────────────────────────────────────────┐    ┌─────────────────────────────────────────┐
│  [2] CODE-AWARE BI-TEMPORAL WRITE PATH  │    │  [3] SUB-250ms GUARANTEED RECALL ENGINE │
│   • `memnos init`: 5s repo hydration    │    │   • Parallel SQL Arms (pgvector HNSW +  │
│     (verbatim CLAUDE.md pins + 90d git) │    │     BM25 RRF + Timeline + Entity Dump)  │
│   • Tier-1 $0 Git/CI/Trailer Extractor  │    │   • Lazy Git-Blob Drift Check: annotates│
│   • Code Anchor Binding: attaches       │    │     `(code changed in <file>)` if diff  │
│     (file_path, blob_sha, identifiers)  │    │     touched the fact's identifiers      │
│   • Advisory-Locked SPO Supersession +  │    │   • Local ONNX Cross-Encoder Rerank +   │
│     Reversal Close-Out (#60, #156)      │    │     Degrade-to-Lexical Circuit Breaker  │
└─────────────┬───────────────────────────┘    └─────────────────┬───────────────────────┘
              │                                                  │
              └────────────────────────┬─────────────────────────┘
                                       ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  [4] SINGLE ENGINE: POSTGRESQL 16 + PGVECTOR (Embedded Zero-Dep Default or External)   │
│   • Bi-Temporal `semantic` + `raw_turns` + `episodic` + `entities/edges` + `provenance`│
│   • Control Plane: Scoped Tokens, Namespace ACLs, Session Pub/Sub + Webhook Push, Vault│
│   • Observability: `memnos explain` Injection Audit Ledger + Health Heuristics         │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Pillar 1: `memnos init` — 5-Second Cold-Start Repo Hydration (Priority #1)
- **The Problem it Solves:** Eliminates the "empty database after setup" dead zone.
- **How it Works:**
  1. **Verbatim Rule Pinning (Issue `#29` compliance):** Scans `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, and `.github/copilot-instructions.md` for imperative rules/guardrails and inserts them **verbatim** as `memory_type='constraint'` with deterministic `constraint_subject` slugs (`derive_constraint_subject` in [`core/service.py`](../core/service.py#L225-L250)). Zero LLM paraphrasing.
  2. **90-Day Git History & PR Hydration:** Extracts conventional commits (`feat:`, `fix:`, `perf:`, `refactor:`, `BREAKING CHANGE:`), commit trailers, and recent merged PRs (if `gh` is authenticated) into `semantic` (`source_speaker='git'`, `memory_type='decision'` or `'fix'`).
  3. **Rank-Time Calibration (Issues `#10`/`#11` compliance):** Rows with `source_speaker='git'` carry a conservative base weight (`0.85x` vs live developer turns) so hydrated git history provides instant day-1 context without drowning out fresh conversational decisions.
  4. **Immediate Payoff:** At the end of `memnos init`, the CLI automatically runs a sample recall against the repo and prints:
     ```text
     ✓ Hydrated proj:memnos in 3.8s (14 pinned constraints from CLAUDE.md, 182 commit/PR facts).
     ✓ Try it now: `memnos recall "how does supersession work?"` or `memnos explain`
     ```

### Pillar 2: Code- & Git-Diff-Anchored Bi-Temporal Facts (Code-Aware Staleness)
- **The Problem it Solves:** Conversational memory tools remember what was true about your code *two weeks ago* even after the code was refactored yesterday.
- **Why Our Design Avoids the False-Positive Trap:**
  - Rather than building brittle per-language AST parsers or flagging a fact stale whenever *any* line in a 2,000-line file is edited, we add lightweight JSONB column `code_anchors` on `semantic`:
    ```json
    [{"file": "core/service.py", "blob_sha": "a91f3c2", "identifiers": ["_is_historical", "_PAST_COPULA_RE"]}]
    ```
  - **Write Time (Deterministic, 0 LLM):** When a turn or fact mentions a repo-relative file path (verified via `git ls-files` cache) alongside code identifiers (`snake_case`, `camelCase`, `UPPER_ENV_VARS`), MEMNOS records the current `git rev-parse HEAD:<file>` `blob_sha` and the matched identifiers.
  - **Recall Time (Lazy Check + Cached Diff):** For top-ranked facts that carry `code_anchors`, MEMNOS checks if `HEAD:<file>` still matches `blob_sha` (in-memory dict cached per `HEAD` SHA, `<1ms`). If the blob changed, it checks whether `git diff -U0 <old_blob> <new_blob>` touches any of the anchored `identifiers` (or if the file was deleted).
  - **Soft Annotation, Never Auto-Supersede:** If matched, `render_context` appends `(⚠ code changed: core/service.py modified _is_historical since this fact was recorded)` and nudges its rank below verified-fresh facts. The agent is warned to read the file before trusting the old fact.

### Pillar 3: `memnos explain` — The Injection Audit & Observability Inspector
- **The Problem it Solves:** Makes invisible plumbing visible, debuggable, and trustworthy on every turn.
- **How it Works:**
  - Every `memnos hook recall` and `/recall` call writes a lightweight ring-buffer entry (last 50 recalls per namespace) recording:
    - The query, classified `query_intent` (`verbatim` | `list` | `broad` | `neutral`), and latency breakdown (`embed_ms`, `sql_ms`, `rerank_ms`).
    - Which **Pinned Constraints** were injected.
    - Which **Facts & Turns** made the cut, their cross-encoder scores, their provenance (`by <author>`, `turn_id`), and which candidates were **demoted as superseded** or **collapsed as duplicates (`dup_count`)**.
  - Running `memnos explain` (or opening the `/admin` **Recall Inspector** tab) prints a crystal-clear table of the last prompt's memory injection in `<50ms`:
    ```text
    $ memnos explain
    Last Recall (14s ago) · ns=proj:memnos · intent=broad · 118ms (embed 12ms, sql 31ms, rerank 75ms)
    ├── PINNED CONSTRAINTS (3 injected, 0 retired)
    │   └── [auto:memnos-is-the-primary-memory-8f2a1c] memnos is the PRIMARY memory...
    ├── TOP FACTS INJECTED (8 of 24 candidates, facts-first active)
    │   ├── 0.94  [fact, 2026-11-11, by tommy-memnos] Session webhook push uses HMAC-SHA256...
    │   └── 0.89  [fact, 2026-11-10, by gemini-critic] Partition semantic by hash(namespace_id)...
    └── SUPPRESSED / DEMOTED (4 stale turns demoted, 6 cron duplicates collapsed)
        └── [superseded 2026-11-11] turn #18412: "webhook delivery is pull-only..."
    ```

### Pillar 4: Hard Recall Latency SLO (`p95 < 250ms` Warm) & Degrade-Never-Fail Guarantee
- **The Problem it Solves:** A daily coding tool sitting in `UserPromptSubmit` cannot add 2–5 seconds of lag or fail a prompt when Postgres is cold.
- **How it Works (Building on Issues `#12`, `#41`, `#59`, PR `#179`):**
  1. **Parallel SQL Arms by Default:** `recall_fetch` dispatches `raw_turns` and `semantic` hybrid searches concurrently via `_dispatch_sql_jobs` (`MEMNOS_RECALL_SQL_CONCURRENCY=2`).
  2. **Rerank Input Truncation & Threading:** Truncate cross-encoder candidate passages to ~256 tokens and default ONNX intra-op threads to `min(4, cpu_count)`, cutting CPU rerank latency by ~60%.
  3. **Strict Hook Deadline (`MEMNOS_HOOK_DEADLINE_MS=400`):** If the cross-encoder or vector arm exceeds the deadline on a cold cache, automatically fall back to RRF/BM25 lexical order with `_degraded=True` rather than stalling the developer's prompt.

### Pillar 5: Untrusted Peer Envelopes (`<memnos:peer_memory>`) & PreToolUse Enforcement
- **The Problem it Solves:** In team namespaces (`team:eng`) or multi-agent swarms, recalled text from another user or external corpus must never be able to prompt-inject the receiving agent, while team rules must be *enforced*, not just suggested.
- **How it Works:**
  1. **XML-Escaped Peer Envelopes:** Memories authored by a different principal (`row["author"] != viewer`) or ingested from external files/git are wrapped in an explicit data boundary:
     ```xml
     <memnos:peer_memory author="dev-alice" namespace="team:eng" trust="peer_data_only">
     ...escaped content...
     </memnos:peer_memory>
     ```
  2. **PreToolUse Constraint Gate as a Headline Feature:** Highlight and expand the `PreToolUse` hook so teams can define executable guardrails (`memnos constraint add --block-cmd "git push.*main" "Never push directly to main"`) that deterministically block bad tool calls before they run.

### Pillar 6: Deterministic Tier-1 Git/CI Extractor & Structured `fix` Memories
- **The Problem it Solves:** Makes `$0` `local-384` mode useful without an LLM, and captures repeatable debugging solutions without hallucinated "auto-skills".
- **How it Works:**
  1. **Tier-1 Deterministic Extractor (`core/extract_deterministic.py`):** Zero-LLM, `<5ms` parser that extracts structured facts from commit messages, PR/issue references (`Fixes #156`), version tags (`v0.1.56`), and pytest/build output blocks (`"25 passed in 1.42s"`).
  2. **Structured `memnos fix` Command & Hook Suggestion:**
     - Adds `memory_type='fix'` with structured fields (`symptom / error_signature`, `root_cause`, `fix_commit_or_diff`, `verify_cmd`).
     - When an agent hits an error signature that matches a stored `fix` memory via BM25/vector hybrid search, that `fix` is boosted to the top of recall so no teammate ever debugs the same issue twice.

---

## 4. GitHub Traction & Growth Playbook (From 5 Stars to 1,000+ Stars)

### Phase 1: Fix the GitHub Storefront & First-60-Second Funnel (Days 1–3)
1. **Rewrite the Top 40 Lines of [`README.md`](../README.md):**
   - **Line 1 Headline:** *"Your AI coding agents forget everything between sessions—and never learn what your teammates' agents just figured out. `memnos` gives Claude Code, Cursor, Codex, and Gemini one shared, code-aware memory on your own Postgres."*
   - **One Hero Visual Above the Fold:** A single 20-second split-screen GIF showing **Claude Code** on the left and **Gemini / Antigravity (or a second Claude Code)** on the right sharing live memory and blocking a forbidden command via a pinned constraint. Move the other 3 GIFs below the fold.
   - **The 3-Command 60-Second Quickstart (Right Below the Hero GIF):**
     ```bash
     uv tool install memnos && memnos setup   # embedded Postgres + pgvector, zero Docker
     memnos init                              # hydrates your repo's CLAUDE.md + 90d git history in 4s
     memnos claude-setup                      # wires auto-recall hooks, MCP & PreToolUse guardrails
     ```
2. **Publish the 6 Finished Full-HD Videos Already in `docs/video-scripts/`:**
   - You already have 6 rendered 1080p MP4 videos and 3 YouTube upload packs sitting untracked in [`docs/video-scripts/`](video-scripts/). Upload them to YouTube, link the 90-second flagship video at the top of `README.md` and `memnos.net`, and embed the Claude Code setup video in [`docs/integrations/claude-code.md`](integrations/claude-code.md).
3. **Turn On Community Surface Area on GitHub:**
   - Enable **GitHub Discussions** (Q&A + Show and Tell).
   - Create **8–10 well-scoped `good first issue` / `help wanted` tickets** (e.g., *"Add `.windsurfrules` parser to `memnos init`"*, *"Add Zed editor setup to `memnos agent-setup`"*, *"Add JetBrains AI proxy guide"*). Contributors star and fork repos they can easily contribute to (currently `0 forks`).

### Phase 2: Ecosystem & Marketplace Distribution (Days 4–7)
Get listed where developers are already searching for MCP servers and coding-agent plugins:
1. **Submit PRs to the High-Traffic Curated Lists (Free, High-Intent Traffic):**
   - `punkpeye/awesome-mcp-servers` (40k+ stars)
   - `modelcontextprotocol/servers` (official MCP community servers list)
   - `hesreallyhim/awesome-claude-code` & `anthropics/claude-code` community showcases
   - `e2b-dev/awesome-ai-agents` & `kyrolabs/awesome-langchain`
   - Glama.ai (`glama.ai/mcp/servers`) & Smithery.ai (`smithery.ai`) MCP directories
2. **One-Command Claude Code Plugin Experience:**
   - Simplify the Claude Code plugin flow so `memnos claude-setup` automatically configures the token and removes any duplicate hooks in one step.

### Phase 3: High-Signal Technical Launches (Weeks 2–3)
Avoid generic promotional posts. Launch with **opinionated engineering artifacts and live multi-agent demos**:

1. **Launch Post #1 — Hacker News (`Show HN`):**
   - **Title:** *"Show HN: Memnos – Shared, bi-temporal Postgres memory for Claude Code, Cursor, and Gemini (no LLM at query time)"*
   - **The Lead Comment (Written by You):** Be radically honest, technical, and humble—the exact tone HN rewards:
     - Why we built on **one plain PostgreSQL + pgvector** instead of a graph DB + vector store.
     - Why **LLM calls at query time** are an anti-pattern for coding-agent hooks (`p95 < 250ms` local ONNX cross-encoder rerank + RRF).
     - Why `78%` of naive agent memory is junk assistant self-narration (Issue `#154`) and why `92%` of naive contradiction detection is false positives on passive voice (`"PR #2024 was merged"`, Issue `#156`).
     - How **judge prompts swing LoCoMo scores from 44% to 88%** on the exact same answers (link the reproducible judge ladder in [`benchmarks/`](../benchmarks/README.md)).
2. **Launch Post #2 — `r/ClaudeAI` & `r/Cursor`:**
   - **Angle:** *"I got tired of Claude Code forgetting yesterday's debugging session and ignoring constraints in long sessions, so I built an open-source Postgres memory + PreToolUse constraint blocker (works 100% locally with embedded Postgres)."*
   - Attach the 60-second terminal video showing `memnos init` → `memnos explain` → two agents sharing live context via pub/sub + memory.
3. **Launch Post #3 — `r/LocalLLaMA` & `r/SelfHosted`:**
   - **Angle:** *"Zero-cloud, $0 persistent memory for local coding agents: Embedded pgvector + local 384-d embeddings + local ONNX cross-encoder reranker + Ollama extraction."*
4. **The Viral Technical Blog Post (Publish on `memnos.net/blog` + Dev.to + Substack):**
   - **Title:** *"We Benchmarked AI Agent Memory on Real Coding Sessions: Why 78% of Extracted Facts Are Noise (and How Bi-Temporal Postgres Fixes It)"*
   - Share real production findings from building MEMNOS (`#10`, `#11`, `#29`, `#153`, `#154`, `#156`). Engineers share postmortems with real numbers; every share funnels back to the GitHub repo.

---

## 5. Prioritized Implementation Roadmap

| Sprint / Wave | Deliverable | User-Visible Outcome | Effort |
| :--- | :--- | :--- | :--- |
| **Wave 1 (Immediate)** | **`README.md` Hero Overhaul + Video Publish + `memnos init` (Pillar 1)** | Fresh install hydrates `CLAUDE.md` constraints verbatim + 90d git history in `<5s`; first recall after `memnos setup` returns real project knowledge immediately. | 2–3 days |
| **Wave 2** | **`memnos explain` Injection Audit CLI & UI Tab (Pillar 3)** | Developers can run `memnos explain` after any turn to see exact injected facts, cross-encoder scores, enforced constraints, and demoted stale turns. | 2 days |
| **Wave 3** | **Recall Latency SLO (`<250ms` p95) + Degrade-to-BM25 (Pillar 4)** | Parallel SQL arms (`#12`), 256-token rerank truncation, multi-thread ONNX, and strict hook deadline so hooks never stall a prompt. | 1–2 days |
| **Wave 4** | **Git-Diff Fact Anchors (`file_path` + `blob_sha` + `identifiers`) (Pillar 2)** | Facts referencing repo files/identifiers are automatically annotated `(⚠ code changed in <file>)` at recall time when a later commit edits those identifiers. | 2–3 days |
| **Wave 5** | **Untrusted `<memnos:peer_memory>` Envelopes + `memnos fix` (Pillars 5 & 6)** | Cross-user/external recalls are isolated in XML data envelopes; `memnos fix` captures structured `Symptom → Cause → Fix → Verify` memories. | 2 days |
