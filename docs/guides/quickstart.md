# memnos Quickstart

Get a governed, self-hosted memory server running and talking to your AI tools in about
five minutes — one package, one command.

> **Engine:** PostgreSQL + pgvector. No second database, no graph store. An LLM is used
> only at **write** time (fact extraction + embeddings); recall is pure SQL + a local
> cross-encoder reranker — **no LLM at query time**.

---

## Prerequisites

- **PostgreSQL** with the `pgvector` extension available. memnos **does not install
  Postgres** — it connects to yours and creates its own schema. (For local dev, any
  Postgres works; the `pgvector/pgvector` Docker image is the easiest.)
- **Python 3.10+** (for `pipx` / `pip`).
- *Optional:* an **OpenAI API key** for 1536-d embeddings + fact extraction. Without one,
  memnos runs in free **local 384-d** mode (embeddings only, no extraction).

> No OpenAI key required to try it. The local mode runs entirely on CPU.

---

## 1. Install the `memnos` package

One package ships the server **and** the client/admin CLI, on macOS, Linux and Windows.

```bash
# macOS / Linux
./install.sh           # installs the `memnos` command (via pipx)

# Windows (PowerShell)
.\install.ps1
```

Prefer to do it by hand? `pipx install memnos` (or `pip install memnos`) is equivalent.
Verify:

```bash
memnos --help
```

---

## 2. Point memnos at your Postgres

```bash
memnos setup
```

The wizard asks for your Postgres connection (host / port / database / user / password, or
pass `--dsn postgresql://user:pass@host:5432/db`). It then:

- enables `pgvector` (`CREATE EXTENSION IF NOT EXISTS vector`),
- creates the memnos schema + the governance control plane,
- generates your encrypted-vault key,
- mints a one-time **admin token** (copy it — it is shown once),
- writes everything to `~/.memnos/config.json` (DSN, port, vault key).

---

## 3. Start the server

```bash
memnos serve            # binds http://127.0.0.1:8900
curl -s localhost:8900/healthz      # -> {"ok": true}
```

Open the management console at **http://127.0.0.1:8900/admin** and paste your admin token.
From the console you can create namespaces, mint/revoke tokens, manage grants, store
secrets, and watch the dashboard.

---

## 4. Create a namespace + a scoped token

Namespaces are **explicit** — you create them; memnos never auto-creates them. Use one per
user, team, project or agent.

From the console, or from the CLI:

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

Reference a stored secret anywhere as `secret://openai` (e.g.
`OPENAI_API_KEY=secret://openai` in `.env` — the server resolves it at startup). Values are
AES-256-GCM encrypted at rest, never logged or returned. Rotate the key with
`memnos secret rotate`.

---

## 7. Connect your AI tools

memnos is **MCP-native** and works with any MCP client, plus hooks for Claude Code:

- **Claude Code** → [claude-code-setup.md](claude-code-setup.md) (MCP tools *and* automatic hooks)
- **Cursor** → [clients/cursor.md](clients/cursor.md)
- **Windsurf** → [clients/windsurf.md](clients/windsurf.md)
- **Any MCP client / REST / SDK** → the `memnos mcp` stdio adapter, or the HTTP API directly

The single command for every MCP client is:

```bash
memnos mcp        # stdio MCP server; reads MEMNOS_TOKEN / MEMNOS_NS from the client env
```

---

## 8. Operate it

```bash
memnos stats      # volume · error% · p50–p95 latency · empty-recall rate
memnos health     # actionable CRITICAL / WARN findings
memnos whoami <token>   # what a token can see
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

## Troubleshooting

**`memnos setup` can't enable pgvector** — your Postgres user needs rights to
`CREATE EXTENSION`, and the `pgvector` extension must be installed on the server. Ask your
DBA, or use the `pgvector/pgvector` image for local dev.

**`/admin` rejects my token** — the admin console requires a token with the `*` grant
(the one `memnos setup` printed). Mint another with `memnos admin` if you lost it.

**Recall returns nothing** — check the namespace; a token only sees namespaces it was
granted. `memnos whoami <token>` shows the grants.
