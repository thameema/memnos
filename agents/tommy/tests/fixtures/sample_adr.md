# ADR-0001: Widget Loader Data Access

Status: Accepted

## Decision

- All widget loader queries MUST go through the repository layer; direct SQL from route handlers is PROHIBITED.
- Widget cache entries SHALL be invalidated within 60 seconds of a write.
- Loader implementations SHOULD emit a structured log line for every batch fetch.
- This paragraph is plain narration describing the history of the widget loader and contains no normative keyword, so an extractor keyed on RFC-2119 language will pass over it.

## Consequences

- New loaders MUST NOT bypass the repository layer's read cache.
