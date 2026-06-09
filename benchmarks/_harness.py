"""LoCoMo benchmark constants + re-exports of the shared `core` helpers, so benchmarks/
depends only on the single `core` engine.

- CATEGORY_MAP   — LoCoMo question-category id → label
- RELAXED_SYS    — the answerer system prompt used for the locked baseline
- TSCostMeter    — thread-safe USD/token cost meter (from core.usage)
- CachedEmbedder — OpenAI text-embedding-3-small (1536-d) cache (from core.embed)
"""
from __future__ import annotations

import os.path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.usage import TSCostMeter  # noqa: E402,F401  (re-exported)
from core.embed import CachedEmbedder  # noqa: E402,F401  (re-exported)

# LoCoMo question categories (5 = adversarial / unanswerable, excluded from scoring).
CATEGORY_MAP = {1: "single_hop", 2: "multi_hop", 3: "temporal", 4: "open_domain", 5: "adversarial"}

# Answerer system prompt for the locked baseline (see LOCKED_BASELINE.md). The production
# answerer is the *calling agent*; the eval uses this prompt with the configured model.
RELAXED_SYS = ("Use the retrieved memories as your primary evidence and reason over them "
               "to answer. It is fine to make a small, well-supported inference from the "
               "memories. Give your best concise answer (a short phrase). Only say "
               "'Not mentioned' if there is truly no relevant information.")
