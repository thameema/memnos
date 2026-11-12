"""memnos SDK — typed REST client (sync + async) for app developers.

Lightweight (httpx only) so agentic apps can `uv pip install memnos-sdk` (or, fallback:
`pip install memnos-sdk`) without the server's heavy deps. Talks to a running memnos
server. Auth = a bearer token; every call is scoped to a namespace (set once on the
client or per call).

    from memnos_sdk import MemnosClient
    mem = MemnosClient(base_url="http://127.0.0.1:8900", token="mnk_...", namespace="org:acme")
    mem.remember("We chose Postgres for the memory store")
    print(mem.context("what db did we choose?"))   # ready-to-inject context, no LLM at query time
"""
from __future__ import annotations

_UNSET = object()   # sentinel: webhook arg omitted -> leave server-side value untouched on re-register

import httpx

DEFAULT_URL = "http://127.0.0.1:8900"


class MemnosError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"memnos {status}: {detail}")
        self.status, self.detail = status, detail


def _ns(namespace, default):
    ns = namespace or default
    if not ns:
        raise ValueError("namespace required (set MemnosClient(namespace=...) or pass namespace=)")
    return ns


def _raise(r):
    if r.status_code >= 400:
        try:
            detail = r.json().get("error", r.text)
        except Exception:
            detail = r.text
        raise MemnosError(r.status_code, detail)
    return r.json()


class MemnosClient:
    """Synchronous client. Use as a context manager or call .close()."""

    def __init__(self, base_url=DEFAULT_URL, token=None, namespace=None, timeout=120.0, transport=None):
        self.namespace = namespace
        self._h = {"Authorization": f"Bearer {token}"} if token else {}
        self._c = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=self._h,
                               transport=transport)

    def remember(self, text, *, namespace=None, speaker=None, session_id=None,
                 memory_type=None, constraint_subject=None, async_=False, extract=None) -> dict:
        """Store a turn. With async_=True the server stores the raw turn immediately and
        extracts facts in the background, returning in ~200ms — use it for capture paths on
        slow local-LLM extraction backends (Ollama/vLLM) where a synchronous call would
        otherwise block past the timeout and lose the write.

        `constraint_subject` (issues #83/#84): optional, only meaningful when
        memory_type="constraint" — see openapi.yaml's RememberRequest for the full contract.

        `extract=False` (issue #193): skip LLM fact extraction for this write — fast,
        deterministic, no model call; the raw turn is still stored and audited. Default
        (None) omits the field, same as the server's own default (True) — existing callers
        see no behavior change."""
        body = {"namespace": _ns(namespace, self.namespace), "text": text,
                "speaker": speaker, "session_id": session_id, "async": async_}
        if memory_type is not None:
            body["type"] = memory_type
        if constraint_subject is not None:
            body["constraint_subject"] = constraint_subject
        if extract is not None:
            body["extract"] = extract
        return _raise(self._c.post("/remember", json=body))

    def recall(self, query, *, namespace=None, raw_quota=None, fact_quota=None, max_chars=None,
               session_id=None, include_peers=False, session_name=None, poll_inbox=False) -> dict:
        """include_peers / session_name / poll_inbox: also return peer_directory (and inbox) in the same call.
        session_id (issue #82): optional, has no effect on retrieval/ranking — recorded on
        the server's per-constraint injection audit event for every pinned constraint this
        recall returns, so a compliance query can prove which guardrails a given agent
        session actually saw."""
        body = {"namespace": _ns(namespace, self.namespace), "query": query}
        for k, v in (("raw_quota", raw_quota), ("fact_quota", fact_quota), ("max_chars", max_chars),
                     ("session_id", session_id)):
            if v is not None:
                body[k] = v
        body.update({k: v for k, v in (("include_peers", include_peers or None), ("session_name", session_name),
                                       ("poll_inbox", poll_inbox or None)) if v})
        return _raise(self._c.post("/recall", json=body))

    def memories_by_type(self, memory_type, *, namespace=None, limit=200) -> dict:
        """Issue #193: EXACT, non-ranked enumeration of every memory tagged `memory_type`
        — NOT recall(). recall()'s near-duplicate dedupe (needed for its own job) can
        collapse several near-identical typed writes down to one result even with a
        large raw_quota; this is a plain indexed lookup for callers that need to see
        every record sharing a tag (e.g. Tommy's structured handoffs). Oldest first."""
        body = {"namespace": _ns(namespace, self.namespace), "type": memory_type, "limit": limit}
        return _raise(self._c.post("/memory/by_type", json=body))

    def ingest_file(self, filename, text, *, namespace=None, extract=False) -> dict:
        """Chunk a document's text into memory under `filename`. Pass extracted text
        (md/txt/code, or text pulled from a PDF/DOCX)."""
        return _raise(self._c.post("/ingest/file", json={
            "namespace": _ns(namespace, self.namespace), "filename": filename,
            "text": text, "extract": extract}))

    def context(self, query, *, namespace=None, **kw) -> str:
        """Just the ready-to-inject context string from recall()."""
        return self.recall(query, namespace=namespace, **kw).get("context", "")

    def consolidate(self, *, namespace=None) -> dict:
        return _raise(self._c.post("/consolidate", json={"namespace": _ns(namespace, self.namespace)}))

    def feedback(self, query, helpful, *, namespace=None, note=None) -> dict:
        return _raise(self._c.post("/feedback", json={
            "namespace": _ns(namespace, self.namespace), "query": query,
            "helpful": bool(helpful), "note": note}))

    def resolve_secret(self, name) -> str:
        """Resolve a stored secret (server-side `core/vault.py` Vault) to its DECRYPTED
        plaintext value over HTTP -- POST /secret/resolve (issue #114, "Secret Shield").
        Not namespace-scoped: the server authorizes the request against the
        pseudo-namespace `secret:<name>` (or a broader `secret:*`/`*` grant) on the
        caller's own token, via the server's normal grant machinery
        (`memnos grant add <principal> secret:<name>`).

        Raises MemnosError(403) if the token isn't authorized for this secret, or
        MemnosError(404) if no secret with that name is stored. The plaintext is
        returned exactly once per call and never logged by the server -- callers
        should inject it directly (e.g. into a subprocess environment) rather than
        printing, storing, or forwarding it into LLM-visible content."""
        return _raise(self._c.post("/secret/resolve", json={"name": name}))["value"]

    def healthy(self) -> bool:
        try:
            return self._c.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def _post_json(self, path, body, namespace, timeout=None):
        r = self._c.post(path, json={"namespace": _ns(namespace, self.namespace), **body},
                         **({"timeout": timeout} if timeout else {}))
        return _raise(r)
    def session_register(self, name, *, role='', harness='', model='', current_task='', channels=None, channel_ads=None, ttl_seconds=1800, webhook=_UNSET, namespace=None) -> dict:
        """POST /session/register (issue #231). channel_ads: {channel: description | {description, what_you_get, intents}}.
        webhook: optional http(s) URL — this session's channel/inbox events are POSTed there as
        they arrive (in addition to the existing pull via channel_wait/inbox_list), so a
        headless/service subscriber with no interactive loop can still react promptly.
        Pass None to clear an existing webhook; omit entirely to leave it (and its secret)
        untouched on re-register. The response's webhook_secret signs each delivery
        (X-Memnos-Signature: sha256=hmac); verify it before trusting a payload."""
        body = {"name": name, "role": role, "harness": harness, "model": model, "current_task": current_task, "channels": channels or [], "channel_ads": channel_ads or {}, "ttl_seconds": ttl_seconds}
        if webhook is not _UNSET:
            body["webhook"] = webhook
        return self._post_json("/session/register", body, namespace)

    def session_heartbeat(self, name, *, status=None, current_task=None, namespace=None) -> dict:
        """POST /session/heartbeat (issue #231)."""
        return self._post_json("/session/heartbeat", {k: v for k, v in {"name": name, "status": status, "current_task": current_task}.items() if v is not None}, namespace)

    def session_list(self, *, include_expired=False, namespace=None) -> dict:
        """POST /session/list (issue #231)."""
        return self._post_json("/session/list", {"include_expired": include_expired}, namespace)

    def session_unregister(self, name, *, namespace=None) -> dict:
        """POST /session/unregister (issue #231)."""
        return self._post_json("/session/unregister", {"name": name}, namespace)

    def channel_publish(self, channel, content, *, sender_session, intent='message', remember=False, metadata=None, namespace=None) -> dict:
        """POST /channel/publish (issue #231)."""
        return self._post_json("/channel/publish", {"channel": channel, "content": content, "sender_session": sender_session, "intent": intent, "remember": remember, "metadata": metadata or {}}, namespace)

    def channel_subscribe(self, channels, *, session_name, from_beginning=False, namespace=None) -> dict:
        """POST /channel/subscribe (issue #231)."""
        return self._post_json("/channel/subscribe", {"channels": channels, "session_name": session_name, "from_beginning": from_beginning}, namespace)

    def channel_unsubscribe(self, channels, *, session_name, namespace=None) -> dict:
        """POST /channel/unsubscribe (issue #231)."""
        return self._post_json("/channel/unsubscribe", {"channels": channels, "session_name": session_name}, namespace)

    def channel_poll(self, session_name, *, channel=None, peek=False, limit=50, namespace=None) -> dict:
        """POST /channel/poll (issue #231)."""
        return self._post_json("/channel/poll", {"session_name": session_name, "channel": channel or "", "peek": peek, "limit": limit}, namespace)

    def channel_wait(self, session_name, *, channel=None, timeout_seconds=30, namespace=None) -> dict:
        """POST /channel/wait (issue #231)."""
        timeout = max(1.0, float(timeout_seconds)) + 15.0
        return self._post_json("/channel/wait", {"session_name": session_name, "channel": channel or "", "timeout_seconds": timeout_seconds}, namespace, timeout=timeout)

    def channel_list(self, *, namespace=None) -> dict:
        """POST /channel/list (issue #231)."""
        return self._post_json("/channel/list", {}, namespace)

    def channel_advertise(self, channel, description, *, what_you_get='', intents=None, owner_session='', namespace=None) -> dict:
        """POST /channel/advertise (issue #231)."""
        return self._post_json("/channel/advertise", {"channel": channel, "description": description, "what_you_get": what_you_get, "intents": intents or [], "owner_session": owner_session}, namespace)

    def inbox_send(self, to_session, content, *, sender_session, subject='', intent='task', priority='normal', thread_key='', replaces_id=None, replace_pending=False, in_reply_to=None, metadata=None, namespace=None) -> dict:
        """POST /inbox/send (issue #231)."""
        body = {"to_session": to_session, "content": content, "sender_session": sender_session, "subject": subject, "intent": intent, "priority": priority, "thread_key": thread_key, "replace_pending": replace_pending, "metadata": metadata or {}}
        if replaces_id is not None:
            body["replaces_id"] = replaces_id
        if in_reply_to is not None:
            body["in_reply_to"] = in_reply_to
        return self._post_json("/inbox/send", body, namespace)

    def inbox_recall(self, *, sender_session='', message_id=None, channel_message_id=None, thread_key='', reason='', namespace=None) -> dict:
        """POST /inbox/recall (issue #231)."""
        body = {"sender_session": sender_session, "thread_key": thread_key, "reason": reason}
        if message_id is not None:
            body["message_id"] = message_id
        if channel_message_id is not None:
            body["channel_message_id"] = channel_message_id
        return self._post_json("/inbox/recall", body, namespace)

    def inbox_list(self, session_name, *, status='active', priority=None, include_recalled=False, mark_read=False, limit=50, namespace=None) -> dict:
        """POST /inbox/list (issue #231)."""
        return self._post_json("/inbox/list", {"to_session": session_name, "status": status, "priority": priority or "", "include_recalled": include_recalled, "mark_read": mark_read, "limit": limit}, namespace)

    def inbox_act(self, session_name, message_id, *, action='acted', resolution='', reply='', namespace=None) -> dict:
        """POST /inbox/act (issue #231)."""
        return self._post_json("/inbox/act", {"to_session": session_name, "message_id": message_id, "action": action, "resolution": resolution, "reply": reply}, namespace)

    def channel_cleanup(self, *, idle_days=7, message_retention_days=30, dry_run=False, namespace=None) -> dict:
        """POST /channel/cleanup (issue #231)."""
        return self._post_json("/channel/cleanup", {"idle_days": idle_days, "message_retention_days": message_retention_days, "dry_run": dry_run}, namespace)

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class AsyncMemnosClient:
    """Async client (httpx.AsyncClient). Use `async with` or call .aclose()."""

    def __init__(self, base_url=DEFAULT_URL, token=None, namespace=None, timeout=120.0, transport=None):
        self.namespace = namespace
        self._h = {"Authorization": f"Bearer {token}"} if token else {}
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, headers=self._h,
                                    transport=transport)

    async def remember(self, text, *, namespace=None, speaker=None, session_id=None,
                       memory_type=None, constraint_subject=None, async_=False) -> dict:
        """Store a turn. async_=True defers fact extraction to the server's background workers
        (returns in ~200ms) so slow local-LLM extraction can't ReadTimeout and lose the write.

        `constraint_subject` (issues #83/#84): optional, only meaningful when
        memory_type="constraint" — see openapi.yaml's RememberRequest for the full contract."""
        body = {"namespace": _ns(namespace, self.namespace), "text": text,
                "speaker": speaker, "session_id": session_id, "async": async_}
        if memory_type is not None:
            body["type"] = memory_type
        if constraint_subject is not None:
            body["constraint_subject"] = constraint_subject
        return _raise(await self._c.post("/remember", json=body))

    async def recall(self, query, *, namespace=None, raw_quota=None, fact_quota=None, max_chars=None,
                     session_id=None, include_peers=False, session_name=None, poll_inbox=False) -> dict:
        """include_peers / session_name / poll_inbox: also return peer_directory (and inbox) in the same call.
        session_id (issue #82): see MemnosClient.recall's docstring."""
        body = {"namespace": _ns(namespace, self.namespace), "query": query}
        for k, v in (("raw_quota", raw_quota), ("fact_quota", fact_quota), ("max_chars", max_chars),
                     ("session_id", session_id)):
            if v is not None:
                body[k] = v
        body.update({k: v for k, v in (("include_peers", include_peers or None), ("session_name", session_name),
                                       ("poll_inbox", poll_inbox or None)) if v})
        return _raise(await self._c.post("/recall", json=body))

    async def context(self, query, *, namespace=None, **kw) -> str:
        return (await self.recall(query, namespace=namespace, **kw)).get("context", "")

    async def consolidate(self, *, namespace=None) -> dict:
        return _raise(await self._c.post("/consolidate", json={"namespace": _ns(namespace, self.namespace)}))

    async def feedback(self, query, helpful, *, namespace=None, note=None) -> dict:
        return _raise(await self._c.post("/feedback", json={
            "namespace": _ns(namespace, self.namespace), "query": query,
            "helpful": bool(helpful), "note": note}))

    async def resolve_secret(self, name) -> str:
        """Async counterpart of MemnosClient.resolve_secret -- see its docstring."""
        return _raise(await self._c.post("/secret/resolve", json={"name": name}))["value"]

    async def _post_json(self, path, body, namespace, timeout=None):
        r = await self._c.post(path, json={"namespace": _ns(namespace, self.namespace), **body},
                         **({"timeout": timeout} if timeout else {}))
        return _raise(r)
    async def session_register(self, name, *, role='', harness='', model='', current_task='', channels=None, channel_ads=None, ttl_seconds=1800, webhook=_UNSET, namespace=None) -> dict:
        """POST /session/register (issue #231). channel_ads: {channel: description | {description, what_you_get, intents}}.
        webhook: optional http(s) URL — this session's channel/inbox events are POSTed there as
        they arrive (in addition to the existing pull via channel_wait/inbox_list).
        Pass None to clear an existing webhook; omit entirely to leave it (and its secret)
        untouched on re-register. The response's webhook_secret signs each delivery
        (X-Memnos-Signature: sha256=hmac); verify it before trusting a payload."""
        body = {"name": name, "role": role, "harness": harness, "model": model, "current_task": current_task, "channels": channels or [], "channel_ads": channel_ads or {}, "ttl_seconds": ttl_seconds}
        if webhook is not _UNSET:
            body["webhook"] = webhook
        return await self._post_json("/session/register", body, namespace)

    async def session_heartbeat(self, name, *, status=None, current_task=None, namespace=None) -> dict:
        """POST /session/heartbeat (issue #231)."""
        return await self._post_json("/session/heartbeat", {k: v for k, v in {"name": name, "status": status, "current_task": current_task}.items() if v is not None}, namespace)

    async def session_list(self, *, include_expired=False, namespace=None) -> dict:
        """POST /session/list (issue #231)."""
        return await self._post_json("/session/list", {"include_expired": include_expired}, namespace)

    async def session_unregister(self, name, *, namespace=None) -> dict:
        """POST /session/unregister (issue #231)."""
        return await self._post_json("/session/unregister", {"name": name}, namespace)

    async def channel_publish(self, channel, content, *, sender_session, intent='message', remember=False, metadata=None, namespace=None) -> dict:
        """POST /channel/publish (issue #231)."""
        return await self._post_json("/channel/publish", {"channel": channel, "content": content, "sender_session": sender_session, "intent": intent, "remember": remember, "metadata": metadata or {}}, namespace)

    async def channel_subscribe(self, channels, *, session_name, from_beginning=False, namespace=None) -> dict:
        """POST /channel/subscribe (issue #231)."""
        return await self._post_json("/channel/subscribe", {"channels": channels, "session_name": session_name, "from_beginning": from_beginning}, namespace)

    async def channel_unsubscribe(self, channels, *, session_name, namespace=None) -> dict:
        """POST /channel/unsubscribe (issue #231)."""
        return await self._post_json("/channel/unsubscribe", {"channels": channels, "session_name": session_name}, namespace)

    async def channel_poll(self, session_name, *, channel=None, peek=False, limit=50, namespace=None) -> dict:
        """POST /channel/poll (issue #231)."""
        return await self._post_json("/channel/poll", {"session_name": session_name, "channel": channel or "", "peek": peek, "limit": limit}, namespace)

    async def channel_wait(self, session_name, *, channel=None, timeout_seconds=30, namespace=None) -> dict:
        """POST /channel/wait (issue #231)."""
        timeout = max(1.0, float(timeout_seconds)) + 15.0
        return await self._post_json("/channel/wait", {"session_name": session_name, "channel": channel or "", "timeout_seconds": timeout_seconds}, namespace, timeout=timeout)

    async def channel_list(self, *, namespace=None) -> dict:
        """POST /channel/list (issue #231)."""
        return await self._post_json("/channel/list", {}, namespace)

    async def channel_advertise(self, channel, description, *, what_you_get='', intents=None, owner_session='', namespace=None) -> dict:
        """POST /channel/advertise (issue #231)."""
        return await self._post_json("/channel/advertise", {"channel": channel, "description": description, "what_you_get": what_you_get, "intents": intents or [], "owner_session": owner_session}, namespace)

    async def inbox_send(self, to_session, content, *, sender_session, subject='', intent='task', priority='normal', thread_key='', replaces_id=None, replace_pending=False, in_reply_to=None, metadata=None, namespace=None) -> dict:
        """POST /inbox/send (issue #231)."""
        body = {"to_session": to_session, "content": content, "sender_session": sender_session, "subject": subject, "intent": intent, "priority": priority, "thread_key": thread_key, "replace_pending": replace_pending, "metadata": metadata or {}}
        if replaces_id is not None:
            body["replaces_id"] = replaces_id
        if in_reply_to is not None:
            body["in_reply_to"] = in_reply_to
        return await self._post_json("/inbox/send", body, namespace)

    async def inbox_recall(self, *, sender_session='', message_id=None, channel_message_id=None, thread_key='', reason='', namespace=None) -> dict:
        """POST /inbox/recall (issue #231)."""
        body = {"sender_session": sender_session, "thread_key": thread_key, "reason": reason}
        if message_id is not None:
            body["message_id"] = message_id
        if channel_message_id is not None:
            body["channel_message_id"] = channel_message_id
        return await self._post_json("/inbox/recall", body, namespace)

    async def inbox_list(self, session_name, *, status='active', priority=None, include_recalled=False, mark_read=False, limit=50, namespace=None) -> dict:
        """POST /inbox/list (issue #231)."""
        return await self._post_json("/inbox/list", {"to_session": session_name, "status": status, "priority": priority or "", "include_recalled": include_recalled, "mark_read": mark_read, "limit": limit}, namespace)

    async def inbox_act(self, session_name, message_id, *, action='acted', resolution='', reply='', namespace=None) -> dict:
        """POST /inbox/act (issue #231)."""
        return await self._post_json("/inbox/act", {"to_session": session_name, "message_id": message_id, "action": action, "resolution": resolution, "reply": reply}, namespace)

    async def channel_cleanup(self, *, idle_days=7, message_retention_days=30, dry_run=False, namespace=None) -> dict:
        """POST /channel/cleanup (issue #231)."""
        return await self._post_json("/channel/cleanup", {"idle_days": idle_days, "message_retention_days": message_retention_days, "dry_run": dry_run}, namespace)

    async def aclose(self):
        await self._c.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.aclose()
