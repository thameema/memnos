"""
memnos.llm.subscription_client — Subscription-aware LLM dispatcher.

Priority order (cheapest first):
  1. claude --print        (Claude Code Max/Pro subscription)
  2. gh copilot explain    (GitHub Copilot subscription)
  3. ANTHROPIC_API_KEY     (direct API — pay per token)
  4. OPENAI_API_KEY        (direct API — pay per token)

Usage:
    from memnos.llm.subscription_client import complete, get_backend

    text = complete("Answer this question: ...")
    print(get_backend())   # "claude-cli" | "copilot-cli" | "anthropic" | "openai" | None
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import textwrap
from functools import lru_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend detection (cached — checked once per process)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_backend() -> str | None:
    """Return the name of the best available LLM backend, or None."""
    if _claude_available():
        return "claude-cli"
    if _copilot_available():
        return "copilot-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _claude_available() -> bool:
    """True if `claude --print` is available (Claude Code installed)."""
    if not shutil.which("claude"):
        return False
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _copilot_available() -> bool:
    """True if `gh copilot explain` is available (GitHub Copilot CLI extension)."""
    if not shutil.which("gh"):
        return False
    try:
        r = subprocess.run(
            ["gh", "copilot", "--version"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

def complete(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    timeout: int = 60,
    backend: str | None = None,
) -> str:
    """Call the best available LLM backend and return the response text.

    Args:
        prompt:     The user prompt.
        model:      Model override (only applies to API backends).
        max_tokens: Max tokens for API backends.
        timeout:    Subprocess timeout in seconds.
        backend:    Force a specific backend (overrides auto-detect).

    Returns:
        Response text, or empty string on failure.
    """
    chosen = backend or get_backend()
    if chosen is None:
        logger.warning(
            "subscription_client: no LLM backend available — "
            "install Claude Code or set ANTHROPIC_API_KEY / OPENAI_API_KEY"
        )
        return ""

    try:
        if chosen == "claude-cli":
            return _claude_complete(prompt, timeout=timeout)
        if chosen == "copilot-cli":
            return _copilot_complete(prompt, timeout=timeout)
        if chosen == "anthropic":
            return _anthropic_complete(prompt, model=model, max_tokens=max_tokens)
        if chosen == "openai":
            return _openai_complete(prompt, model=model, max_tokens=max_tokens)
    except Exception as exc:
        logger.warning("subscription_client: %s backend failed: %s", chosen, exc)
    return ""


def _claude_complete(prompt: str, timeout: int) -> str:
    """Call `claude --print` — uses Claude Code Max/Pro subscription."""
    result = subprocess.run(
        ["claude", "--print", prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0 and result.stderr:
        logger.debug("claude --print stderr: %s", result.stderr[:200])
    return result.stdout.strip()


def _copilot_complete(prompt: str, timeout: int) -> str:
    """Call `gh copilot explain` — uses GitHub Copilot subscription.

    Note: gh copilot explain is designed for shell command explanation,
    so we frame general prompts as command explanation requests.
    For better results, use claude-cli if available.
    """
    # gh copilot explain reads from stdin
    result = subprocess.run(
        ["gh", "copilot", "explain", prompt[:500]],
        capture_output=True, text=True, timeout=timeout,
        input=prompt,
    )
    return result.stdout.strip()


def _anthropic_complete(prompt: str, model: str | None, max_tokens: int) -> str:
    import anthropic  # type: ignore
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model or "claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _openai_complete(prompt: str, model: str | None, max_tokens: int) -> str:
    from openai import OpenAI  # type: ignore
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model or "gpt-4o-mini",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Convenience: log which backend is active
# ---------------------------------------------------------------------------

def log_backend() -> None:
    b = get_backend()
    if b == "claude-cli":
        logger.info("LLM backend: claude --print (Claude Code subscription — no API cost)")
    elif b == "copilot-cli":
        logger.info("LLM backend: gh copilot (GitHub Copilot subscription — no API cost)")
    elif b == "anthropic":
        logger.info("LLM backend: Anthropic API (direct — API cost applies)")
    elif b == "openai":
        logger.info("LLM backend: OpenAI API (direct — API cost applies)")
    else:
        logger.warning("LLM backend: none available")
