"""
engram.corpus.extractor — Structural markdown extractor for architecture docs.

Parses a markdown file into typed engram memory nodes:

  constraint  — sentences containing SHALL / MUST / MUST NOT / REQUIRED / PROHIBITED
  decision    — sections titled Decision / ADR / Rationale, or "We chose / Selected" sentences
  fact        — all other content under API / Interface / Configuration headings

Each node carries:
  - content   : "[<TYPE>|<SEVERITY>] <text>\\nSource: <file> | Section: <heading>"
  - tags       : ["corpus:<corpus_id>", "module:<module>", "severity:<SHALL|SHOULD|MAY>",
                  "section:<heading_slug>", "corpus-sync"]
  - metadata  : {"corpus_id": ..., "source_file": ..., "section": ..., "git_sha": ...}
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity detection
# ---------------------------------------------------------------------------

_SHALL_RE   = re.compile(r'\b(SHALL|MUST(?!\s+NOT)|REQUIRED|PROHIBITED)\b')
_MUSTNOT_RE = re.compile(r'\b(MUST\s+NOT|SHALL\s+NOT|MUST\s+NEVER|NEVER)\b')
_SHOULD_RE  = re.compile(r'\b(SHOULD(?:\s+NOT)?|RECOMMENDED|NOT\s+RECOMMENDED)\b')
_MAY_RE     = re.compile(r'\b(MAY|OPTIONAL|CAN)\b')

_DECISION_HEADING_RE = re.compile(
    r'^#{1,4}\s*(Decision|ADR|Rationale|Why|Approach|Trade.?offs?)\b',
    re.IGNORECASE,
)
_DECISION_SENTENCE_RE = re.compile(
    r'\b(We chose|Selected|Decided to|The decision is|We selected|We use)\b',
    re.IGNORECASE,
)

_API_HEADING_RE = re.compile(
    r'^#{1,4}\s*(API|Interface|Endpoint|Configuration|Contract|Schema|Model)\b',
    re.IGNORECASE,
)


def _severity(text: str) -> str | None:
    if _MUSTNOT_RE.search(text):
        return "SHALL"
    if _SHALL_RE.search(text):
        return "SHALL"
    if _SHOULD_RE.search(text):
        return "SHOULD"
    if _MAY_RE.search(text):
        return "MAY"
    return None


# ---------------------------------------------------------------------------
# Section-aware line walker
# ---------------------------------------------------------------------------

@dataclass
class _Section:
    heading: str
    level: int
    lines: list[str] = field(default_factory=list)


def _parse_sections(text: str) -> list[_Section]:
    """Split markdown text into heading-anchored sections."""
    sections: list[_Section] = [_Section(heading="__preamble__", level=0)]
    heading_re = re.compile(r'^(#{1,6})\s+(.*)')
    for line in text.splitlines():
        m = heading_re.match(line)
        if m:
            sections.append(_Section(heading=m.group(2).strip(), level=len(m.group(1))))
        else:
            sections[-1].lines.append(line)
    return sections


def _heading_slug(heading: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', heading.lower()).strip('-')


# ---------------------------------------------------------------------------
# Candidate sentence extraction
# ---------------------------------------------------------------------------

def _sentences(lines: list[str]) -> Iterator[str]:
    """Yield individual sentences from a list of markdown lines."""
    buf = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('```') or stripped.startswith('|'):
            if buf:
                yield ' '.join(buf)
                buf = []
            continue
        # Strip leading list markers
        stripped = re.sub(r'^[-*+•]\s+', '', stripped)
        stripped = re.sub(r'^\d+\.\s+', '', stripped)
        # Strip inline markdown
        stripped = re.sub(r'`[^`]+`', lambda m: m.group(0)[1:-1], stripped)
        stripped = re.sub(r'\*\*([^*]+)\*\*', r'\1', stripped)
        stripped = re.sub(r'\*([^*]+)\*', r'\1', stripped)

        if stripped:
            buf.append(stripped)
            if stripped.endswith(('.', ':', '!')):
                yield ' '.join(buf)
                buf = []
    if buf:
        yield ' '.join(buf)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ExtractedNode:
    content: str
    memory_type: str          # "constraint" | "decision" | "fact"
    severity: str             # "SHALL" | "SHOULD" | "MAY" | ""
    source_file: str
    section: str
    tags: list[str]
    metadata: dict


def extract_file(
    path: Path,
    corpus_id: str,
    namespace: str,
    module: str = "",
    git_sha: str = "",
) -> list[ExtractedNode]:
    """Parse a single markdown file and return all extracted nodes."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("corpus extractor: cannot read %s — %s", path, exc)
        return []

    rel_path = str(path)
    nodes: list[ExtractedNode] = []

    # Derive module from path if not provided
    if not module:
        parts = path.parts
        for i, p in enumerate(parts):
            if p == "modules" and i + 1 < len(parts):
                module = parts[i + 1]
                break
        if not module:
            module = path.stem

    sections = _parse_sections(text)
    is_decision_file = "decision" in path.stem.lower() or "adr" in path.stem.lower() or "rationale" in path.stem.lower()

    for section in sections:
        heading = section.heading
        slug = _heading_slug(heading)
        is_decision_section = bool(_DECISION_HEADING_RE.match(f"# {heading}"))
        is_api_section = bool(_API_HEADING_RE.match(f"# {heading}"))

        base_tags = [
            f"corpus:{corpus_id}",
            f"module:{module}",
            "corpus-sync",
        ]
        if slug != "__preamble__":
            base_tags.append(f"section:{slug}")

        base_meta = {
            "corpus_id": corpus_id,
            "source_file": rel_path,
            "section": heading,
            "git_sha": git_sha,
            "module": module,
        }

        for sentence in _sentences(section.lines):
            if len(sentence) < 20:
                continue

            severity = _severity(sentence)
            is_decision_sentence = bool(_DECISION_SENTENCE_RE.search(sentence))

            if severity:
                # Constraint node
                label = "MUST NOT" if _MUSTNOT_RE.search(sentence) else severity
                content = (
                    f"[CONSTRAINT|{label}] {sentence}\n"
                    f"Source: {rel_path} | Section: {heading}"
                )
                tags = base_tags + [f"severity:{severity}"]
                nodes.append(ExtractedNode(
                    content=content,
                    memory_type="constraint",
                    severity=severity,
                    source_file=rel_path,
                    section=heading,
                    tags=tags,
                    metadata={**base_meta, "severity": severity},
                ))

            elif is_decision_section or is_decision_file or is_decision_sentence:
                content = (
                    f"[DECISION] {sentence}\n"
                    f"Source: {rel_path} | Section: {heading}"
                )
                nodes.append(ExtractedNode(
                    content=content,
                    memory_type="decision",
                    severity="",
                    source_file=rel_path,
                    section=heading,
                    tags=base_tags + ["decision"],
                    metadata=base_meta,
                ))

            elif is_api_section and len(sentence) > 40:
                content = (
                    f"[CONTRACT] {sentence}\n"
                    f"Source: {rel_path} | Section: {heading}"
                )
                nodes.append(ExtractedNode(
                    content=content,
                    memory_type="fact",
                    severity="",
                    source_file=rel_path,
                    section=heading,
                    tags=base_tags + ["contract"],
                    metadata=base_meta,
                ))

    logger.debug(
        "corpus extractor: %s → %d nodes (module=%s)",
        rel_path, len(nodes), module,
    )
    return nodes


def extract_corpus(
    source_path: Path,
    path_pattern: str,
    corpus_id: str,
    namespace: str,
    git_sha: str = "",
) -> list[ExtractedNode]:
    """Walk source_path matching path_pattern and extract all nodes."""
    all_nodes: list[ExtractedNode] = []
    matched = list(source_path.glob(path_pattern))
    logger.info(
        "corpus extractor: scanning %s with pattern %s → %d files",
        source_path, path_pattern, len(matched),
    )
    for fpath in matched:
        if fpath.is_file():
            nodes = extract_file(fpath, corpus_id=corpus_id, namespace=namespace, git_sha=git_sha)
            all_nodes.extend(nodes)
    return all_nodes
