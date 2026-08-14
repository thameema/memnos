"""memnos capture policy for Omnigent (github.com/omnigent-ai/omnigent).

Wires memnos into an Omnigent SERVER (not a single agent) as a `type: function`
policy — Omnigent's own extension point for server-wide guardrails, declared in the
server's `--config` YAML under a top-level `policies:` mapping and resolved via a
dotted Python import path (see omnigent/policies/function.py `resolve_function_policy`
+ `_resolve_dotted_path`, and omnigent/spec/parser.py `parse_default_policies`). No
Omnigent source changes are involved — this file just needs to be importable in
whatever Python environment the `omnigent server` process runs in:

    uv pip install memnos-sdk        # (or: pip install memnos-sdk, same environment)

Generate the YAML wiring with `memnos server-setup omnigent` (see memnos_cli.py) rather
than hand-writing it. The generated `handler:` value points at `capture_response` below.

What this does: every time an Omnigent-orchestrated agent finishes a turn and its
assistant message is persisted through the normal Agent Platform events API (the
`type: "message", role: "assistant"` path — see omnigent/server/routes/sessions/
routes_events.py, the branch that calls `_evaluate_output_policy`), Omnigent's policy
engine calls `capture_response(event, config)` with the full, untruncated assistant
text. This function fires a best-effort write to memnos's `/remember` endpoint and
returns immediately — it never blocks, and never fails, the turn it's observing.

What this does NOT do: Omnigent's separate "external assistant message" event type
(`external_assistant_message`) — used to mirror text from a process running OUTSIDE
any Omnigent task (e.g. a raw external terminal session Omnigent is just displaying) —
bypasses `_evaluate_output_policy` entirely (see
`_persist_external_assistant_message`'s own docstring: "intentionally bypasses the
legacy persist path"). Those turns are NOT captured. Nor is this recall/injection —
memnos never re-enters the conversation; see docs/integrations/omnigent.md.

Contract with Omnigent's FunctionPolicy engine (omnigent/policies/function.py):
  - `event` is a dict: {"type": "response", "target": None, "data": <str>,
    "context": {"actor": {...}, ...}, "session_state": {...}, "llm_client": ...}.
    `data` is the raw assistant text; `context.actor` is {"run_as": ..., "client_id":
    ...} when the caller's identity is known, else {}.
  - `config` is the policy's YAML `config:` block (str-keyed dict), or {} if omitted.
  - The callable's return value becomes a PolicyResult. Any exception it raises is
    caught by the engine and converted to a fail-closed DENY (POLICIES.md §4) — which
    would replace the user-visible assistant response with a deny sentinel. A memory
    side-channel must never be able to do that, so every code path below is wrapped and
    the function unconditionally returns an ALLOW dict.

Latency: because `_evaluate_output_policy` is awaited before the assistant message is
persisted, whatever this function does runs in the response path. To keep that
overhead at effectively zero regardless of memnos's own latency or reachability, the
actual HTTP call is dispatched onto a short-lived daemon thread and this function
returns immediately — the same "never block the agent turn" approach memnos already
uses for Hermes's native memory plugin (integrations/hermes/__init__.py).
"""
from __future__ import annotations

import logging
import os
import threading

from ..client import MemnosClient

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8900"
DEFAULT_NAMESPACE = "agent:omnigent"
DEFAULT_TIMEOUT_S = 8.0

# Kept so a synchronous caller (tests) can wait for the fire-and-forget write to land
# without adding a `blocking:` config knob that production never exercises.
_last_capture_thread: threading.Thread | None = None


def _resolve(config: dict | None):
    """Resolve (url, token, namespace, timeout_s) from config: block + env vars.

    Precedence: policy `config:` YAML > environment > built-in default. The bearer
    token is READ FROM THE ENVIRONMENT ONLY (`MEMNOS_TOKEN`) — never accepted from the
    YAML config block, since the server's `--config` file is operator-editable and
    often world-readable (same posture memnos's own server_config.py documents for its
    hosted entrypoints). `memnos server-setup omnigent` never writes a token into the
    YAML for this reason; it prints an `export MEMNOS_TOKEN=...` instruction instead.
    """
    cfg = config or {}
    url = str(cfg.get("memnos_url") or os.environ.get("MEMNOS_URL") or DEFAULT_URL)
    namespace = str(cfg.get("memnos_namespace") or os.environ.get("MEMNOS_NS") or DEFAULT_NAMESPACE)
    token = os.environ.get("MEMNOS_TOKEN")
    raw_timeout = cfg.get("memnos_timeout_s")
    try:
        timeout_s = float(raw_timeout) if raw_timeout is not None else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        logger.warning("omnigent capture: config.memnos_timeout_s=%r is not a number — using %ss",
                       raw_timeout, DEFAULT_TIMEOUT_S)
        timeout_s = DEFAULT_TIMEOUT_S
    return url, token, namespace, timeout_s


def _extract_actor_label(event: dict) -> str | None:
    """Best-effort caller identity for `speaker`/attribution — never raises."""
    try:
        actor = (event.get("context") or {}).get("actor") or {}
        return actor.get("run_as") or actor.get("client_id") or None
    except Exception:
        return None


def _do_remember(text: str, *, url: str, token: str | None, namespace: str, timeout_s: float,
                 actor_label: str | None, transport=None) -> None:
    """The actual memnos write. Runs on a background thread — swallow everything.

    :param transport: Test-only httpx transport override (e.g. ``httpx.MockTransport``)
        forwarded to :class:`MemnosClient`. Never set by :func:`capture_response` itself
        — production calls always leave this ``None`` and hit the real network.
    """
    client = None
    try:
        client = MemnosClient(base_url=url, token=token, namespace=namespace, timeout=timeout_s,
                              transport=transport)
        client.remember(text, speaker="assistant", session_id=actor_label, async_=True)
        logger.debug("omnigent capture: stored assistant turn in namespace %r", namespace)
    except Exception as exc:
        logger.warning("omnigent capture: memnos write failed (namespace=%r url=%r): %s",
                       namespace, url, exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def capture_response(event: dict, config: dict | None = None) -> dict:
    """Omnigent `type: function` policy handler — capture the assistant's response.

    Signature matches what `omnigent/policies/function.py::FunctionPolicy._call` sends
    a 2-positional-parameter callable: `(event, config)`, dispatched via
    `asyncio.to_thread` since this is a plain sync function.

    Always returns an ALLOW verdict — this policy only observes; it never blocks,
    denies, or asks. Every failure mode (bad event shape, unreachable memnos server,
    disabled/misconfigured token) is caught here and logged, never raised, per the
    fail-closed-on-exception contract in POLICIES.md §4 (function.py:130-141).

    :param event: The RESPONSE-phase event built by `_build_event()` — `event["data"]`
        is the full assistant text.
    :param config: The policy's YAML `config:` block, or `None`/`{}`.
    :returns: `{"result": "allow"}` unconditionally.
    """
    global _last_capture_thread
    try:
        text = event.get("data")
        if not isinstance(text, str) or not text.strip():
            return {"result": "allow"}
        url, token, namespace, timeout_s = _resolve(config)
        actor_label = _extract_actor_label(event)
        thread = threading.Thread(
            target=_do_remember,
            kwargs=dict(text=text, url=url, token=token, namespace=namespace,
                       timeout_s=timeout_s, actor_label=actor_label),
            name="memnos-omnigent-capture",
            daemon=True,
        )
        thread.start()
        _last_capture_thread = thread
    except Exception as exc:
        logger.warning("omnigent capture: handler failed before dispatch: %s", exc)
    return {"result": "allow"}
