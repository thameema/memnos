# Connecting memnos to Open WebUI

Open WebUI can call external tools via **OpenAPI tool servers**. The open-source memnos
server exposes a REST API (no hosted MCP-over-HTTP endpoint), so you point Open WebUI at
that REST API with a small OpenAPI spec.

## Prerequisites

- memnos running (`memnos start`, default `http://127.0.0.1:8900`) — see [quickstart](../quickstart.md).
- A scoped token + namespace (`memnos token alice` → `mnk_…`; `memnos grant alice user:alice`).

## Add memnos as a tool server

**Settings → Tools → Add Tool Server** and provide the memnos base URL plus an OpenAPI spec
describing `/recall` and `/remember` (see the [ChatGPT guide](chatgpt.md) for the same
schema). Set the auth header:

```
Authorization: Bearer mnk_...
```

## Docker networking

If Open WebUI runs in Docker, `localhost` points at the container, not the host:

- **macOS / Windows:** use `http://host.docker.internal:8900`
- **Linux / same compose network:** run memnos on the shared network and use its service
  name, e.g. `http://memnos:8900`

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:latest
    ports: ["3000:8080"]
    extra_hosts: ["host.docker.internal:host-gateway"]   # Linux
```

(memnos itself connects to *your* PostgreSQL — it is the only datastore; there is no second
service to run.)

## Verify

In a chat, ask: *"recall my notes about project status (namespace user:alice)"*. Open WebUI
invokes the `recall` tool and the model answers from the returned context — with no LLM at
query time inside memnos. Confirm writes in the console at `http://127.0.0.1:8900/admin`.
