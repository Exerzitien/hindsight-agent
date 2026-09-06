"""Embedding providers used by the Hindsight RAG pipeline."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import requests


class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OllamaEmbeddingProvider:
    def __init__(
        self,
        host: str,
        model_id: str,
        dimension: int = 768,
        batch_size: int = 32,
        timeout_seconds: int = 180,
        session: requests.Session | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.model_id = model_id
        self.dimension = dimension
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for index in range(0, len(texts), self.batch_size):
            batch = texts[index : index + self.batch_size]
            try:
                response = self.session.post(
                    f"{self.host}/api/embed",
                    json={"model": self.model_id, "input": batch},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError(
                    f"Ollama embedding request failed at {self.host}; "
                    f"confirm Ollama is running and model {self.model_id!r} is installed"
                ) from exc
            embeddings = payload.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                raise RuntimeError("Ollama returned an unexpected embedding response")
            for vector in embeddings:
                if len(vector) != self.dimension:
                    raise RuntimeError(
                        f"Embedding dimension is {len(vector)}, expected {self.dimension}; "
                        "update HINDSIGHT_EMBED_DIM or use the configured model"
                    )
                vectors.append([float(value) for value in vector])
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"search_document: {text}" for text in texts])

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"search_query: {text}" for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self.embed_queries([text])[0]


class HashEmbeddingProvider:
    """Dependency-free deterministic embedding for tests and offline smoke checks."""

    model_id = "hash-embedding-test-only"

    def __init__(self, dimension: int = 128) -> None:
        self.dimension = dimension

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.casefold())
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            position = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[position] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)
