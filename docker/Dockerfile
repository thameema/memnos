FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install all packages in one layer
COPY packages/core/pyproject.toml packages/core/
COPY packages/mcp-server/pyproject.toml packages/mcp-server/
COPY packages/orchestrator/pyproject.toml packages/orchestrator/
COPY packages/api/pyproject.toml packages/api/
COPY packages/gateway/pyproject.toml packages/gateway/
COPY packages/learning/pyproject.toml packages/learning/

RUN pip install --no-cache-dir \
    "packages/core[all]" \
    "packages/mcp-server" \
    "packages/orchestrator" \
    "packages/api" \
    "packages/gateway" \
    "packages/learning" \
    2>/dev/null || true

# Copy source
COPY packages/ packages/
COPY agents/ agents/
COPY skills/ skills/

RUN pip install --no-cache-dir \
    -e packages/core \
    -e packages/mcp-server \
    -e packages/orchestrator \
    -e packages/api \
    -e packages/gateway \
    -e packages/learning

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765 8766

ENTRYPOINT ["/entrypoint.sh"]
