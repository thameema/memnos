# Connecting memnos to Codex CLI (OpenAI)

Codex CLI does **not** support MCP. Use the memnos REST API instead.

## Option A — Python SDK

```bash
pip install memnos-sdk
```

```python
from memnos_sdk import MemnosClient

client = MemnosClient("http://localhost:8766", api_key="memnos-local-dev-key")
memories = client.search("your query", "your:namespace", top_k=5)
context = "\n".join(m.content for m in memories)

# Pass context to codex via --instructions or stdin
```

## Option B — Shell Wrapper Script

Create `codex-with-memory.sh`:

```bash
#!/bin/bash
# Usage: ./codex-with-memory.sh "query" [codex args...]
# Injects top-5 memnos memories matching the query as Codex instructions

QUERY="${1:-}"
shift

MEMORIES=$(curl -s "http://localhost:8766/api/v1/memory/search?q=${QUERY}&top_k=5" \
  -H "Authorization: Bearer memnos-local-dev-key" \
  | python3 -c "import json,sys; [print(m['content']) for m in json.load(sys.stdin)]")

codex --instructions "$MEMORIES" "$@"
```

```bash
chmod +x codex-with-memory.sh
./codex-with-memory.sh "project context" "refactor this function"
```

## Option C — Namespace-scoped search

To search a specific namespace (e.g. personal notes only):

```bash
MEMORIES=$(curl -s "http://localhost:8766/api/v1/memory/search?q=project+status&ns=personal:me&top_k=5" \
  -H "Authorization: Bearer memnos-local-dev-key" \
  | python3 -c "import json,sys; [print(m['content']) for m in json.load(sys.stdin)]")
```

Drop the `ns=` parameter to search all accessible namespaces.
