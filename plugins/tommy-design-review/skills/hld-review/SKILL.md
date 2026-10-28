---
name: hld-review
description: Review a high-level design (HLD): a design doc, RFC, architecture proposal or "how should we build X" plan. Use before implementation starts, or when asked to review, challenge or sign off a system design.
---

# HLD review

Review the design against the real system, not against the doc's own claims. Read the design first, then read the code and config it says it touches. A finding that cites a file or a number beats one that cites a principle.

## Steps

1. **State the goal and non-goals** in two lines. If the doc doesn't make them clear, that is finding #1.
2. **Check the boundaries.** Which components own which data? Where does each request cross a process, network or trust boundary? Name every new boundary and what authenticates it.
3. **Walk the failure modes.** For each dependency: what happens when it is slow, down, or returns garbage? Look for retries without backoff or idempotency, missing timeouts, unbounded queues, and partial failure across two writes.
4. **Check data and migration.** Schema changes, backfills, rollback path, and whether old and new versions can run side by side during a rolling deploy.
5. **Check scale and cost** with numbers: expected load, the hot path, the largest table or fan-out, and what grows without bound.
6. **Check security and privacy**: who can read or write what, where secrets live, what gets logged, tenant or namespace isolation.
7. **Check it fits the codebase**: does something equivalent already exist? Does it follow the existing layering and conventions, or does it quietly introduce a second way of doing the same thing?
8. **Check operability**: how will anyone know it works in production (metrics, alerts, logs), how is it rolled out and rolled back.

## Output

- **Verdict:** approve / approve with changes / rework.
- **Blocking findings**, ranked, each with: the claim in the design, the evidence (file, line or number), and the concrete change that fixes it.
- **Non-blocking suggestions**, short.
- **Open questions** the author must answer.
- **What you did not check** (e.g. no load numbers were available, the dependency's SLA was not read).

Do not rewrite the design. Do not pad with praise. If the design is sound, say so in one line.
