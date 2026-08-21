# sketch

Convert a mermaid sequence diagram into Canonical Flow Corpus (CFC)
constraints and ingest them into memnos's architecture corpus, via Tommy's
`tommy_sketch` MCP tool.

Unlike the `memnos-*` slash commands in this directory, which call memnos's
own MCP tools directly, this command calls `tommy_sketch` — a
Tommy-specific tool, only available when Tommy's own MCP server (`tommy
--mcp`) is registered in this editor, not just memnos's.

Mermaid TEXT in, not an image — there is no image->mermaid step here (see
issue #111). If you only have a picture of a diagram, transcribe it to
mermaid sequence-diagram syntax yourself first, then run this command
against the transcription.

## Usage
```
/sketch <flow_name>
```
followed by the mermaid source, either:
- pasted as a fenced ` ```mermaid ` code block earlier in this conversation, or
- referenced as a file path after `<flow_name>` (e.g. `/sketch checkout-flow docs/diagrams/checkout.mmd`).

## What to do
1. Determine `flow_name` from `$ARGUMENTS`'s first token.
2. Locate the mermaid source:
   - If `$ARGUMENTS` has a second token that resolves to an existing file, read it.
   - Otherwise use the most recent ```mermaid fenced code block in this conversation.
   - If neither is available, ask the user to paste the mermaid diagram or point to a file.
3. Call:
```python
tommy_sketch(
    flow_name="<flow_name>",
    mermaid_text="<the mermaid source, if pasted inline>",
    # or: mermaid_file="<path>",
)
```
4. Report back to the user:
   - How many constraints were ingested (`result["constraints"]`), or the
     error if `result["ok"]` is False (e.g. a read-only memnos token 403s
     here — `/corpus/ingest` is a write-authenticated endpoint).
   - Every entry in `result["warnings"]`, verbatim — these are lines or
     blocks (nested `alt`/`opt`/`loop`, wrapped labels, unrecognized
     syntax) the parser could not confidently convert and skipped, not
     cosmetic noise.
   - If `flow_name` already had constraints ingested from a previous
     `/sketch` or doc ingest, note that this run replaced them (re-ingesting
     under the same name deletes-then-replaces that source's prior
     constraints).
