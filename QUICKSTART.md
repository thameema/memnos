# memnos Quickstart

Get a governed, self-hosted memory server running and talking to your AI tools in about
five minutes — one package, one command.

> **Engine:** PostgreSQL + pgvector. No second database, no graph store. An LLM is used
> only at **write** time (fact extraction + embeddings); recall is pure SQL + a local
> cross-encoder reranker — **no LLM at query time**.

---

## Prerequisites

- **PostgreSQL 13+** with the **pgvector ≥ 0.7** extension available. memnos **does not install
  Postgres** — it connects to yours and creates its own schema. (For local dev, the
  `pgvector/pgvector` Docker image is easiest: `docker compose -f docker-compose.dev.yml up -d`.)
- **Python 3.10+** (for `uv` / `pip`).
- *Optional:* an **OpenAI API key** for 1536-d embeddings + fact extraction. Without one,
  memnos runs in free **local 384-d** mode (embeddings only, no extraction).

> No OpenAI key required to try it. The local mode runs entirely on CPU.

---

## 1. Install the `memnos` package

One package ships the server **and** the client/admin CLI, on macOS, Linux and Windows.
Install it into its **own isolated environment** — `uv` (fastest) or `pipx`. **Don't
`pip install` into your system Python**; a polluted/half-upgraded system interpreter will
fail to load native deps like `psycopg`.

```bash
uv tool install memnos        # recommended  (no uv? `brew install uv`  or
                              #  curl -LsSf https://astral.sh/uv/install.sh | sh)
# or:  pipx install memnos
# from a source checkout:  ./install.sh   (macOS/Linux) | .\install.ps1 (Windows)
```

Verify:

```bash
memnos --help
```

> Inside your own virtualenv, plain `pip` is fine too:
> `python -m venv .venv && .venv/bin/pip install memnos`.
> If a fresh shell can't find `memnos`, run `hash -r` and open a new terminal (the
> installer already added its bin dir to your PATH).

---

## 2. Point memnos at your Postgres

```bash
memnos setup
```

The wizard asks for your Postgres connection (or pass `--dsn postgresql://user:pass@host:5432/db`).
It then enables `pgvector`, creates the memnos schema + the governance control plane,
generates your encrypted-vault key, mints a one-time **admin token** (copy it — shown once),
and writes everything to `~/.memnos/config.json`. If pgvector is missing or built for the
wrong PG version, it tells you exactly how to fix it. If it detects Claude Code it offers to
wire it up (`memnos claude-setup`).

The wizard also asks for an **optional OpenAI key** (hidden input, validated live against
the OpenAI API, stored encrypted in the vault) — that's what chooses between 1536-d OpenAI
mode and free local 384-d mode. Choose carefully, but it's not one-way:
`memnos migrate-embeddings` later re-embeds every memory between the two dimensions,
losslessly (it re-embeds from the stored text). Re-running `memnos setup` is safe — the
schema is additive and never wipes data.

> **Alternative (needs Docker):** `memnos setup --docker` runs a pre-configured pgvector
> Postgres for you — no Postgres install or version-matching. Then continue to step 3.

---

## 3. Start the server

```bash
memnos start                          # background server on http://127.0.0.1:8900
memnos status                         # version · config · embedding mode · server state
curl -s localhost:8900/healthz        # -> {"ok": true}
```

Manage it like any daemon: `memnos stop` / `restart` / `status`. The **first** start
downloads the local embedding/reranker models (~1 GB) — `memnos start` shows the progress.
(`memnos serve` runs the server in the *foreground* instead — for systemd, launchd, Docker,
or debugging.)

**Recommended:** `memnos autostart` installs a login service (launchd on macOS, systemd
`--user` on Linux) so the server starts at every login, restarts if it dies, and **waits for
Postgres** if it isn't up yet — your agents always have memory without you thinking about
it. Setup offers this automatically. Logs go to `~/.memnos/server.log` (auto-rotated at
10 MB); remove with `memnos autostart --remove`.

Open the management console at **http://127.0.0.1:8900/admin** and paste your admin token to
create namespaces, mint/revoke tokens, manage grants, store secrets, and watch the dashboard.

---

## 4. Create a namespace + a scoped token

Namespaces are **explicit** — you create them; memnos never auto-creates them. Use one per
user, team, project or agent. From the console, or the CLI:

```bash
memnos namespace add user:alice:notes
memnos principal alice
memnos token alice --label "alice laptop"     # prints a token ONCE — copy it
memnos grant alice user:alice:notes
```

---

## 5. Remember & recall

```bash
TOK=mnk_...                       # the token from step 4
NS=user:alice:notes

memnos remember "On 2026-06-07 Alice moved to Seattle and joined Acme as a staff engineer." \
  --namespace "$NS" --token "$TOK"

memnos recall "Where does Alice work and live?" \
  --namespace "$NS" --token "$TOK"
```

Recall returns ranked memories **and** a ready-to-paste context block — with no LLM in the
loop. (Equivalent REST: `POST /remember`, `POST /recall` with a `Bearer` token.)

---

## 6. Secrets (encrypted vault + auto-redaction)

memnos auto-redacts secret-looking text (API keys, tokens, passwords) from remembered
messages **before** storage, so credentials never leak into recall. For secrets you *do*
want to keep:

```bash
memnos secret set openai          # prompts for the value (not echoed)
memnos secret ls
```

Reference a stored secret anywhere as `secret://openai` (e.g. `OPENAI_API_KEY=secret://openai`
in `.env` — the server resolves it at startup). Values are AES-256-GCM encrypted at rest,
never logged or returned. Rotate the key with `memnos secret rotate`.

---

## 7. Connect your AI tools (one command each)

memnos wires itself into your agent — no manual config editing:

```bash
memnos claude-setup            # Claude Code: MCP + hooks (auto recall/save) + /memnos + CLAUDE.md
memnos agent-setup codex       # Codex CLI
memnos agent-setup cursor      # Cursor
memnos agent-setup windsurf    # Windsurf
memnos agent-setup claude-desktop
memnos agent-setup openclaw    # OpenClaw (assistant gateway — ~/.openclaw/openclaw.json)
memnos agent-setup hermes      # Hermes Agent (Nous Research — ~/.hermes/config.yaml)
```

Each mints a scoped token, is idempotent, and backs up files it edits. **Restart the agent
afterward.** Claude Code is the only agent with lifecycle **hooks** (auto-recall before each
prompt, auto-save after); the rest get the memnos MCP **tools** (`recall`, `recall_wide`,
`remember`, `reconcile_claim`, …). Full client guides:
[`docs/guides/clients/`](docs/guides/clients/README.md).

---

## 8. Operate it

```bash
memnos status     # version · config · embedding mode · server up?
memnos stats      # volume · error% · p50–p95 latency · empty-recall rate
memnos health     # actionable CRITICAL / WARN findings
memnos usage      # cost per op (extraction tokens tracked)
memnos whoami <token>   # what a token can see
memnos upgrade    # check PyPI for a newer version and update in place
memnos migrate-embeddings --to 1536   # switch local 384-d ↔ OpenAI 1536-d (re-embeds all)
```

---

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/healthz` | — | liveness |
| GET  | `/readyz`  | — | DB reachable |
| POST | `/remember`| Bearer (write) | store a message → raw turn + extracted bi-temporal facts |
| POST | `/recall`  | Bearer (read)  | ranked memories + context block (no LLM) |
| POST | `/consolidate` | Bearer (write) | distill facts into entity dossiers |
| POST | `/feedback`| Bearer | was the recall helpful? (quality signal) |

---

## Next

- **Claude Code:** [`docs/integrations/claude-code.md`](docs/integrations/claude-code.md)
- **Any MCP client (Cursor, Windsurf):** [`docs/integrations/mcp.md`](docs/integrations/mcp.md)
- **Accuracy config + LoCoMo numbers:** [`benchmarks/`](benchmarks/README.md)

## Troubleshooting

**`memnos setup` can't enable pgvector** — your Postgres user needs rights to
`CREATE EXTENSION`, and the `pgvector` extension must be installed on the server. Use the
`pgvector/pgvector` image for local dev.

**`/admin` rejects my token** — the console requires a token with the `*` grant (the one
`memnos setup` printed). Mint another with `memnos admin` if you lost it.

**Recall returns nothing** — check the namespace; a token only sees namespaces it was
granted. `memnos whoami <token>` shows the grants.
