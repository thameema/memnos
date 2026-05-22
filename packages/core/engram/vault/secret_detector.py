"""
engram.vault.secret_detector — Scan text for credential patterns.

Called by EngramClient.add() before storing a memory.  Any matched secrets are
redacted in the stored content and a list of DetectedSecret objects is returned
so the caller can warn the user.

Supported patterns:
  anthropic_api_key   sk-ant-...
  openai_api_key      sk-... (48 chars)
  github_pat          ghp_... (36 chars)
  github_fine_grained github_pat_... (82 chars)
  gitlab_pat          glpat-...
  aws_access_key_id   AKIA... (16 uppercase alphanums)
  jwt_token           eyJ...<base64>.<base64>.<base64>
  slack_token         xox[bpas]-...
  stripe_api_key      sk_live_... / sk_test_... / pk_live_... / pk_test_...
  bearer_token        Bearer <40+ char token>
  generic_env_secret  KEY_NAME=value (env-var style, ≥20 char value)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DetectedSecret:
    pattern_name: str
    match: str
    start: int
    end: int


# ---------------------------------------------------------------------------
# Compiled patterns — order matters: more specific patterns first so they
# take precedence over broader ones (e.g. anthropic before openai).
# Each entry is (compiled_pattern, pattern_name).
# ---------------------------------------------------------------------------
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anthropic API key: sk-ant-<20+ alphanums/dash/underscore>
    (
        re.compile(r"\bsk-ant-[a-zA-Z0-9\-_]{20,}\b"),
        "anthropic_api_key",
    ),
    # OpenAI API key: sk-<exactly 48 alphanums>
    (
        re.compile(r"\bsk-[a-zA-Z0-9]{48}\b"),
        "openai_api_key",
    ),
    # GitHub fine-grained PAT: github_pat_<82 chars of alphanums/underscore>
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
        "github_fine_grained",
    ),
    # GitHub classic PAT: ghp_<36 alphanums>
    (
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "github_pat",
    ),
    # GitLab PAT: glpat-<20+ alphanums/dash/underscore>
    (
        re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b"),
        "gitlab_pat",
    ),
    # AWS Access Key ID: AKIA followed by exactly 16 uppercase alphanums
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "aws_access_key_id",
    ),
    # JWT token: eyJ<base64url>.<base64url>.<base64url>
    (
        re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        "jwt_token",
    ),
    # Slack token: xox followed by b/p/a/s then dash and the rest
    (
        re.compile(r"\bxox[bpas]-[A-Za-z0-9\-]+"),
        "slack_token",
    ),
    # Stripe API key: sk_live_, sk_test_, pk_live_, pk_test_
    (
        re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
        "stripe_api_key",
    ),
    # Bearer token: "Bearer " followed by 40+ non-whitespace chars
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9\-_=+/]{40,}"),
        "bearer_token",
    ),
    # Generic env-var secret: KEY_NAME=<20+ non-whitespace chars>
    # Key must start with an uppercase letter, be 5+ chars, and end with a
    # known sensitive suffix.  Value side is case-insensitive (matched by \S).
    (
        re.compile(
            r"[A-Z][A-Z0-9_]{4,}(?:_KEY|_SECRET|_TOKEN|_PASSWORD|_PASS|_PWD)"
            r"\s*=\s*\S{20,}",
            re.IGNORECASE,
        ),
        "generic_env_secret",
    ),
]


def detect(text: str) -> list[DetectedSecret]:
    """Scan *text* for all recognised secret patterns.

    Returns a deduplicated (by position) list of :class:`DetectedSecret`
    objects sorted by their start position.
    """
    found: dict[tuple[int, int], DetectedSecret] = {}

    for pattern, name in _PATTERNS:
        for m in pattern.finditer(text):
            pos = (m.start(), m.end())
            # If two patterns overlap at the same start position, keep the one
            # that was registered first (i.e. the more specific one).
            if pos not in found:
                found[pos] = DetectedSecret(
                    pattern_name=name,
                    match=m.group(),
                    start=m.start(),
                    end=m.end(),
                )

    return sorted(found.values(), key=lambda s: s.start)


def redact(text: str, detected: list[DetectedSecret]) -> str:
    """Replace each matched secret in *text* with ``***REDACTED[name]***``.

    Processes matches from end to start so that earlier character positions
    remain valid throughout the operation.
    """
    result = text
    for secret in sorted(detected, key=lambda s: s.start, reverse=True):
        replacement = f"***REDACTED[{secret.pattern_name}]***"
        result = result[: secret.start] + replacement + result[secret.end :]
    return result
