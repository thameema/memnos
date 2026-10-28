---
name: qa-backend
description: QA backend logic: design and run unit, integration and edge-case tests for services, jobs, database code and business rules. Use when asked to test a backend change, find bugs in a service, or raise test coverage where it matters.
---

# Backend QA

Aim tests at the rules that cost money or data when they are wrong, not at coverage numbers.

1. **Read the change and its callers.** List the rules it must enforce and the invariants it must not break.
2. **Choose the level.** Pure logic gets unit tests. Anything involving the database, queue or transaction gets an integration test against the real engine the project uses, not a mock of it.
3. **Cases to cover for each rule:** the normal case, boundaries (0, 1, max, empty, null), duplicates and retries (is it idempotent?), concurrent callers, ordering, time and timezone edges, and partial failure (the second write fails after the first succeeded).
4. **Money, auth and deletion paths** get the most cases: wrong user, wrong tenant, expired or revoked credentials, double charge, undo.
5. **Write the failing test first** when you suspect a bug, watch it fail for the right reason, then report it or fix it as asked.
6. **Run the project's existing suite** with its own command and report real output. Do not mark anything passing that you did not run.

## Report

Tests added (file and what each proves), bugs found with the failing input, and anything you could not test (no test database, external service, flaky timing). Keep tests deterministic: no sleeps, no network, no shared state between tests.
