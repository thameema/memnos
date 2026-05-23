FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 1: install heavy dependencies before copying source ──────────────
# Copy only pyproject.toml files first so this layer is cache-valid as long
# as dependency specs don't change (source edits don't bust this cache).
COPY packages/core/pyproject.toml          packages/core/pyproject.toml
COPY packages/mcp-server/pyproject.toml    packages/mcp-server/pyproject.toml
COPY packages/orchestrator/pyproject.toml  packages/orchestrator/pyproject.toml
COPY packages/api/pyproject.toml           packages/api/pyproject.toml
COPY packages/gateway/pyproject.toml       packages/gateway/pyproject.toml
COPY packages/learning/pyproject.toml      packages/learning/pyproject.toml

# Install all third-party deps declared across packages.
# These are listed explicitly so pip can resolve them without source present.
RUN pip install --no-cache-dir \
    "httpx>=0.27" \
    "sentence-transformers>=2.7" \
    "spacy>=3.7" \
    "anthropic>=0.30" \
    "pydantic>=2.0" \
    "pyyaml>=6.0" \
    "python-dotenv>=1.0" \
    "tenacity>=8.0" \
    "cryptography>=42.0" \
    "mcp>=1.0" \
    "fastapi>=0.111" \
    "uvicorn[standard]>=0.29" \
    "sse-starlette>=2.0" \
    "openai>=1.30" \
    "aiosqlite>=0.20" \
    "apscheduler>=3.10" \
    "python-telegram-bot>=21.0"

# Download spaCy English model for named-entity extraction
RUN python -m spacy download en_core_web_sm

# ── Layer 2: install engram packages in editable mode ─────────────────────
COPY packages/ packages/
COPY agents/   agents/
COPY skills/   skills/

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
