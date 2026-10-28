---
name: lld-review
description: Review a low-level design (LLD): module and class structure, interfaces, data models, schemas, API contracts, sequence of calls and error handling, before or alongside implementation. Use when asked to review an implementation plan, interface design or data model.
---

# LLD review

Review the detailed design for correctness and fit. Read the existing code around the change first so you judge against the real conventions.

## Checks

1. **Interfaces.** Are signatures minimal, typed, and stable? Does each caller get what it needs without reaching into internals? Flag leaky abstractions, boolean-flag parameters that hide two behaviours, and public surface that nobody asked for.
2. **Data model.** Keys, nullability, constraints and indexes match the queries actually run. Check migrations are additive and reversible, and that old and new code can both run during a rolling deploy.
3. **State and concurrency.** Who owns each piece of state? Look for races, double-submit, non-idempotent retries, lock ordering, and transactions that span more than they should.
4. **Error handling.** Every external call has a timeout and a defined failure result. Errors carry enough context to debug and never swallow data loss. No catch-all that hides bugs.
5. **Validation at the boundary.** Untrusted input is validated once, where it enters; internal code can then trust it.
6. **Sequence.** Walk the main flow and each error flow call by call. Name the first step where an invariant could be violated.
7. **Reuse and consistency.** Does a helper, type or pattern for this already exist? Does naming and layering match the neighbours?
8. **Testability.** Can each rule be tested without a network or clock? List the 3–5 tests that would catch the most likely bugs in this design.

## Output

- **Verdict:** approve / approve with changes / rework.
- **Findings**, ranked, each with the file or section, what breaks and when, and the smallest fix.
- **Tests to add** (the 3–5 above).
- **What you did not check.**

Cite code, not taste. If something is merely different from how you would do it, leave it out.
