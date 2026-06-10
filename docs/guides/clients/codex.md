# Connecting memnos to Codex CLI (OpenAI)

## One command

```bash
memnos agent-setup codex
```

Wires memnos into Codex as an **MCP server** — it writes `[mcp_servers.memnos]` to
`~/.codex/config.toml` (mints a scoped token for you) and adds a memnos section to
`~/.codex/AGENTS.md`. Idempotent; backs up files it edits. **Restart Codex** afterward.

Codex then has the memnos tools — `recall` / `recall_wide`, `remember`, `reconcile_claim`,
`get_entity`, etc. Unlike Claude Code, Codex has **no lifecycle hooks**, so memory isn't
auto-injected/saved — the agent calls the tools explicitly (the `AGENTS.md` note tells it to).

What it writes:

```toml
[mcp_servers.memnos]
command = "memnos"
args = ["mcp"]

[mcp_servers.memnos.env]
MEMNOS_URL = "http://127.0.0.1:8900"
MEMNOS_TOKEN = "mnk_..."
MEMNOS_NS = "user:you"
```

---

## Manual / REST fallback

If you're on a Codex build without MCP, use the REST API or the SDK directly.

### Python SDK

```bash
uv pip install memnos-sdk        # (or: pip install memnos-sdk)
```
```python
from memnos_sdk import MemnosClient
mem = MemnosClient("http://127.0.0.1:8900", token="mnk_...", namespace="user:you")
context = mem.context("your query")     # ready-to-paste; no LLM at query time
mem.remember("Decided to use pgvector over a separate graph DB.")
```

### Shell wrapper (inject memnos context as Codex instructions)

```bash
#!/bin/bash
# ./codex-with-memory.sh "query" [codex args...]
set -euo pipefail
QUERY="${1:-}"; shift || true
CONTEXT=$(curl -s http://127.0.0.1:8900/recall \
  -H "Authorization: Bearer ${MEMNOS_TOKEN:?set MEMNOS_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"${MEMNOS_NS:-user:you}\",\"query\":\"${QUERY}\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('context',''))")
codex --instructions "$CONTEXT" "$@"
```
