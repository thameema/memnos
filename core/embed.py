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
from array import array
from collections import OrderedDict

CACHE_MAX = max(64, int(os.environ.get("MEMNOS_EMBED_CACHE_MAX", "4096")))


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
