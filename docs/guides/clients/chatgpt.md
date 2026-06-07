# Connecting memnos to ChatGPT

The open-source memnos server exposes a **REST API** (no hosted MCP-over-HTTP endpoint), so
the way to use it from ChatGPT is a **custom GPT Action** against that REST API.

## Requirement: a public URL

memnos binds `127.0.0.1:8900`. ChatGPT needs a publicly reachable URL — expose it with a
tunnel (and put auth in front for anything beyond local testing):

```bash
ngrok http 8900        # or: cloudflared tunnel --url http://localhost:8900
```

Copy the forwarding URL (e.g. `https://abc123.ngrok-free.app`).

## Create a custom GPT Action

In **ChatGPT → Create a GPT → Configure → Actions → Create new action**, paste this schema
(swap in your tunnel URL):

```yaml
openapi: 3.1.0
info: { title: memnos Memory API, version: "1.0.0" }
servers:
  - url: https://abc123.ngrok-free.app
paths:
  /recall:
    post:
      operationId: recall
      summary: Recall ranked memories + a context block for a query
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [namespace, query]
              properties:
                namespace: { type: string }
                query: { type: string }
      responses: { "200": { description: ranked memories + context } }
  /remember:
    post:
      operationId: remember
      summary: Store a message (raw turn + extracted bi-temporal facts)
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [namespace, text]
              properties:
                namespace: { type: string }
                text: { type: string }
      responses: { "200": { description: stored } }
```

## Authentication

Under **Authentication** choose **API Key → Bearer**, and paste a memnos token
(`memnos token alice` → `mnk_…`). The token is sent as `Authorization: Bearer mnk_…` and
clamps every call to that token's granted namespaces.

## Use it

Tell the GPT: *"Use recall on namespace `user:alice` to find what I decided about X"* or
*"remember this in `user:alice`: …"*. Recall runs with **no LLM at query time**; the GPT
reasons over the returned context.

> **Limitations:** ChatGPT only calls the Action when asked — there is no automatic
> inject-on-every-prompt (that's available for Claude Code via hooks). Never expose your
> tunnel publicly without auth + TLS.
