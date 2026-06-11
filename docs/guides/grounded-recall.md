# Grounded recall — knowledge namespaces & links

Ground an agent's (or a team's) recall in a shared, curated knowledge base — without
copying data between namespaces and without widening anyone's permissions.

## Concepts

- **Namespace kind** — every namespace is `memory` (default, conversational) or
  `knowledge` (a curated reference corpus: architecture docs, policies, runbooks).
- **Namespace link** — a directed edge `X -> K` meaning *"recall on X should also be
  grounded in K"*. A link is **policy only**.
- **Read grant** — the caller's permission, unchanged from before. For the fan-out to
  actually happen, the calling principal must hold a **read grant on K**.

**Link = policy, grant = permission. Both are required.** A link never bypasses the ACL.

## Setup

```bash
# mark the curated corpus as a knowledge namespace (informational, shows in `namespace ls`)
memnos namespace set kb:architecture --kind knowledge

# ground recall on the project namespace in the knowledge base
memnos namespace link proj:checkout kb:architecture

# inspect / remove
memnos namespace links proj:checkout
memnos namespace unlink proj:checkout kb:architecture

# the caller still needs a read grant on the knowledge namespace:
memnos grant my-agent kb:architecture --read-only
```

## What recall returns

A `/recall` against a linked namespace fetches candidates from the primary namespace
**plus every linked namespace the caller may read**, then reranks everything in a single
pass — the best memories win regardless of where they live.

```json
{
  "memories": [
    {"content": "...", "kind": "turn", "score": 0.91},
    {"content": "...", "kind": "fact", "score": 0.87, "namespace": "kb:architecture"}
  ],
  "context": "- (said) ...\n- (fact) [kb:architecture] ...",
  "grounded_in": ["kb:architecture"],
  "links_skipped": ["kb:finance"]
}
```

- Results from a linked namespace are tagged with their source `namespace`; primary
  results are untagged.
- `grounded_in` lists the linked namespaces that contributed.
- `links_skipped` lists linked namespaces the caller has **no read grant** for — the
  skip is visible, never silent. Grant read access (or remove the link) to resolve it.
- A namespace with no links behaves exactly as before (neither key is present).

## Admin API

Link CRUD is exposed for the management console (admin token — `*` grant — required):

```
GET    /admin/api/namespaces/links[?ns=<src>]      # list links
POST   /admin/api/namespaces/links {"src": "...", "dst": "..."}
DELETE /admin/api/namespaces/links?src=...&dst=...
POST   /admin/api/namespaces/kind  {"name": "...", "kind": "knowledge"}
```

## Author attribution (related, 0.1.6)

Every memory written through the server is stamped with the **authenticated** principal's
name (`author_principal`) — taken from the bearer token, never from the request body, so
it cannot be spoofed. Recall rows include `author`, and the rendered context tags lines
`(by <author>)` only when the author differs from the caller — a single-user namespace
stays clean, while bot- or teammate-written memories in shared namespaces are visibly
attributed. You can also filter: `{"query": "...", "author": "billing-bot"}`.
