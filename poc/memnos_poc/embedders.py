"""Pluggable embedders. OpenAI (default per decision) or local — same interface,
so swapping is config, not code. OpenAI calls are metered via CostMeter.
"""
from __future__ import annotations

from memnos_poc import local_models


class LocalEmbedder:
    """bge-small-en-v1.5, 384-d, free/local."""
    dim = 384

    def embed(self, text: str) -> list[float]:
        return local_models.embed(text)


class OpenAIEmbedder:
    """text-embedding-3-small, 1536-d. Negligible cost, metered."""

    def __init__(self, client, meter, model: str = "text-embedding-3-small", dim: int = 1536):
        self.client, self.meter, self.model, self.dim = client, meter, model, dim

    def embed(self, text: str) -> list[float]:
        r = self.client.embeddings.create(model=self.model, input=text)
        self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
        return r.data[0].embedding
