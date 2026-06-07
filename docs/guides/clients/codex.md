# Connecting memnos to Codex CLI (OpenAI)

Codex CLI does **not** support MCP. Use the memnos REST API or the Python SDK.

## Prerequisites

- memnos running (`memnos serve`, default `http://127.0.0.1:8900`) — see [quickstart](../quickstart.md).
- A scoped token + namespace (`memnos token alice` → `mnk_…`; `memnos grant alice user:alice`).

## Option A — Python SDK (recommended)

```bash
pip install memnos-sdk
```

```python
from memnos_sdk import MemnosClient

mem = MemnosClient("http://127.0.0.1:8900", token="mnk_...", namespace="user:alice")

# A ready-to-paste context block (ranked, no LLM at query time):
context = mem.context("your query")

# Or the structured result:
result = mem.recall("your query")          # {"memories": [...], "context": "..."}

# Store something:
mem.remember("Decided to use pgvector over a separate graph DB.")

# Pass the context to Codex via --instructions or stdin
```

## Option B — Shell wrapper (REST)

Create `codex-with-memory.sh`:

```bash
#!/bin/bash
# Usage: ./codex-with-memory.sh "query" [codex args...]
# Injects the memnos context block for the query as Codex instructions.
set -euo pipefail
QUERY="${1:-}"; shift || true

CONTEXT=$(curl -s http://127.0.0.1:8900/recall \
  -H "Authorization: Bearer ${MEMNOS_TOKEN:?set MEMNOS_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":\"${MEMNOS_NS:-user:alice}\",\"query\":\"${QUERY}\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('context',''))")

codex --instructions "$CONTEXT" "$@"
```

```bash
chmod +x codex-with-memory.sh
export MEMNOS_TOKEN=mnk_... MEMNOS_NS=user:alice
./codex-with-memory.sh "auth service decisions" "refactor this handler"
```

The `/recall` endpoint returns ranked memories **and** a ready-to-paste `context` string —
no LLM at query time. To store from the shell, `POST /remember` with
`{"namespace": "...", "text": "..."}`.
