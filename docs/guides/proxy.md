# `memnos proxy` — deterministic conversation capture for any base-URL-configurable client

The MCP protocol has no "after each response" event, so MCP-based memory tools can only
*ask* the model to save memories — capture is at the model's discretion. The memnos proxy
closes that gap for every client that lets you override its LLM endpoint: it sits between
your agent and the provider, relays every request untouched, and **captures both sides of
each completed exchange — the user's message and the model's reply — into memnos
automatically**. No hooks, no cooperation from the agent, no missed answers.

```
agent / IDE / assistant
   │   base URL → http://127.0.0.1:8910
   ▼
memnos proxy ──────────► api.openai.com / api.anthropic.com (or OpenRouter, Ollama, …)
   │  transparent relay (streaming included), keys forwarded verbatim — never stored
   └─► user + assistant turns → memnos /remember (audited `proxy` principal, redacted,
       namespace-scoped, fact-extracted like every other write)
```

## Start it

```bash
memnos proxy                  # foreground, http://127.0.0.1:8910
memnos proxy --namespace proj:myapp --port 8911
memnos proxy --no-capture    # relay only (useful for testing a client against it)
```

On first run it mints a dedicated **`proxy` service principal** — capture is a first-class
authenticated identity: visible in the audit log, ACL-scoped, instantly revocable.

## Point your client at it

> **Rule of thumb: Claude Code should use HOOKS, not the proxy.** Hooks are equally
> deterministic, capture both speakers, and run *off* the request path — a hook failure can
> never delay or break your session, while a proxy sits between you and the model. Use the
> proxy for clients that have **no hooks** (Hermes, OpenClaw, Open WebUI, SDK apps). Never
> run both on the same client — that double-captures.

| Client | How | Wire format | Capture |
|---|---|---|---|
| **Claude Code** | prefer **hooks** (`memnos agent-setup claude-code`); proxy possible via `ANTHROPIC_BASE_URL` but not recommended | Anthropic | ✅ via hooks |
| **Hermes Agent** | `custom_providers:` entry with `base_url: http://127.0.0.1:8910/v1` | OpenAI | ✅ |
| **OpenClaw** | `models.providers` → `"baseUrl": "http://127.0.0.1:8910/v1"` | OpenAI | ✅ |
| **Open WebUI** | `OPENAI_API_BASE_URL=http://127.0.0.1:8910/v1` | OpenAI | ✅ |
| **Any SDK app** | `OpenAI(base_url=...)` / `Anthropic(base_url=...)` | both | ✅ |
| **Cursor** | Settings → Override OpenAI Base URL | OpenAI | ⚠ chat panel only — Tab/backend features bypass it |
| **Codex CLI** | — | Responses API | ❌ not yet (`/v1/responses` capture is planned) |
| **Claude Desktop** | — | — | ❌ **impossible** — no endpoint override exists; use the MCP tools tier |

Your provider API key keeps working unchanged — the client sends it as usual and the proxy
**forwards `Authorization`/`x-api-key` verbatim to the upstream. Keys are never stored,
never logged.** The proxy binds `127.0.0.1` only.

## What gets captured (and what deliberately doesn't)

Agentic tools make many internal LLM calls per user turn — tool-call loops, title
generation, summarizers. Capturing it all would drown your memory in noise (industry
measurements put 60–70% of agent-log tokens in this bucket). The proxy applies a gate
chain and keeps only the user-facing exchange:

1. **Terminal responses only** — `finish_reason: stop` / `stop_reason: end_turn`, with no
   tool calls. Intermediate tool-loop iterations are skipped; the final answer (which
   contains the conclusion) is kept.
2. **Human turns only** — the request's last message must be plain user text (a
   `tool_result` follow-up is an agent-loop step, not a person).
3. **Background models dropped** — configurable denylist (default `*haiku*` — Claude
   Code's title/topic calls) and `max_tokens < 512` requests.
4. **Dedupe** — agents resend the whole conversation each call; only the trailing new
   exchange is captured, with a hash LRU as a second layer.

Captured turns flow through the **normal `/remember` pipeline**: secret redaction first,
verbatim raw turn + LLM fact extraction, bi-temporal supersession — identical treatment to
the Claude Code hooks.

## Config

`~/.memnos/config.json`:

```json
{
  "proxy": {
    "port": 8910,
    "capture": true,
    "capture_model_denylist": ["*haiku*"],
    "capture_min_max_tokens": 512,
    "upstreams": {
      "openai": "https://api.openai.com",
      "anthropic": "https://api.anthropic.com"
    }
  }
}
```

- Point `upstreams.openai` at OpenRouter / Ollama / NVIDIA NIM to capture against any
  OpenAI-compatible backend.
- Per-request namespace: send an `x-memnos-namespace: proj:foo` header (the proxy strips
  it before forwarding upstream). Default namespace: `user:<you>` or `--namespace`.
- `GET /healthz` shows capture stats (`captured` / `skipped` / `errors`).

## Knowing it's working — no blind spots

A proxy in the request path is a critical dependency, so memnos makes its state visible
everywhere:

- **`memnos status`** shows the proxy: running or not, plus live counters
  (`captured / skipped / errors`) and the last capture error.
- **Claude Code sessions show a status line at start** (SessionStart hook, wired by
  `agent-setup claude-code`): `memnos: memory ACTIVE → user:you · capture proxy ACTIVE` —
  or a loud `⚠ memory OFF` warning with the fix. You never silently lose memory after a
  reboot.
- **Survive reboots:** `memnos autostart --proxy` installs login services for BOTH the
  server and the proxy (launchd/systemd) — clients pointed at the proxy keep working after
  every restart without you thinking about it.
- **Unambiguous errors.** If a request fails, the error body tells you exactly who failed:
  - LLM provider errors (bad key, rate limit, model) are **relayed verbatim** with the
    provider's own status and body — if you see those, it's the LLM, not the proxy.
  - Network/proxy failures return a distinct shape with `"source": "memnos-proxy"` and a
    typed reason: `upstream_connect_timeout` (no answer in 15s — firewall/DNS/offline),
    `upstream_unreachable` (connection refused), `upstream_read_timeout` (provider went
    silent), or `proxy_error` (our bug). Each message states it is **not** an LLM error and
    how to bypass the proxy if needed.

## Reliability & limits — read this

- **Fail-open by design:** if memnos is down or capture errors, the relay continues
  untouched. Memory can degrade — visibly, via `memnos status` and the session status
  line — but your agent never breaks. For autonomous agents (Hermes, OpenClaw) this is the
  contract that matters: the proxy never modifies a response, never injects content, and
  never converts a capture problem into an agent failure.
- **Added latency is one local hop** (~ms). Streaming is relayed chunk-by-chunk before any
  parsing happens.
- The gates are heuristics: rare agent shapes (a final answer that legitimately ends with
  a tool call, synthesized "user" messages) can be missed or over-captured. Everything
  captured is namespace-scoped to the revocable `proxy` principal, so cleanup is one
  `memnos namespace rm`.
- **This is not "universal":** Claude Desktop cannot be captured this way (no base-URL
  override — that's a vendor decision, true for every memory product). Codex needs the
  Responses API (planned). For those, the MCP `remember` tool remains the path, and it is
  model-discretionary — we say this plainly rather than overclaim.
