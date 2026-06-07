"""Secret redaction for ingestion — strip credentials BEFORE they enter memory.

The memory-specific leakage risk: a user pastes "my key is sk-..." → it gets stored as a
raw turn + extracted fact → surfaced verbatim in recall context → leaked back to the LLM
and logs. We redact known secret shapes at write time so plaintext secrets never land in
raw_turns / semantic / recall. Pattern-based (no LLM, no key needed) — always on.
"""
import re

# (label, compiled pattern). Order matters: most specific first.
_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
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
    return text, n


if __name__ == "__main__":   # quick manual check
    import sys
    print(redact(sys.stdin.read()))
