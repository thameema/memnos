---
name: qa-rest-api
description: QA a REST API: contract, status codes, validation, auth and authorization, pagination, idempotency and error shapes, against a running instance or an OpenAPI spec. Use when asked to test an endpoint, verify an API change, or check a service for contract drift.
---

# REST API QA

Test the API as a client sees it: real HTTP against a local or staging instance you were pointed at. Never run this against a production or third-party service you do not own.

1. **Get the contract.** Read the OpenAPI spec or the route definitions and handlers. List every endpoint, method, input and documented response.
2. **Per endpoint, check:**
   - Happy path: status, body shape, headers, content type.
   - Validation: missing, wrong-type, oversized and malformed fields each return a 4xx with a useful, consistent error body, never a 500.
   - Auth: no credential gives 401; a valid credential without access gives 403 (or 404 by policy); another tenant's or user's ID is refused, not just hidden in the UI.
   - Idempotency: repeating a PUT or DELETE is safe; a retried POST does not create duplicates if the API promises that.
   - Pagination, filtering and sorting: first page, last page, empty result, a limit above the maximum, stable order.
   - Concurrency: two simultaneous writes to the same resource.
   - Side effects: the write actually persisted and a follow-up GET shows it.
3. **Check drift:** the real response against the spec, field by field, including nullability and enum values.
4. **Use test data and test credentials only**, created for this run. Clean up what you created.
5. **Leave regression tests** in the repo's own framework (or a plain script) for each bug found.

## Report

A table of endpoint, check, expected and actual, then the failures ranked by severity with the exact request and response. State what you did not cover (rate limits, large payloads, endpoints you could not reach).
