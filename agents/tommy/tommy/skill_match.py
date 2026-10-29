"""Rank installed skills against a prompt with plain BM25 over name + description.
No model, no network, no resident process: this is the matcher a per-prompt hint would use, kept
small enough to measure offline before anything depends on it (`tommy skills bench`)."""
from __future__ import annotations

import math
import re
from collections import Counter

_STOP = set("a an and are as at be by can for from has have how i if in is it its me my of on or our so that the "
            "their then there these this to up use used uses using want was we what when where which while who "
            "will with you your do does not any all also into out over such than them they".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suf in ("ing", "es", "ed", "s"):   # crude stemmer: enough to join test/tests/testing
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class Index:
    """BM25 (k1=1.5, b=0.75) over one document per skill. The name is counted twice: it is the
    strongest single signal a description has."""

    def __init__(self, skills: list[tuple[str, str]]):
        self.names = [n for n, _ in skills]
        self.docs = [Counter(tokens(f"{n} {n} {d}")) for n, d in skills]
        self.len = [sum(c.values()) for c in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df: Counter = Counter()
        for c in self.docs:
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def rank(self, prompt: str, k: int = 3) -> list[tuple[str, float]]:
        q = set(tokens(prompt))
        scored = []
        for name, c, ln in zip(self.names, self.docs, self.len):
            s = 0.0
            for t in q:
                f = c.get(t)
                if f:
                    s += self.idf[t] * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * ln / self.avg))
            if s > 0:
                scored.append((name, s))
        return sorted(scored, key=lambda x: -x[1])[:k]
