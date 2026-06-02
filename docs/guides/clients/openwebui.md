# Connecting memnos to Open WebUI

Open WebUI supports MCP tools natively.

## Local Setup (both running on host)

1. Open WebUI → Settings → Tools → Add Tool Server
2. URL: `http://localhost:8765/sse`
3. Header: `Authorization: Bearer memnos-local-dev-key`
4. Save

All memnos tools (`memory_search`, `memory_write`, `memory_delete`, etc.) appear in the Tools panel.

## Docker Setup

If Open WebUI runs in Docker, `localhost` inside the container refers to the container itself, not the host.

**Option A — host.docker.internal (macOS/Windows):**

```
URL: http://host.docker.internal:8765/sse
```

**Option B — shared Docker network (recommended for production):**

Add both services to the same network in `docker-compose.yml`:

```yaml
services:
  memnos:
    image: memnos:latest
    ports:
      - "8765:8765"
      - "8766:8766"
    networks:
      - ai-net

  open-webui:
    image: ghcr.io/open-webui/open-webui:latest
    ports:
      - "3000:8080"
    environment:
      - WEBUI_SECRET_KEY=your-secret
    networks:
      - ai-net
    depends_on:
      - memnos

networks:
  ai-net:
    driver: bridge
```

Then set the Tool Server URL to `http://memnos:8765/sse` (using the service name as hostname).

## Verify

After adding the tool server, open a chat and type:

```
Search my memories for "project status"
```

Open WebUI should invoke `memory_search` automatically if tool use is enabled, or you can manually select it from the Tools panel.
