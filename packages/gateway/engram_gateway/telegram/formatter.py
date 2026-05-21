"""
engram_gateway.telegram.formatter — Text formatting helpers for Telegram messages.

Telegram has a 4096-character hard limit per message.  Long results are
truncated with a note, and callers can optionally send the full text as a
file attachment instead.
"""

from __future__ import annotations

_TELEGRAM_MAX_LEN = 4096
_TRUNCATION_SUFFIX = "\n\n… _(truncated — full result available as attachment)_"


def format_result(text: str, max_len: int = _TELEGRAM_MAX_LEN) -> str:
    """
    Prepare a result string for delivery as a Telegram message.

    If *text* exceeds *max_len* characters it is truncated and a note is
    appended.  The caller should then send the full text as a document.

    Parameters
    ----------
    text:
        Raw result text from the orchestrator.
    max_len:
        Character limit (default: 4096, Telegram's hard cap).

    Returns
    -------
    str
        Formatted text ready to send.
    """
    if not text:
        return "_(empty response)_"

    if len(text) <= max_len:
        return text

    # Reserve space for the suffix
    keep = max_len - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


def format_search_results(results: list[dict]) -> str:
    """
    Format a list of memory search results into a compact Telegram message.

    Parameters
    ----------
    results:
        List of dicts with keys ``content``, ``score``, ``created_at``, ``id``.

    Returns
    -------
    str
        A Markdown-formatted message.
    """
    if not results:
        return "No memories found."

    lines = ["*Search results:*\n"]
    for i, r in enumerate(results, 1):
        content = r.get("content", "")
        score = r.get("score", 0.0)
        created_at = r.get("created_at", "")
        if isinstance(created_at, str) and "T" in created_at:
            created_at = created_at.split("T")[0]  # date only
        preview = content[:200] + ("…" if len(content) > 200 else "")
        lines.append(f"*{i}.* `[{score:.2f}]` {preview}")
        if created_at:
            lines.append(f"   _({created_at})_")
        lines.append("")

    return format_result("\n".join(lines))


def format_task_status(task_id: str, status: str, result: str | None, error: str | None) -> str:
    """Format a task status response."""
    lines = [f"*Task* `{task_id[:8]}…`", f"*Status:* {status}"]
    if result:
        lines.append(f"\n*Result:*\n{result[:1000]}")
    if error:
        lines.append(f"\n*Error:* `{error[:500]}`")
    return format_result("\n".join(lines))
