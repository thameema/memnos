"""Embedding helpers used by the server, the consolidation worker, and the benchmark.

`CachedEmbedder` wraps OpenAI `text-embedding-3-small` (1536-d) with an in-memory cache and
a batch `prime()` so the bulk of turns/facts embed in a few calls instead of thousands. The
caller passes in the OpenAI client and a cost meter, so this module has no hard dependency on
the `openai` package or on any credentials.
"""
from __future__ import annotations

import threading


class CachedEmbedder:
    """OpenAI text-embedding-3-small (1536-d) with a cache + batch `prime()`."""
    dim = 1536

    def __init__(self, client, meter, model="text-embedding-3-small", dim=1536):
        self.client, self.meter, self.model, self.dim = client, meter, model, dim
        self.cache: dict[str, list] = {}
        self._lock = threading.Lock()

    def prime(self, texts):
        uniq = [t for t in dict.fromkeys(texts) if t and t not in self.cache]
        for i in range(0, len(uniq), 512):
            chunk = uniq[i:i + 512]
            r = self.client.embeddings.create(model=self.model, input=chunk)
            self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
            for t, d in zip(chunk, r.data):
                self.cache[t] = d.embedding

    def embed(self, text):
        v = self.cache.get(text)
        if v is not None:
            return v
        r = self.client.embeddings.create(model=self.model, input=text)
        self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
        v = r.data[0].embedding
        with self._lock:
            self.cache[text] = v
        return v

    __call__ = embed
