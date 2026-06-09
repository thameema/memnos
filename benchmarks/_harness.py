"""Self-contained helpers for the LoCoMo benchmark, so benchmarks/ depends only on the
single `core` engine (no legacy substrate). These are the bits the eval harness needs:

- CATEGORY_MAP   — LoCoMo question-category id → label
- RELAXED_SYS    — the answerer system prompt used for the locked baseline
- TSCostMeter    — thread-safe USD/token cost meter (subclass of core.usage.CostMeter)
- CachedEmbedder — OpenAI text-embedding-3-small (1536-d) with a cache + batch prime()
"""
from __future__ import annotations

import os.path
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.usage import CostMeter  # noqa: E402

# LoCoMo question categories (5 = adversarial / unanswerable, excluded from scoring).
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "adversarial"}

# Answerer system prompt for the locked baseline (see LOCKED_BASELINE.md). The production
# answerer is the *calling agent*; the eval uses this prompt with the configured model.
RELAXED_SYS = ("Use the retrieved memories as your primary evidence and reason over them "
               "to answer. It is fine to make a small, well-supported inference from the "
               "memories. Give your best concise answer (a short phrase). Only say "
               "'Not mentioned' if there is truly no relevant information.")


class TSCostMeter(CostMeter):
    """Thread-safe meter: lock around record() so concurrent calls accumulate correctly
    and the budget cap stays authoritative."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._lock = threading.Lock()

    def record(self, *a, **k):
        with self._lock:
            return super().record(*a, **k)


class CachedEmbedder:
    """OpenAI text-embedding-3-small (1536-d) with a cache + batch `prime()` so the bulk
    of turns/facts embed in a few calls instead of thousands."""
    dim = 1536

    def __init__(self, client, meter, model="text-embedding-3-small", dim=1536):
        self.client, self.meter, self.model, self.dim = client, meter, model, dim
        self.cache = {}
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
