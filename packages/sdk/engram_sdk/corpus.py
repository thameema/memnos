"""
engram_sdk.corpus — Corpus sub-client for architecture constraint management.

Accessed via ``client.corpus`` on both AsyncEngramClient and EngramClient.
Provides a typed interface to the corpus REST API without requiring callers
to know endpoint paths or parse raw JSON.

Async usage::

    async with AsyncEngramClient(url="...", api_key="...") as client:
        corpus = await client.corpus.register(
            name="hdig-platform-architecture",
            source_path="/repos/hdig-platform/docs",
            namespace="org:hc:hdig:architecture",
            watch=True,
        )
        # Wait for initial sync, then check code:
        result = await client.corpus.check(
            corpus_id=corpus.id,
            code=diff_text,
            context="patient-access consent validation filter",
        )
        for hit in result.shall_violations:
            print(f"SHALL violation: {hit.content}")
            print(f"  Source: {hit.source_file} | {hit.section}")

Sync usage::

    with EngramClient(url="...", api_key="...") as client:
        corpora = client.corpus.list()
        result  = client.corpus.check(corpus_id=corpora[0].id, code=code)
        print(result.format())

CI / GitLab webhook integration::

    # Register once with watch=True and a webhook_secret:
    corpus = await client.corpus.register(..., watch=True, webhook_secret="secret")
    # Then configure GitLab to POST corpus.sync_url to trigger re-sync on push.
    print(corpus.sync_url(base_url="https://engram.internal"))

Custom connectors::

    # To use a non-default connector type, pass connector_type:
    corpus = await client.corpus.register(
        name="payments-openapi",
        source_path="/specs/payments-v3.yaml",
        namespace="org:acme:payments:architecture",
        connector_type="openapi",        # must be registered in server REGISTRY
    )
"""

from __future__ import annotations

from typing import Any

from engram_sdk.models import CheckResult, ConstraintHit, CorpusInfo, CorpusStatus


def _parse_corpus(data: dict) -> CorpusInfo:
    return CorpusInfo(
        id=data["id"],
        name=data["name"],
        source_path=data["source_path"],
        path_pattern=data.get("path_pattern", "**/*.md"),
        namespace=data["namespace"],
        connector_type=data.get("connector_type", "git-doc"),
        watch=data.get("watch", False),
        status=CorpusStatus(data.get("status", "pending")),
        node_count=data.get("node_count", 0),
        last_sync_sha=data.get("last_sync_sha", ""),
        last_sync_at=data.get("last_sync_at"),
        error_msg=data.get("error_msg", ""),
        created_at=data["created_at"],
        created_by=data.get("created_by", ""),
    )


def _parse_check(data: dict) -> CheckResult:
    hits = [
        ConstraintHit(
            memory_id=c["memory_id"],
            content=c["content"],
            severity=c.get("severity", ""),
            source_file=c.get("source_file", ""),
            section=c.get("section", ""),
            score=float(c.get("score", 0)),
        )
        for c in data.get("constraints", [])
    ]
    return CheckResult(
        corpus_id=data["corpus_id"],
        namespace=data["namespace"],
        constraints=hits,
    )


class AsyncCorpusClient:
    """Async corpus operations. Accessed via ``AsyncEngramClient.corpus``."""

    def __init__(self, transport) -> None:
        self._t = transport

    async def register(
        self,
        name: str,
        source_path: str,
        namespace: str,
        *,
        path_pattern: str = "**/*.md",
        connector_type: str = "git-doc",
        watch: bool = False,
        webhook_secret: str = "",
    ) -> CorpusInfo:
        """Register a corpus source and trigger initial ingestion.

        The ingestion runs in the background on the server; poll ``get()``
        until ``status == CorpusStatus.READY`` before calling ``check()``.
        """
        data = await self._t.post(
            "/api/v1/corpus/",
            json={
                "name": name,
                "source_path": source_path,
                "namespace": namespace,
                "path_pattern": path_pattern,
                "connector_type": connector_type,
                "watch": watch,
                "webhook_secret": webhook_secret,
            },
        )
        return _parse_corpus(data)

    async def list(self) -> list[CorpusInfo]:
        """Return all registered corpus sources."""
        data = await self._t.get("/api/v1/corpus/")
        return [_parse_corpus(item) for item in data]

    async def get(self, corpus_id: str) -> CorpusInfo:
        """Fetch a single corpus by ID."""
        data = await self._t.get(f"/api/v1/corpus/{corpus_id}")
        return _parse_corpus(data)

    async def sync(self, corpus_id: str) -> CorpusInfo:
        """Trigger a re-sync of corpus nodes from source.

        Non-blocking: the sync runs in the background.  Poll ``get()``
        until ``status == CorpusStatus.READY``.
        """
        data = await self._t.post(f"/api/v1/corpus/{corpus_id}/sync", json={})
        return _parse_corpus(data)

    async def delete(self, corpus_id: str) -> None:
        """Unregister a corpus. Does not delete the ingested memory nodes."""
        await self._t.delete(f"/api/v1/corpus/{corpus_id}")

    async def check(
        self,
        corpus_id: str,
        code: str,
        context: str = "",
        *,
        top_k: int = 10,
    ) -> CheckResult:
        """Return architecture constraints relevant to a code snippet.

        Parameters
        ----------
        corpus_id : ID of the registered corpus
        code      : code snippet being reviewed or implemented
        context   : free-text description of what the code does and which
                    module/component it belongs to, e.g.
                    "patient-access consent validation filter"
        top_k     : max constraints to return

        Returns
        -------
        CheckResult with ``.constraints``, ``.shall_violations``,
        ``.should_violations``, and ``.format()`` helper.

        Example
        -------
        ::

            result = await client.corpus.check(
                corpus_id=corpus.id,
                code=diff_text,
                context="patient-access consent filter",
            )
            if result.shall_violations:
                raise ArchitectureViolationError(result.format())
        """
        data = await self._t.post(
            f"/api/v1/corpus/{corpus_id}/check",
            json={"code": code, "context": context, "top_k": top_k},
        )
        return _parse_check(data)

    async def check_all(
        self,
        code: str,
        context: str = "",
        *,
        top_k: int = 10,
    ) -> list[CheckResult]:
        """Check code against ALL registered corpora and return combined results.

        Useful when a code change may touch multiple modules and you want
        constraints from all relevant corpora without knowing the corpus IDs.
        Only READY corpora are checked; SYNCING/ERROR corpora are skipped.
        """
        corpora = await self.list()
        results = []
        for corpus in corpora:
            if corpus.status != CorpusStatus.READY:
                continue
            result = await self.check(corpus.id, code, context, top_k=top_k)
            if result.constraints:
                results.append(result)
        return results


class SyncCorpusClient:
    """Synchronous corpus operations. Accessed via ``EngramClient.corpus``."""

    def __init__(self, async_client: AsyncCorpusClient, run_fn) -> None:
        self._async = async_client
        self._run = run_fn

    def register(
        self,
        name: str,
        source_path: str,
        namespace: str,
        *,
        path_pattern: str = "**/*.md",
        connector_type: str = "git-doc",
        watch: bool = False,
        webhook_secret: str = "",
    ) -> CorpusInfo:
        return self._run(
            self._async.register(
                name, source_path, namespace,
                path_pattern=path_pattern,
                connector_type=connector_type,
                watch=watch,
                webhook_secret=webhook_secret,
            )
        )

    def list(self) -> list[CorpusInfo]:
        return self._run(self._async.list())

    def get(self, corpus_id: str) -> CorpusInfo:
        return self._run(self._async.get(corpus_id))

    def sync(self, corpus_id: str) -> CorpusInfo:
        return self._run(self._async.sync(corpus_id))

    def delete(self, corpus_id: str) -> None:
        self._run(self._async.delete(corpus_id))

    def check(
        self,
        corpus_id: str,
        code: str,
        context: str = "",
        *,
        top_k: int = 10,
    ) -> CheckResult:
        return self._run(self._async.check(corpus_id, code, context, top_k=top_k))

    def check_all(
        self,
        code: str,
        context: str = "",
        *,
        top_k: int = 10,
    ) -> list[CheckResult]:
        return self._run(self._async.check_all(code, context, top_k=top_k))
