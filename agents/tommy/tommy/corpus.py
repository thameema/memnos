"""
Direct HTTP client for memnos's `/corpus/check`, `/corpus/ingest`,
`/corpus/list`, and `/corpus/check_diff` endpoints (issue #109 — Tommy's
corpus gate + auto-ingest; issue #112's `corpus_check_diff` added on top of
memnos#105's diff-verdict endpoint).

Not part of memnos-sdk's ``MemnosClient`` (sdk/memnos_sdk/client.py): that
client only wraps ``/remember``, ``/recall``, ``/consolidate``,
``/ingest/file``, ``/feedback``, and ``/secret/resolve`` — no corpus
endpoints yet. Rather than block this issue on an SDK release, Tommy talks
to these endpoints directly here, using the exact same
``httpx.Client(transport=...)`` shape ``MemnosClient`` uses, so tests can
substitute an ``httpx.MockTransport`` instead of a live server (see
sdk/tests/test_client.py for the pattern this mirrors).

Every function returns a plain dict with an ``"ok"`` flag and never raises
for network/HTTP-error conditions — callers (``mcp_server.py``'s corpus
gate, ``cli.py``'s auto-ingest) need to distinguish "the call went through"
from "the call itself failed" without a try/except at every call site, per
issue #109's fail-open-but-visible design: a corpus check (or ingest) that
could not run must be visible and distinguishable from one that ran and
found nothing — but must never raise up into a dispatch/launch path and
block it.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


def _error_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("error")
        return str(detail) if detail is not None else resp.text
    except Exception:
        return resp.text


def _post(
    memnos_url: str,
    token: Optional[str],
    path: str,
    body: dict,
    *,
    timeout: float,
    transport: Optional[httpx.BaseTransport],
) -> tuple[Optional[httpx.Response], Optional[str]]:
    """Shared POST helper. Returns (response, None) on a completed HTTP
    round-trip (any status code), or (None, error_message) if the request
    itself could not be made (DNS/connect/timeout/etc)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(
            base_url=memnos_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport,
        ) as client:
            return client.post(path, json=body), None
    except Exception as exc:
        # Deliberately broad: this module's whole contract is "never raises"
        # (fail-open-but-visible — see module docstring). httpx.HTTPError
        # covers connect/timeout/protocol errors, but a caller-supplied
        # MockTransport (tests) or an unexpected httpx internal error should
        # still degrade to a visible {"ok": False, ...} rather than crashing
        # tommy_dispatch or the CLI's auto-ingest loop.
        return None, f"{type(exc).__name__}: {exc}"


def corpus_check(
    memnos_url: str,
    token: Optional[str],
    namespace: str,
    snippet: str,
    *,
    timeout: float = 10.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST /corpus/check — constraints relevant to `snippet`.

    Returns exactly one of:
      {"ok": True,  "constraints": [...]}                — the check ran; the
                                                             list may legitimately
                                                             be empty (no matches).
      {"ok": False, "constraints": [], "error": "..."}   — the check itself
                                                             could not run
                                                             (network, auth,
                                                             malformed response).
    Never raises.
    """
    resp, err = _post(
        memnos_url, token, "/corpus/check",
        {"namespace": namespace, "snippet": snippet},
        timeout=timeout, transport=transport,
    )
    if resp is None:
        return {"ok": False, "constraints": [], "error": f"corpus check unreachable: {err}"}
    if resp.status_code >= 400:
        return {"ok": False, "constraints": [],
                "error": f"corpus check failed ({resp.status_code}): {_error_detail(resp)}"}
    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "constraints": [], "error": f"corpus check returned unparseable response: {exc}"}
    return {"ok": True, "constraints": data.get("constraints", [])}


def corpus_ingest(
    memnos_url: str,
    token: Optional[str],
    namespace: str,
    name: str,
    text: str,
    *,
    kind: str = "doc",
    git_sha: Optional[str] = None,
    timeout: float = 30.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST /corpus/ingest — parse RFC-2119 constraints out of `text` and
    store them under source `name`. WRITE_OPS endpoint (memnos_server.py's
    WRITE_OPS set) — a read-only token 403s here.

    Returns exactly one of:
      {"ok": True,  "constraints": N, "ids": [...]}
      {"ok": False, "error": "..."}
    Never raises.
    """
    body: dict[str, Any] = {"namespace": namespace, "name": name, "text": text, "kind": kind}
    if git_sha:
        body["git_sha"] = git_sha
    resp, err = _post(memnos_url, token, "/corpus/ingest", body, timeout=timeout, transport=transport)
    if resp is None:
        return {"ok": False, "error": f"corpus ingest unreachable: {err}"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"corpus ingest failed ({resp.status_code}): {_error_detail(resp)}"}
    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": f"corpus ingest returned unparseable response: {exc}"}
    return {"ok": True, "constraints": data.get("constraints", 0), "ids": data.get("ids", [])}


def corpus_check_diff(
    memnos_url: str,
    token: Optional[str],
    namespace: str,
    diff: str,
    *,
    name: Optional[str] = None,
    timeout: float = 15.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST /corpus/check_diff — memnos#105's DiffVerdict for a unified diff,
    consumed here by issue #112's ``tommy_verdict``. Unlike ``corpus_check``'s
    flat ranked list, this classifies each corpus constraint the diff is
    topically relevant to as violated / satisfied / uncovered, plus a
    ``score`` and its ``evaluated`` denominator (``len(violated) +
    len(satisfied)``) — the two are kept separate so a caller can tell a
    vacuous 1.0 (``evaluated == 0``, nothing matched) apart from a real 1.0
    (``evaluated > 0``, everything matched was satisfied); see
    core/store.py's ``corpus_check_diff`` docstring for the full reasoning,
    and ``mcp_server.py``'s ``tommy_verdict`` for why that distinction
    matters for a merge-gate decision.

    Returns exactly one of:
      {"ok": True,  "violated": [...], "satisfied": [...], "uncovered": [...],
       "score": <float>, "evaluated": <int>}
      {"ok": False, "violated": [], "satisfied": [], "uncovered": [],
       "score": None, "evaluated": 0, "error": "..."}
    Never raises. ``score`` is ``None`` (never ``1.0``) on failure — ``1.0``
    is corpus_check_diff's own legitimate vacuous-pass value on the server
    side, and must never be produced here to stand in for "the check didn't
    run."
    """
    body: dict[str, Any] = {"namespace": namespace, "diff": diff}
    if name:
        body["name"] = name
    resp, err = _post(memnos_url, token, "/corpus/check_diff", body, timeout=timeout, transport=transport)
    empty = {"violated": [], "satisfied": [], "uncovered": [], "score": None, "evaluated": 0}
    if resp is None:
        return {"ok": False, **empty, "error": f"corpus check_diff unreachable: {err}"}
    if resp.status_code >= 400:
        return {"ok": False, **empty,
                "error": f"corpus check_diff failed ({resp.status_code}): {_error_detail(resp)}"}
    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, **empty, "error": f"corpus check_diff returned unparseable response: {exc}"}
    return {
        "ok": True,
        "violated": data.get("violated", []),
        "satisfied": data.get("satisfied", []),
        "uncovered": data.get("uncovered", []),
        "score": data.get("score"),
        "evaluated": data.get("evaluated", 0),
    }


def corpus_list(
    memnos_url: str,
    token: Optional[str],
    namespace: str,
    *,
    timeout: float = 10.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict[str, Any]:
    """POST /corpus/list — current corpus sources for `namespace`, each with
    its `name` and `constraint_count`. Used by ``_auto_ingest_changed_docs()``
    to warn (not block) when a re-ingest is about to silently wipe a source's
    prior constraints down to zero (issue #109's "silent constraint wipe"
    open question).

    Returns {"ok": True, "sources": [...]} or {"ok": False, "sources": [], "error": "..."}.
    Never raises.
    """
    resp, err = _post(memnos_url, token, "/corpus/list", {"namespace": namespace},
                       timeout=timeout, transport=transport)
    if resp is None:
        return {"ok": False, "sources": [], "error": f"corpus list unreachable: {err}"}
    if resp.status_code >= 400:
        return {"ok": False, "sources": [],
                "error": f"corpus list failed ({resp.status_code}): {_error_detail(resp)}"}
    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "sources": [], "error": f"corpus list returned unparseable response: {exc}"}
    return {"ok": True, "sources": data.get("sources", [])}
