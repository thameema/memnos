# Layer 3 — real-Docker E2E tests

These tests bring up an **actual engram stack** (no mocks) and exercise the
real API + MCP endpoints. They are slow on first run (3-5 min to build the
engram image) and require Docker on the host.

Isolation from any existing dev install:
- Project name: `engram-e2e` (not `engram`)
- Ports: 18765 / 18766 / 12480
- Data dir: `/tmp/engram-e2e-data`
- Source clone: `/tmp/engram-e2e-src`

Run:
```bash
bash tests/install/run.sh e2e
```

The first test that needs the stack calls `e2e_up` which builds + starts it.
Subsequent tests in the same run reuse the existing healthy stack.

To tear it down manually:
```bash
( cd /tmp/engram-e2e-src && \
  docker compose --project-name engram-e2e \
    --env-file /tmp/engram-e2e-data/.env down -v )
rm -rf /tmp/engram-e2e-{src,data}
```
