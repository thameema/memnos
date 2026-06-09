# memnos memory server image. Local embeddings + cross-encoder rerank run in-process on
# ONNX Runtime (fastembed) — no torch, so the image stays light.
FROM python:3.11-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 HF_HOME=/app/.hf

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core/ ./core/
COPY ui/ ./ui/
COPY memnos_server.py memnos_admin.py memnos_cli.py memnos_mcp.py memnos_consolidate.py nsresolve.py ./
# pre-warm the reranker/embedder model downloads into the image layer (optional; comment to slim)
RUN python -c "from core import rerank; rerank.rerank('warm',['a','b'])" || true

EXPOSE 8900
CMD ["python", "memnos_server.py"]
