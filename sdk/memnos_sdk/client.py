"""memnos SDK — typed REST client (sync + async) for app developers.

Lightweight (httpx only) so agentic apps can `pip install memnos-sdk` without the server's
heavy deps. Talks to a running memnos server. Auth = a bearer token; every call is scoped
to a namespace (set once on the client or per call).

    from memnos_sdk import MemnosClient
    mem = MemnosClient(base_url="http://127.0.0.1:8900", token="mnk_...", namespace="org:acme")
    mem.remember("We chose Postgres for the memory store")
    print(mem.context("what db did we choose?"))   # ready-to-inject context, no LLM at query time
"""
from __future__ import annotations

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
                 async_=False) -> dict:
        """Store a turn. With async_=True the server stores the raw turn immediately and
        extracts facts in the background, returning in ~200ms — use it for capture paths on
        slow local-LLM extraction backends (Ollama/vLLM) where a synchronous call would
        otherwise block past the timeout and lose the write."""
        return _raise(self._c.post("/remember", json={
            "namespace": _ns(namespace, self.namespace), "text": text,
            "speaker": speaker, "session_id": session_id, "async": async_}))

    def recall(self, query, *, namespace=None, raw_quota=None, fact_quota=None, max_chars=None) -> dict:
        body = {"namespace": _ns(namespace, self.namespace), "query": query}
        for k, v in (("raw_quota", raw_quota), ("fact_quota", fact_quota), ("max_chars", max_chars)):
            if v is not None:
                body[k] = v
        return _raise(self._c.post("/recall", json=body))

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

    def healthy(self) -> bool:
        try:
            return self._c.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False

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
                       async_=False) -> dict:
        """Store a turn. async_=True defers fact extraction to the server's background workers
        (returns in ~200ms) so slow local-LLM extraction can't ReadTimeout and lose the write."""
        return _raise(await self._c.post("/remember", json={
            "namespace": _ns(namespace, self.namespace), "text": text,
            "speaker": speaker, "session_id": session_id, "async": async_}))

    async def recall(self, query, *, namespace=None, raw_quota=None, fact_quota=None, max_chars=None) -> dict:
        body = {"namespace": _ns(namespace, self.namespace), "query": query}
        for k, v in (("raw_quota", raw_quota), ("fact_quota", fact_quota), ("max_chars", max_chars)):
            if v is not None:
                body[k] = v
        return _raise(await self._c.post("/recall", json=body))

    async def context(self, query, *, namespace=None, **kw) -> str:
        return (await self.recall(query, namespace=namespace, **kw)).get("context", "")

    async def consolidate(self, *, namespace=None) -> dict:
        return _raise(await self._c.post("/consolidate", json={"namespace": _ns(namespace, self.namespace)}))

    async def feedback(self, query, helpful, *, namespace=None, note=None) -> dict:
        return _raise(await self._c.post("/feedback", json={
            "namespace": _ns(namespace, self.namespace), "query": query,
            "helpful": bool(helpful), "note": note}))

    async def aclose(self):
        await self._c.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.aclose()
