"""memnos-sdk Omnigent capture-policy tests.

Unit/contract tests for `memnos_sdk.integrations.omnigent` — Omnigent's function-policy
handler that captures assistant responses into memnos. Uses an httpx MockTransport (no
server needed) for the write path, and asserts the handler's core safety contract: it
must NEVER raise (Omnigent's engine converts any exception from a function-policy
callable into a fail-closed DENY on the live assistant response — see
omnigent/runtime/policies/engine.py's `_run_policy_safely`), and must always return an
ALLOW verdict regardless of whether the underlying memnos write succeeds.

The REAL end-to-end proof (a real event -> real memnos server -> fact recallable) lives
in tests/test_omnigent_capture_live.py at the repo root, run against a live server —
see that file's docstring for why unit tests alone can't prove that.

Run: python sdk/tests/test_omnigent_integration.py
"""
import inspect
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from memnos_sdk.integrations import omnigent as capture_mod

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def _event(text="The build is fixed by pinning webpack to 5.90.", actor=None):
    return {
        "type": "response",
        "target": None,
        "data": text,
        "context": {"actor": actor or {}, "usage": {}, "user_daily_cost": {}, "model": None,
                    "harness": None, "labels": {}, "subtree_usage": {}},
        "session_state": {},
        "llm_client": None,
    }


def main():
    print("=== memnos_sdk.integrations.omnigent ===")

    # --- contract: signature shape Omnigent's FunctionPolicy engine relies on ---
    # `_callable_arity` counts POSITIONAL_ONLY/POSITIONAL_OR_KEYWORD params (defaults
    # don't matter to the count); `_has_no_required_params` only kicks in for the
    # short-string `handler:` form and checks whether every positional param has a
    # default. `event` must NOT have one (else Omnigent would treat this as a
    # zero-arg factory to call once at startup, not a per-turn evaluator).
    sig = inspect.signature(capture_mod.capture_response)
    params = list(sig.parameters.values())
    check("capture_response(event, config) — exactly 2 positional params",
          len(params) == 2 and params[0].name == "event" and params[1].name == "config")
    check("event has NO default (so Omnigent doesn't mistake this for a factory)",
          params[0].default is inspect.Parameter.empty)
    check("config HAS a default (so `handler: <path>` short form still works)",
          params[1].default is not inspect.Parameter.empty)

    # --- resolution precedence: config: block > env > built-in default ---
    os.environ.pop("MEMNOS_URL", None)
    os.environ.pop("MEMNOS_NS", None)
    os.environ.pop("MEMNOS_TOKEN", None)
    url, token, ns, timeout_s = capture_mod._resolve(None)
    check("no config/env -> built-in defaults", (url, ns, timeout_s) ==
          (capture_mod.DEFAULT_URL, capture_mod.DEFAULT_NAMESPACE, capture_mod.DEFAULT_TIMEOUT_S))
    check("no MEMNOS_TOKEN -> token is None", token is None)

    os.environ["MEMNOS_URL"] = "https://memnos.example.internal"
    os.environ["MEMNOS_NS"] = "agent:omnigent-prod"
    os.environ["MEMNOS_TOKEN"] = "mnk_env_token"
    url, token, ns, timeout_s = capture_mod._resolve(None)
    check("env vars fill in when config: is absent",
          (url, token, ns) == ("https://memnos.example.internal", "mnk_env_token", "agent:omnigent-prod"))

    url, token, ns, timeout_s = capture_mod._resolve({
        "memnos_url": "http://127.0.0.1:9999", "memnos_namespace": "agent:custom", "memnos_timeout_s": 3,
    })
    check("config: block overrides env for url/namespace/timeout",
          (url, ns, timeout_s) == ("http://127.0.0.1:9999", "agent:custom", 3.0))
    check("token is NEVER read from config: (env-only, secrets stay out of the YAML)",
          capture_mod._resolve({"memnos_token": "mnk_should_be_ignored"})[1] == "mnk_env_token")

    _, _, _, bad_timeout = capture_mod._resolve({"memnos_timeout_s": "not-a-number"})
    check("non-numeric memnos_timeout_s falls back to default (never raises)",
          bad_timeout == capture_mod.DEFAULT_TIMEOUT_S)

    for k in ("MEMNOS_URL", "MEMNOS_NS", "MEMNOS_TOKEN"):
        os.environ.pop(k, None)

    # --- actor label extraction ---
    check("actor.run_as preferred", capture_mod._extract_actor_label(
        _event(actor={"run_as": "alice", "client_id": "c1"})) == "alice")
    check("actor.client_id fallback", capture_mod._extract_actor_label(
        _event(actor={"client_id": "c1"})) == "c1")
    check("no actor -> None (never raises)", capture_mod._extract_actor_label(_event(actor={})) is None)
    check("malformed event -> None (never raises)", capture_mod._extract_actor_label({}) is None)

    # --- the write path, via MockTransport (real MemnosClient, fake network) ---
    calls = []

    def handler(request):
        body = json.loads(request.content or b"{}")
        calls.append((request.url.path, body, dict(request.headers)))
        return httpx.Response(200, json={"turn_id": 1, "extraction": "queued"})

    capture_mod._do_remember(
        "captured assistant text", url="http://test", token="mnk_x", namespace="agent:omnigent",
        timeout_s=5.0, actor_label="alice", transport=httpx.MockTransport(handler),
    )
    check("_do_remember hit POST /remember exactly once", len(calls) == 1 and calls[0][0] == "/remember")
    if calls:
        path, body, headers = calls[0]
        check("body: correct namespace + text + speaker=assistant + async=true",
              body.get("namespace") == "agent:omnigent" and body.get("text") == "captured assistant text"
              and body.get("speaker") == "assistant" and body.get("async") is True)
        check("body: session_id carries the actor label", body.get("session_id") == "alice")
        check("Authorization header carries the bearer token", headers.get("authorization") == "Bearer mnk_x")

    # a write that 5xxs must not raise out of _do_remember
    def err_handler(request):
        return httpx.Response(500, json={"error": "boom"})
    try:
        capture_mod._do_remember("x", url="http://test", token="t", namespace="ns", timeout_s=5.0,
                                 actor_label=None, transport=httpx.MockTransport(err_handler))
        check("_do_remember swallows a 5xx from memnos", True)
    except Exception:
        check("_do_remember swallows a 5xx from memnos", False)

    # a transport that raises (simulated connection failure) must not raise either
    def raising_transport(request):
        raise httpx.ConnectError("connection refused", request=request)
    try:
        capture_mod._do_remember("x", url="http://test", token="t", namespace="ns", timeout_s=5.0,
                                 actor_label=None, transport=httpx.MockTransport(raising_transport))
        check("_do_remember swallows a connection error", True)
    except Exception:
        check("_do_remember swallows a connection error", False)

    # --- capture_response: the real entry point, real background thread, real (bad) network ---
    # Point at a closed local port so the connection fails fast without external I/O,
    # proving the handler returns immediately and never raises even on total failure.
    os.environ["MEMNOS_TOKEN"] = "mnk_x"
    t0 = time.perf_counter()
    result = capture_mod.capture_response(_event("assistant turn text"),
                                          {"memnos_url": "http://127.0.0.1:1", "memnos_namespace": "agent:omnigent"})
    dt = time.perf_counter() - t0
    check("capture_response returns ALLOW", result == {"result": "allow"})
    check("capture_response returns near-instantly (fire-and-forget, not blocking on the network)",
          dt < 0.5)
    thread = capture_mod._last_capture_thread
    check("a background thread was spawned", thread is not None and thread.name == "memnos-omnigent-capture")
    if thread is not None:
        thread.join(timeout=5)
        check("background thread finishes on its own (didn't hang or crash the process)",
              not thread.is_alive())
    os.environ.pop("MEMNOS_TOKEN", None)

    # --- malformed / empty events never raise and never spawn a thread ---
    capture_mod._last_capture_thread = None
    for bad_event in ({}, {"data": None}, {"data": ""}, {"data": "   "}, {"data": 42}):
        try:
            r = capture_mod.capture_response(bad_event, {})
            ok = r == {"result": "allow"}
        except Exception:
            ok = False
        check(f"malformed event {bad_event!r} -> ALLOW, no raise", ok)
    check("no thread spawned for any malformed event", capture_mod._last_capture_thread is None)

    # config=None (the short `handler: <path>` YAML form never supplies a config block).
    # Point MEMNOS_URL at a closed local port so this never depends on — or touches —
    # whatever memnos server (if any) happens to be reachable at the built-in default.
    os.environ["MEMNOS_URL"] = "http://127.0.0.1:1"
    try:
        r = capture_mod.capture_response(_event(), None)
        check("config=None works (short handler: form)", r == {"result": "allow"})
    except Exception:
        check("config=None works (short handler: form)", False)
    if capture_mod._last_capture_thread is not None:
        capture_mod._last_capture_thread.join(timeout=5)
    os.environ.pop("MEMNOS_URL", None)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
