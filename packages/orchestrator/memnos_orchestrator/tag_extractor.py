"""Simple tag extractor for categorising tasks by domain."""
from __future__ import annotations

import re
from typing import Pattern

_TAG_PATTERNS: list[tuple[str, Pattern]] = [
    ("code", re.compile(r"\b(code|function|class|bug|refactor|test|implement|debug|api|endpoint)\b", re.I)),
    ("research", re.compile(r"\b(research|find|search|look up|investigate|explore|discover)\b", re.I)),
    ("writing", re.compile(r"\b(write|document|summarize|draft|explain|describe|report)\b", re.I)),
    ("data", re.compile(r"\b(data|analyze|chart|graph|csv|database|sql|query|metrics)\b", re.I)),
    ("planning", re.compile(r"\b(plan|design|architect|outline|structure|organize|roadmap)\b", re.I)),
    ("memory", re.compile(r"\b(remember|recall|memory|store|retrieve|forget|history)\b", re.I)),
]


def extract_tags(task: str) -> list[str]:
    """Extract domain tags from a task description via keyword matching.

    Parameters
    ----------
    task:
        The raw task prompt or description to classify.

    Returns
    -------
    list[str]
        One or more domain tag strings.  Falls back to ``["general"]`` when no
        pattern matches.
    """
    tags = []
    for tag, pattern in _TAG_PATTERNS:
        if pattern.search(task):
            tags.append(tag)
    return tags or ["general"]
