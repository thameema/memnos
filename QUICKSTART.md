# memnos — Local Quickstart

Run the memnos memory server on your machine and talk to it from Claude Code (or any
MCP client, or plain HTTP) in ~5 minutes.

> **Engine:** PostgreSQL + pgvector, one container. **No second database.** LLM is used
> only at write time (extraction + embeddings); retrieval is pure SQL + a local
> cross-encoder reranker (no LLM at query time). See [`LOCKED_BASELINE.md`](LOCKED_BASELINE.md)
> for the accuracy config.

---

## Fastest path — `memnos` CLI (pip/pipx)
**Postgres is a prerequisite** (an existing PG with the `vector` extension available — the
installer never installs Postgres). Then:
```bash
./install.sh                    # macOS/Linux  (Windows: .\install.ps1)  → installs the `memnos` command
memnos setup                    # enter your Postgres connection → creates schema + admin token
memnos serve                    # start the server → open http://127.0.0.1:8900/admin
```
`memnos --help` lists everything: `setup serve token grant namespace secret stats health
remember recall whoami ns`. Config (DSN, vault key, port) lives in `~/.memnos/config.json`.

The manual/dev steps below are equivalent (and what the launchd service uses).

---

## 1. Prerequisites
- PostgreSQL with **pgvector** (a prerequisite — not installed for you). For local dev a
  container is easy: `docker compose -f docker-compose.poc.yml up -d`.
- Python 3.10+
- An OpenAI API key (extraction + 1536-d embeddings). *Optional:* without it the server
  runs in free **local 384-d** mode (embeddings only, no fact extraction).

## 2. Start Postgres (pgvector)
```bash
docker compose -f docker-compose.poc.yml up -d        # Postgres on host port 5433
```

## 3. Install deps + configure
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
echo 'OPENAI_API_KEY=sk-...' > .env                    # omit for free local mode
```

## 4. Start the memnos server
```bash
.venv/bin/python memnos_server.py                      # http://127.0.0.1:8900
# health: curl -s localhost:8900/healthz   ->  {"ok": true}
```

## 5. Create an identity + token (governance is built in)

> **Use the venv Python** for all commands below: either `source .venv/bin/activate`
> first, or prefix each with `.venv/bin/python`. Plain `python` will fail with
> `ModuleNotFoundError: psycopg`.

**Option A — Management console (recommended):**
```bash
.venv/bin/python memnos_admin.py admin       # prints an ADMIN token once
```
Open **http://127.0.0.1:8900/admin**, paste the admin token, then create namespaces,
mint/revoke tokens, manage grants, and view the dashboard — all from the UI.

**Option B — CLI:**
```bash
.venv/bin/python memnos_admin.py init                            # one-time: control plane
.venv/bin/python memnos_admin.py principal alice                 # create a principal
.venv/bin/python memnos_admin.py token alice                     # prints a token ONCE — copy it
.venv/bin/python memnos_admin.py grant alice "user:alice:notes"  # grant a namespace (ACL)
```
> Namespaces are **explicit** — you create them (console or CLI). memnos does not
> auto-create namespaces. Use a namespace per project/user/agent as you like.

## 6. Use it (HTTP)
```bash
TOK=mnk_...        # the token from step 5
NS=user:alice:notes

# remember
curl -s localhost:8900/remember -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"$NS\",\"text\":\"On 2026-06-07 Alice moved to Seattle and joined Acme as a staff engineer.\"}"

# recall (returns ranked memories + a ready-to-paste context block; NO LLM at query time)
curl -s localhost:8900/recall -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"$NS\",\"query\":\"Where does Alice work and live?\"}"
```

## 6b. Secrets (encrypted vault + auto-redaction)
memnos auto-redacts secret-looking text (API keys, tokens, passwords) from remembered
messages **before** storage, so credentials never leak into recall. For secrets you *do*
want to keep (provider keys, integration creds), use the encrypted vault:
```bash
.venv/bin/python memnos_admin.py secret-keygen     # prints MEMNOS_SECRET_KEY → add to .env
.venv/bin/python memnos_admin.py secret-set openai # prompts for the value (not echoed)
.venv/bin/python memnos_admin.py secret-list
```
Reference a secret anywhere as `secret://openai` (e.g. set `OPENAI_API_KEY=secret://openai`
in `.env` — the server resolves it at startup). Plaintext is AES-256-GCM encrypted at rest
and never logged/listed/returned. Also manageable in the console **Secrets** tab.

## 7. Operate it (the governance/observability dashboard)
```bash
python memnos_admin.py stats     # volume / error% / p50-p95 latency / recall-empty
python memnos_admin.py health    # actionable CRITICAL/WARN findings
python memnos_admin.py usage     # cost per op (extraction tokens tracked)
python memnos_admin.py audit     # who/what/when
```

## 8. Background jobs (optional, recommended)
```bash
python memnos_consolidate.py     # "sleep pass": distill facts → entity dossiers (dirty-only)
python memnos_eval.py            # quality canary: stale-suppression (bi-temporal trust)
```
On macOS these can run via LaunchAgents (`com.memnos.server`, `com.memnos.consolidate`,
`com.memnos.canary`) — see the handoffs in the dev vault.

---

## Endpoints
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/healthz` | — | liveness |
| GET  | `/readyz`  | — | DB reachable |
| GET  | `/metrics` | Bearer | ops rollup |
| POST | `/remember`| Bearer (write) | store a message → raw turn + extracted facts |
| POST | `/recall`  | Bearer (read)  | ranked memories + context block |
| POST | `/consolidate` | Bearer (write) | build entity dossiers |
| POST | `/feedback`| Bearer | was the recall helpful? (quality signal) |

## Next
- **Claude Code:** [`docs/integrations/claude-code.md`](docs/integrations/claude-code.md)
- **Any MCP client (Cursor, Windsurf):** [`docs/integrations/mcp.md`](docs/integrations/mcp.md)
- **Accuracy config + LoCoMo numbers:** [`LOCKED_BASELINE.md`](LOCKED_BASELINE.md)
