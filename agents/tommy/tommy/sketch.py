"""
Mermaid sequence-diagram text -> Canonical Flow Corpus (CFC) constraint text
(issue #111 — the tommy-side, buildable-today half of `/sketch`).

Scope, per the issue: mermaid TEXT in (not an image — no vision/image-input
path exists anywhere in Tommy's harness discovery today, see
`discovery/harnesses.py`'s `HarnessSpec`/`HARNESS_REGISTRY`) ->
`_mermaid_to_cfc()` -> `POST /corpus/ingest`. The image->mermaid vision call
itself is a separate, not-yet-built memnos-server capability and is
explicitly out of scope here.

`_mermaid_to_cfc()` is a naive line-based parser, not a mermaid grammar. It
applies four transformations to a flat, single-level sequence diagram:

  - `participant`/`actor` declarations become the actor labels used in
    later statements (`participant C as Client` -> "Client").
  - Arrows (any of `->`, `-->`, `->>`, `-->>`, `-x`, `--x`, `-)`, `--)`,
    optionally with a `+`/`-` activation shorthand on the target) become
    `"<From> SHALL send \"<label>\" to <To>."` statements. All arrow
    variants are treated uniformly as directed messages — this is a
    deliberate v1 simplification; the sync/async/failure semantics some of
    those tokens carry in real mermaid are not distinguished in the
    generated CFC text.
  - Flat, single-level `alt`/`else` blocks become "When <condition>, ..."
    prefixes on every statement generated from arrows inside that branch.
  - `Note ...` lines (`over`/`left of`/`right of`, including `Note over`
    spanning multiple comma-separated participants) whose text contains a
    negative RFC-2119-style phrase ("must not", "shall not", "should not",
    "may not" — case-insensitive) are passed through as prohibition
    statements.

Known v1 limitations — each of these degrades PREDICTABLY (the offending
construct is skipped and reported in the returned `warnings` list) rather
than being silently mis-parsed into confident-looking but wrong CFC text:

  - Multi-line / wrapped arrow labels. A label that continues onto a second
    physical line is not recognized as a continuation — the continuation
    line falls through to "unrecognized syntax" and is skipped+warned.
  - Nested `alt`/`opt`/`loop` blocks. Only a flat, single-level `alt`/`else`
    is claimed as supported in v1. `opt` and `loop` bodies are ALWAYS
    treated as unsupported (their contents are skipped+warned, never
    emitted as an unconditional SHALL) — emitting a hard constraint for
    content that mermaid itself marked optional/repeated would be actively
    wrong, not just incomplete. A second `alt` opened while one is already
    open is likewise skipped+warned rather than merged or mis-nested.
  - Comments (`%% ...`) and diagram directives (`sequenceDiagram`,
    `autonumber`, `title ...`) are recognized and dropped cleanly — this is
    the one bullet from the issue's list of naive-parser risks that this
    implementation resolves outright rather than merely documenting, since
    doing so is no more code than getting it wrong.
  - An unrecognized line inside an open, supported `alt` block does not
    close that block — a genuinely malformed diagram (e.g. a wrapped label
    line where the real `end` got dropped/mangled) can leave later, valid
    arrow lines still carrying that block's stale "When <condition>, ..."
    prefix. The unrecognized line itself is always reported in `warnings`
    (per the bullet above), so the condition is visible, not silent — but
    it is not independently flagged as "this alt is now suspect."
  - Sequence-diagram syntax variance (`->>` vs `-->>` vs `->`,
    `activate`/`deactivate`, `Note over` spanning multiple participants) is
    also actively handled rather than left as a gap — see the arrow/note
    regexes above — but arrow *semantics* beyond "a directed message
    exists" are not modeled, per the arrow-handling note above.

`flow_name` collisions: the caller (`tommy_sketch` in mcp_server.py) passes
`flow_name` straight through as `/corpus/ingest`'s `name`. Re-ingesting
under the same name DELETE-then-replaces that source's prior constraints
(`core/store.py`'s `ingest_constraints`) — the same already-acknowledged
risk issue #109's `auto_ingest` carries for design docs (see
`cli.py`'s `_auto_ingest_changed_docs()` docstring). Nothing here guards
against it; the caller is responsible for picking a stable `flow_name` per
diagram.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# --- line patterns ----------------------------------------------------------

_COMMENT_RE = re.compile(r"^%%")
_DIRECTIVE_RE = re.compile(r"^(sequenceDiagram|autonumber\b.*|title\b.*)$", re.IGNORECASE)
_PARTICIPANT_RE = re.compile(
    r"^(?:participant|actor)\s+(?P<id>\S+?)(?:\s+as\s+(?P<alias>.+))?$", re.IGNORECASE)
_ACTIVATE_RE = re.compile(r"^(?:de)?activate\s+\S+$", re.IGNORECASE)
_BOX_RE = re.compile(r"^box\b.*$", re.IGNORECASE)
_ALT_RE = re.compile(r"^alt\b\s*(?P<cond>.*)$", re.IGNORECASE)
_ELSE_RE = re.compile(r"^else\b\s*(?P<cond>.*)$", re.IGNORECASE)
_OPT_RE = re.compile(r"^opt\b\s*(?P<label>.*)$", re.IGNORECASE)
_LOOP_RE = re.compile(r"^loop\b\s*(?P<label>.*)$", re.IGNORECASE)
_END_RE = re.compile(r"^end$", re.IGNORECASE)
_NOTE_RE = re.compile(
    r"^Note\s+(?P<pos>over|left of|right of)\s+(?P<who>[^:]+):\s*(?P<text>.+)$", re.IGNORECASE)

# Longest arrow tokens first so e.g. "-->>" isn't swallowed by "->" alone.
_ARROW_RE = re.compile(
    r"^(?P<frm>[A-Za-z0-9_.]+)\s*"
    r"(?P<arrow>-->>|--x|--\)|->>|-->|-x|-\)|->)"
    r"\s*[+-]?(?P<to>[A-Za-z0-9_.]+)[+-]?\s*"
    r":\s*(?P<label>.+)$"
)

_PROHIBITION_RE = re.compile(r"\b(must not|shall not|should not|may not)\b", re.IGNORECASE)


@dataclass
class _Block:
    kind: str              # "alt" | "opt" | "loop" | "box"
    supported: bool         # False => this block's contents are being skipped (v1 limitation)
    condition: Optional[str] = None   # current alt/else condition text (kind == "alt" only)


def _mermaid_to_cfc(mermaid_text: str, flow_name: str) -> tuple[str, list[str]]:
    """Convert mermaid sequence-diagram text into CFC constraint text.

    Returns (cfc_text, warnings):
      - cfc_text is "" when nothing extractable was found (caller should
        treat that as a failure to derive any constraints, not a valid
        empty ingest).
      - warnings lists every line/construct this naive parser could not
        confidently handle and therefore skipped, keyed by 1-based source
        line number where applicable — never silent about what was dropped.
    """
    participants: dict[str, str] = {}
    stack: list[_Block] = []
    constraint_lines: list[str] = []
    warnings: list[str] = []
    saw_header = False

    def suppressed() -> bool:
        return any(not b.supported for b in stack)

    def resolve(pid: str) -> str:
        return participants.get(pid, pid)

    def nearest_active_alt() -> Optional[_Block]:
        for b in reversed(stack):
            if b.kind == "alt" and b.supported:
                return b
        return None

    for lineno, raw in enumerate(mermaid_text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if _COMMENT_RE.match(line):
            continue
        if _DIRECTIVE_RE.match(line):
            if line.lower().startswith("sequencediagram"):
                saw_header = True
            continue

        if _END_RE.match(line):
            if stack:
                stack.pop()
            else:
                warnings.append(f"line {lineno}: 'end' with no open block — ignored")
            continue

        m = _ALT_RE.match(line)
        if m:
            if suppressed():
                stack.append(_Block(kind="alt", supported=False))
            elif any(b.kind in ("alt", "opt", "loop") for b in stack):
                warnings.append(
                    f"line {lineno}: nested 'alt' block not supported in v1 "
                    "(only flat, single-level alt/else) — contents skipped")
                stack.append(_Block(kind="alt", supported=False))
            else:
                cond = m.group("cond").strip() or "the following condition"
                stack.append(_Block(kind="alt", supported=True, condition=cond))
            continue

        m = _ELSE_RE.match(line)
        if m:
            target = next((b for b in reversed(stack) if b.kind == "alt"), None)
            if target is not None and target.supported:
                target.condition = m.group("cond").strip() or "otherwise"
            elif not suppressed():
                warnings.append(f"line {lineno}: 'else' without an open supported 'alt' — ignored")
            continue

        m = _OPT_RE.match(line)
        if m:
            was_suppressed = suppressed()
            stack.append(_Block(kind="opt", supported=False))
            if not was_suppressed:
                label = m.group("label").strip() or "(unlabeled)"
                warnings.append(
                    f"line {lineno}: 'opt {label}' block not supported in v1 "
                    "(only flat, single-level alt/else) — contents skipped")
            continue

        m = _LOOP_RE.match(line)
        if m:
            was_suppressed = suppressed()
            stack.append(_Block(kind="loop", supported=False))
            if not was_suppressed:
                label = m.group("label").strip() or "(unlabeled)"
                warnings.append(
                    f"line {lineno}: 'loop {label}' block not supported in v1 "
                    "(only flat, single-level alt/else) — contents skipped")
            continue

        if _BOX_RE.match(line):
            # A box is a purely visual grouping of participants — it never
            # gates content the way alt/opt/loop do, so it's always "supported".
            stack.append(_Block(kind="box", supported=True))
            continue

        if suppressed():
            continue  # inside an unsupported ancestor block — drop until its 'end'

        m = _PARTICIPANT_RE.match(line)
        if m:
            pid = m.group("id")
            alias = (m.group("alias") or "").strip()
            participants[pid] = alias if alias else pid
            continue

        if _ACTIVATE_RE.match(line):
            continue

        m = _NOTE_RE.match(line)
        if m:
            text = m.group("text").strip()
            if _PROHIBITION_RE.search(text):
                sentence = text if text.endswith((".", "!", "?")) else text + "."
                constraint_lines.append(sentence)
            continue

        m = _ARROW_RE.match(line)
        if m:
            label = m.group("label").strip()
            if not label:
                warnings.append(f"line {lineno}: arrow with no label — skipped")
                continue
            frm = resolve(m.group("frm"))
            to = resolve(m.group("to"))
            statement = f'{frm} SHALL send "{label}" to {to}'
            active_alt = nearest_active_alt()
            if active_alt is not None:
                statement = f"When {active_alt.condition}, {statement}"
            constraint_lines.append(statement + ".")
            continue

        warnings.append(f"line {lineno}: unrecognized syntax, skipped: {line[:80]!r}")

    if stack:
        warnings.append(
            f"{len(stack)} block(s) never closed with a matching 'end' — "
            "diagram may be truncated or malformed")
    if not saw_header:
        warnings.append(
            "no 'sequenceDiagram' header line found — _mermaid_to_cfc() only "
            "supports mermaid sequence diagrams; other diagram types will "
            "mostly yield 'unrecognized syntax' warnings")

    if not constraint_lines:
        return "", warnings

    cfc_text = "\n".join([f"# Canonical Flow Corpus — {flow_name}", ""] + constraint_lines)
    return cfc_text, warnings
