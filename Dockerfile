# memnos memory server image. Heavy (~torch via sentence-transformers) because local
# embeddings + cross-encoder rerank run in-process. For an OpenAI-only/light build,
# drop sentence-transformers and run with OPENAI_API_KEY set + reranker disabled.
FROM python:3.11-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 HF_HOME=/app/.hf

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY memnos_brain/ ./memnos_brain/
COPY memnos_core/ ./memnos_core/
COPY ui/ ./ui/
COPY memnos_server.py memnos_admin.py memnos_cli.py validate_brain.py locomo_pg_parallel.py ./
# pre-warm the reranker/embedder model downloads into the image layer (optional; comment to slim)
RUN python -c "from memnos_brain import rerank; rerank.rerank('warm',['a','b'])" || true

EXPOSE 8900
CMD ["python", "memnos_server.py"]
