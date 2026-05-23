"""
engram.vector.embedder — Embedding abstraction with multiple backends.

Providers
---------
openai   (default) — OpenAI text-embedding-3-small; reads OPENAI_API_KEY from env.
                     Also works with any OpenAI-compatible endpoint via base_url
                     (Mistral, Together AI, Ollama, etc.).
voyage             — Voyage AI embeddings; reads VOYAGE_API_KEY from env.
                     Recommended when the rest of the stack uses Anthropic.
local              — sentence-transformers (CPU/GPU, fully offline).
                     Opt-in: pip install 'engram-core[local-embeddings]'
                     Pulls in ~2 GB of torch + CUDA packages.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.config import EmbeddingsConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Embedder(ABC):
    """Abstract embedder — takes text, returns float vectors."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Embed a single string."""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of strings efficiently."""

    @property
    @abstractmethod
    def vector_size(self) -> int:
        """Dimensionality of the produced vectors."""


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible implementation
# ---------------------------------------------------------------------------

class OpenAIEmbedder(Embedder):
    """
    Embedder backed by OpenAI's text-embedding API.

    Also works with any OpenAI-compatible endpoint — set base_url to point at
    Mistral, Together AI, Ollama (/v1), Groq, etc.

    api_key is optional here: when omitted the openai SDK reads OPENAI_API_KEY
    from the environment automatically, so no secrets need to live in the config.
    """

    _DIMENSIONS: dict[str, int] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        model: str,
        api_key: str = "",
        base_url: str = "",
        dimensions: int = 0,
    ) -> None:
        self._model = model
        self._api_key = api_key or None   # None → SDK reads OPENAI_API_KEY from env
        self._base_url = base_url or None
        self._dimensions = dimensions      # 0 = model default
        self._client: object | None = None

    def _get_client(self):  # type: ignore[return]
        if self._client is None:
            try:
                from openai import AsyncOpenAI  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "openai package is required. Install with: pip install openai"
                ) from exc
            kwargs: dict = {}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    @property
    def vector_size(self) -> int:
        if self._dimensions:
            return self._dimensions
        return self._DIMENSIONS.get(self._model, 1536)

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        logger.debug("OpenAIEmbedder: embedding %d texts with %s", len(texts), self._model)
        kwargs: dict = {"model": self._model, "input": texts}
        if self._dimensions:
            kwargs["dimensions"] = self._dimensions
        response = await client.embeddings.create(**kwargs)  # type: ignore[union-attr]
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


# ---------------------------------------------------------------------------
# Voyage AI implementation
# ---------------------------------------------------------------------------

class VoyageEmbedder(Embedder):
    """
    Embedder backed by Voyage AI (https://www.voyageai.com).

    Reads VOYAGE_API_KEY from the environment when api_key is not set.
    Recommended models: voyage-3-lite (1024-dim, fast), voyage-3 (1024-dim, best).
    """

    _DIMENSIONS: dict[str, int] = {
        "voyage-3-lite": 512,
        "voyage-3":      1024,
        "voyage-code-3": 1024,
        "voyage-finance-2": 1024,
    }

    def __init__(self, model: str = "voyage-3-lite", api_key: str = "") -> None:
        self._model = model
        self._api_key = api_key
        self._client: object | None = None

    def _get_client(self):  # type: ignore[return]
        if self._client is None:
            try:
                import voyageai  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "voyageai package is required for VoyageEmbedder. "
                    "Install with: pip install voyageai"
                ) from exc
            import os
            key = self._api_key or os.environ.get("VOYAGE_API_KEY", "")
            self._client = voyageai.AsyncClient(api_key=key)
        return self._client

    @property
    def vector_size(self) -> int:
        return self._DIMENSIONS.get(self._model, 1024)

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        logger.debug("VoyageEmbedder: embedding %d texts with %s", len(texts), self._model)
        result = await client.embed(texts, model=self._model)  # type: ignore[union-attr]
        return result.embeddings


# ---------------------------------------------------------------------------
# Local implementation (sentence-transformers — opt-in)
# ---------------------------------------------------------------------------

class LocalEmbedder(Embedder):
    """
    Embedder backed by sentence-transformers (CPU/GPU, fully offline).

    Opt-in only — requires: pip install 'engram-core[local-embeddings]'
    This pulls in torch + CUDA (~2 GB). Not included in the default Docker image.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model
        self._model: object | None = None

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is not installed.\n"
                    "Enable local embeddings with: pip install 'engram-core[local-embeddings]'\n"
                    "Note: this installs torch (~2 GB) and CUDA libraries."
                ) from exc
            logger.info("LocalEmbedder: loading %s (first call — may take a moment)", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    @property
    def vector_size(self) -> int:
        try:
            dim = self._get_model().get_sentence_embedding_dimension()  # type: ignore[union-attr]
            return int(dim) if dim else 384
        except ImportError:
            return 384

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import asyncio
        model = self._get_model()
        logger.debug("LocalEmbedder: embedding %d texts", len(texts))
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: model.encode(texts, convert_to_numpy=True).tolist(),  # type: ignore[union-attr]
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_embedder(config: "EmbeddingsConfig") -> Embedder:
    """
    Return the correct Embedder for the configured provider.

    ENGRAM_EMBED_MODE aliases:
      online  → openai  (default; lightweight, no local ML packages)
      local   → local   (sentence-transformers; requires local-embeddings extra)
    """
    provider = (config.provider or "online").lower()

    # "online" is the user-facing alias for the default remote provider
    if provider in ("online", "openai"):
        return OpenAIEmbedder(
            model=config.model or "text-embedding-3-small",
            api_key=config.api_key,
            base_url=config.base_url,
            dimensions=config.dimensions,
        )

    if provider == "voyage":
        return VoyageEmbedder(
            model=config.model or "voyage-3-lite",
            api_key=config.api_key,
        )

    if provider == "local":
        return LocalEmbedder(model=config.model or "all-MiniLM-L6-v2")

    raise ValueError(
        f"Unknown embeddings provider {config.provider!r}. "
        "Supported: 'online' (OpenAI), 'voyage', 'local' (sentence-transformers)."
    )
