"""
engram.vector.embedder — Embedding abstraction with OpenAI and local backends.
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
# OpenAI implementation
# ---------------------------------------------------------------------------

class OpenAIEmbedder(Embedder):
    """Embedder backed by OpenAI's text-embedding API."""

    # Known dimensions for OpenAI models
    _DIMENSIONS: dict[str, int] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key
        self._client: object | None = None  # lazy-init

    def _get_client(self):  # type: ignore[return]
        if self._client is None:
            try:
                from openai import AsyncOpenAI  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "openai package is required for OpenAIEmbedder. "
                    "Install it with: pip install openai"
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    @property
    def vector_size(self) -> int:
        return self._DIMENSIONS.get(self._model, 1536)

    async def embed(self, text: str) -> list[float]:
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get_client()
        logger.debug("OpenAIEmbedder: embedding %d texts with model %s", len(texts), self._model)
        response = await client.embeddings.create(model=self._model, input=texts)  # type: ignore[union-attr]
        # response.data is sorted by index
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


# ---------------------------------------------------------------------------
# Local implementation (sentence-transformers)
# ---------------------------------------------------------------------------

class LocalEmbedder(Embedder):
    """Embedder backed by sentence-transformers (CPU/GPU, no API key needed)."""

    def __init__(self, model: str) -> None:
        self._model_name = model
        self._model: object | None = None  # lazy-init

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers package is required for LocalEmbedder. "
                    "Install it with: pip install 'engram-core[local-embeddings]'"
                ) from exc
            logger.info("LocalEmbedder: loading model %s (first call — may take a moment)", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    @property
    def vector_size(self) -> int:
        # Common default; will be correct after model loads
        try:
            model = self._get_model()
            dim = model.get_sentence_embedding_dimension()  # type: ignore[union-attr]
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
        # Run blocking encode in a thread pool to avoid blocking the event loop
        embeddings = await loop.run_in_executor(
            None,
            lambda: model.encode(texts, convert_to_numpy=True).tolist(),  # type: ignore[union-attr]
        )
        return embeddings


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_embedder(config: "EmbeddingsConfig") -> Embedder:
    """Return the correct Embedder implementation based on config.provider."""
    provider = (config.provider or "local").lower()
    if provider == "local":
        return LocalEmbedder(model=config.model or "all-MiniLM-L6-v2")
    if provider == "openai":
        if not config.api_key:
            logger.warning(
                "OpenAI embeddings selected but api_key is empty — calls will likely fail"
            )
        return OpenAIEmbedder(model=config.model, api_key=config.api_key)
    raise ValueError(
        f"Unknown embeddings provider {config.provider!r}. "
        "Supported values: 'local', 'openai'."
    )
