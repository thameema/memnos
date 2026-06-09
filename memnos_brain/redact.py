"""Secret redaction for ingestion — strip credentials BEFORE they enter memory.

The memory-specific leakage risk: a user pastes "my key is sk-..." → it gets stored as a
raw turn + extracted fact → surfaced verbatim in recall context → leaked back to the LLM
and logs. We redact known secret shapes at write time so plaintext secrets never land in
raw_turns / semantic / recall. Pattern-based (no LLM, no key needed) — always on.
"""
import math
import re

# (label, compiled pattern). Order matters: most specific first.
_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("gh_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("memnos_token", re.compile(r"\bmnk_[A-Za-z0-9_-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}\b")),
    # key: value / password = value forms
    ("credential", re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|pwd|token|access[_-]?key)\b"
                              r"\s*[:=]\s*[\"']?([^\s\"';,]{8,})[\"']?")),
]


# entropy catch-all: long, mixed-charset, high-entropy tokens that named patterns missed
# (unknown vendors, raw API tokens). Tuned to NOT trip on normal prose / hex hashes-in-words.
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-+/=]{28,}\b")


def _shannon(s):
    if not s:
        return 0.0
    from collections import Counter
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _looks_secret(tok):
    # require length, BOTH letters and digits (or +/=), and high entropy
    if len(tok) < 28:
        return False
    has_alpha = any(c.isalpha() for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    has_sym = any(c in "_-+/=" for c in tok)
    if not (has_alpha and (has_digit or has_sym)):
        return False
    return _shannon(tok) >= 3.5


def redact(text):
    """Return (clean_text, n_redacted). Replaces each secret with [REDACTED:label]."""
    if not text:
        return text, 0
    n = 0
    for label, pat in _PATTERNS:
        if label == "credential":
            def _sub(m):
                nonlocal n
                n += 1
                return m.group(0)[:m.start(1) - m.start(0)] + f"[REDACTED:{label}]"
            text = pat.sub(_sub, text)
        else:
            def _sub2(m):
                nonlocal n
                n += 1
                return f"[REDACTED:{label}]"
            text = pat.sub(_sub2, text)

    def _ent(m):
        nonlocal n
        tok = m.group(0)
        if tok.startswith("[REDACTED") or not _looks_secret(tok):
            return tok
        n += 1
        return "[REDACTED:high_entropy]"
    text = _TOKEN.sub(_ent, text)
    return text, n


if __name__ == "__main__":   # quick manual check
    import sys
    print(redact(sys.stdin.read()))
