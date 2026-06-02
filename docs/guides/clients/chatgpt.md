# Connecting memnos to ChatGPT

ChatGPT Plus/Pro supports MCP via **Connectors** (Settings → Connectors → Add MCP Server).

## Requirement: Public URL

memnos MCP runs on `localhost:8765`. ChatGPT requires a publicly accessible URL. Use ngrok to expose it:

```bash
ngrok http 8765
```

Copy the forwarding URL (e.g. `https://abc123.ngrok-free.app`).

## Add as MCP Connector

1. Open ChatGPT → Settings → Connectors → Add MCP Server
2. URL: `https://abc123.ngrok-free.app/sse`
3. Add header: `Authorization: Bearer memnos-local-dev-key`
4. Save and enable

## Limitations

ChatGPT MCP support is limited:
- `memory_search` and `memory_write` work via explicit tool calls
- **Hooks do not fire** — auto-inject on session start is not supported
- You must explicitly ask ChatGPT to call memnos tools (e.g. "search my memories for X")

## Alternative: GPT Action (REST API)

Use the memnos REST API at `localhost:8766` via a custom GPT Action with an OpenAPI spec.

```yaml
openapi: 3.1.0
info:
  title: memnos Memory API
  version: 1.0.0
servers:
  - url: https://abc123.ngrok-free.app
paths:
  /api/v1/memory/search:
    get:
      operationId: searchMemory
      summary: Search memories
      parameters:
        - name: q
          in: query
          required: true
          schema:
            type: string
        - name: top_k
          in: query
          schema:
            type: integer
            default: 5
      responses:
        "200":
          description: List of matching memories
  /api/v1/memory/:
    post:
      operationId: writeMemory
      summary: Write a memory
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                content:
                  type: string
                namespace:
                  type: string
                tags:
                  type: array
                  items:
                    type: string
      responses:
        "201":
          description: Memory created
```

Add authentication: API Key in `Authorization` header with value `Bearer memnos-local-dev-key`.
