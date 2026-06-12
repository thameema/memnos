"""Embedding helpers used by the server, the consolidation worker, and the benchmark.

`CachedEmbedder` wraps OpenAI `text-embedding-3-small` (1536-d) with an in-memory cache and
a batch `prime()` so the bulk of turns/facts embed in a few calls instead of thousands. The
caller passes in the OpenAI client and a cost meter, so this module has no hard dependency on
the `openai` package or on any credentials.

Memory bound (issue #8): a 1536-d embedding stored as a Python list of floats costs ~56 KB
of heap — an unbounded cache through a 35K-request ingest is multiple GB of growth that
never frees. Two fixes: entries are stored as `array('f')` (~6 KB, 9x smaller; still a
Sequence[float] for vlit()/SQL), and the cache is a bounded LRU
(MEMNOS_EMBED_CACHE_MAX, default 4096 entries ≈ 25 MB) — dedup within an ingest batch is
what the cache is for, so a bounded window keeps the hit-rate benefit without the leak.
"""
from __future__ import annotations

import os
import threading
import time
from array import array
from collections import OrderedDict

CACHE_MAX = max(64, int(os.environ.get("MEMNOS_EMBED_CACHE_MAX", "4096")))


class QueryEmbedCache:
    """Short-TTL bounded LRU for QUERY embeddings, keyed (query text, model) — issue #12.

    Hooks and Claude Desktop repeat near-identical queries within seconds (the same
    prompt fans into /recall + /memory/context, retries, paginated follow-ups); a 60s
    TTL lets those repeats skip the OpenAI embedding round-trip entirely while staying
    too short to ever serve a stale embedding-model swap. Distinct from CachedEmbedder's
    ingest cache: that LRU is sized for WRITE-path dedup and gets churned by every
    ingest batch — query embeddings need their own small, churn-proof window.
    MEMNOS_QUERY_CACHE_TTL_S tunes the TTL (0 disables)."""

    def __init__(self, ttl_s: float = 60.0, maxsize: int = 256):
        self.ttl, self.maxsize = float(ttl_s), int(maxsize)
        self._d: OrderedDict[tuple, tuple] = OrderedDict()    # key -> (expires, vec)
        self._lock = threading.Lock()

    def get(self, query: str, model: str):
        if self.ttl <= 0:
            return None
        k = (query, model)
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(k)
            if hit is None:
                return None
            if hit[0] < now:
                del self._d[k]
                return None
            self._d.move_to_end(k)
            return hit[1]

    def put(self, query: str, model: str, vec) -> None:
        if self.ttl <= 0 or vec is None:
            return
        with self._lock:
            self._d[(query, model)] = (time.monotonic() + self.ttl, array("f", vec))
            self._d.move_to_end((query, model))
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


class CachedEmbedder:
    """OpenAI text-embedding-3-small (1536-d) with a bounded LRU cache + batch `prime()`."""
    dim = 1536

    def __init__(self, client, meter, model="text-embedding-3-small", dim=1536,
                 cache_max=CACHE_MAX):
        self.client, self.meter, self.model, self.dim = client, meter, model, dim
        self.cache: OrderedDict[str, array] = OrderedDict()
        self.cache_max = cache_max
        self._lock = threading.Lock()

    def _put(self, text, vec):
        # compact storage (array('f') ≈ 6 KB vs ~56 KB for a list of Python floats)
        self.cache[text] = array("f", vec)
        self.cache.move_to_end(text)
        while len(self.cache) > self.cache_max:
            self.cache.popitem(last=False)            # evict least-recently-used

    def prime(self, texts):
        with self._lock:
            uniq = [t for t in dict.fromkeys(texts) if t and t not in self.cache]
        for i in range(0, len(uniq), 512):
            chunk = uniq[i:i + 512]
            r = self.client.embeddings.create(model=self.model, input=chunk)
            self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
            with self._lock:
                for t, d in zip(chunk, r.data):
                    self._put(t, d.embedding)

    def embed(self, text):
        with self._lock:
            v = self.cache.get(text)
            if v is not None:
                self.cache.move_to_end(text)          # LRU touch
                return v
        r = self.client.embeddings.create(model=self.model, input=text)
        self.meter.record("embed", self.model, r.usage.prompt_tokens, 0)
        v = r.data[0].embedding
        with self._lock:
            self._put(text, v)
            return self.cache[text]

    __call__ = embed
