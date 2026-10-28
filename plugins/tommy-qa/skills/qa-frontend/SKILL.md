---
name: qa-frontend
description: QA a web frontend: build a test plan from the real UI and run it in a real browser, including click paths, mixed states, forms, errors, responsive layouts and accessibility. Use when asked to test, verify or QA a page, flow or UI change.
---

# Frontend QA

Test what a user can do, in a real browser, against the running app. A green unit test is not evidence the UI works.

1. **Find what changed** (diff, ticket, or the page named) and list the user-visible flows it touches.
2. **Plan.** For each flow: the happy path, empty state, loading state, error state, validation failures, double-click and back-button, long text, and a narrow phone width. For forms, test every field's invalid input, not just one.
3. **Run it.** Use the project's existing browser-test setup (Playwright or Cypress config in the repo) if there is one; otherwise drive the app with the browser tools available. Click through the real controls. Do not call internals to fake state when a real click path exists.
4. **Look at it.** Take a screenshot at each step that matters and read the console and failed network requests. A page that renders but logs errors is not passing.
5. **Accessibility pass:** keyboard-only through the flow, visible focus, labels on inputs, contrast on new UI.
6. **Leave a test behind** for each bug found, in the repo's existing test framework, so it cannot silently regress.

## Report

Per flow: pass or fail, with the step, what you saw, and a screenshot or console line as evidence. List bugs ranked by user impact. End with what you did not test (browsers, locales, data volumes). Claim "works" only for what you actually clicked through.

Test only the local or staging app you were pointed at. Do not use real customer data or real payment details; use the project's seed data or test values.
