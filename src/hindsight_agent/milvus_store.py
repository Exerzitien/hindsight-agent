"""Milvus collections and vector operations for incident-to-factory retrieval."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

from .rag_documents import RagDocument


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float
    title: str
    text: str
    naics: str
    metadata: dict[str, Any]


class VectorStore(Protocol):
    factory_collection: str
    incident_collection: str

    def ensure_collections(self, dimension: int) -> None: ...

    def upsert_factory(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int: ...

    def upsert_incidents(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int: ...

    def search_factory(self, vector: list[float], limit: int) -> list[VectorHit]: ...

    def status(self) -> dict[str, Any]: ...


class MilvusVectorStore:
    def __init__(
        self,
        uri: str,
        token: str,
        factory_collection: str,
        incident_collection: str,
    ) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "pymilvus is not installed; run: python -m pip install -e ."
            ) from exc
        kwargs: dict[str, Any] = {"uri": uri}
        if token:
            kwargs["token"] = token
        try:
            self.client = MilvusClient(**kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot connect to Milvus at {uri}; start Docker Desktop and "
                "run docker compose up -d"
            ) from exc
        self.factory_collection = factory_collection
        self.incident_collection = incident_collection

    @staticmethod
    def _schema(dimension: int):
        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=512)
        schema.add_field("document_type", DataType.VARCHAR, max_length=32)
        schema.add_field("source_record_id", DataType.VARCHAR, max_length=512)
        schema.add_field("title", DataType.VARCHAR, max_length=2048)
        schema.add_field("text", DataType.VARCHAR, max_length=16384)
        schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
        schema.add_field("event_date", DataType.VARCHAR, max_length=64)
        schema.add_field("naics", DataType.VARCHAR, max_length=16)
        schema.add_field("metadata_json", DataType.VARCHAR, max_length=16384)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimension)
        return schema

    def _ensure_collection(self, name: str, dimension: int) -> None:
        if self.client.has_collection(collection_name=name):
            description = self.client.describe_collection(collection_name=name)
            fields = description.get("fields", [])
            vector_fields = [field for field in fields if field.get("name") == "embedding"]
            if vector_fields:
                params = vector_fields[0].get("params", {})
                existing_dim = int(params.get("dim", dimension))
                if existing_dim != dimension:
                    raise RuntimeError(
                        f"Milvus collection {name} has dimension {existing_dim}, "
                        f"but the embedding model requires {dimension}. Change the "
                        "collection prefix or rebuild the collection."
                    )
            self.client.load_collection(collection_name=name)
            return

        from pymilvus import MilvusClient

        index_params = MilvusClient.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_name="embedding_cosine",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        self.client.create_collection(
            collection_name=name,
            schema=self._schema(dimension),
            index_params=index_params,
            consistency_level="Strong",
        )
        self.client.load_collection(collection_name=name)

    def ensure_collections(self, dimension: int) -> None:
        self._ensure_collection(self.factory_collection, dimension)
        self._ensure_collection(self.incident_collection, dimension)

    @staticmethod
    def _entities(
        documents: list[RagDocument], vectors: list[list[float]]
    ) -> list[dict[str, Any]]:
        if len(documents) != len(vectors):
            raise ValueError("document and embedding counts do not match")
        return [
            {
                "id": document.id,
                "document_type": document.document_type,
                "source_record_id": document.source_record_id,
                "title": document.title[:2048],
                "text": document.text[:16384],
                "content_hash": document.content_hash,
                "event_date": document.event_date[:64],
                "naics": document.naics[:16],
                "metadata_json": json.dumps(
                    document.metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                )[:16384],
                "embedding": vector,
            }
            for document, vector in zip(documents, vectors, strict=True)
        ]

    def _upsert(
        self,
        collection: str,
        documents: list[RagDocument],
        vectors: list[list[float]],
    ) -> int:
        if not documents:
            return 0
        self.client.upsert(
            collection_name=collection,
            data=self._entities(documents, vectors),
        )
        self.client.flush(collection_name=collection)
        return len(documents)

    def upsert_factory(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int:
        return self._upsert(self.factory_collection, documents, vectors)

    def upsert_incidents(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int:
        return self._upsert(self.incident_collection, documents, vectors)

    def search_factory(self, vector: list[float], limit: int) -> list[VectorHit]:
        results = self.client.search(
            collection_name=self.factory_collection,
            data=[vector],
            anns_field="embedding",
            limit=limit,
            output_fields=["title", "text", "naics", "metadata_json"],
            search_params={"metric_type": "COSINE", "params": {}},
        )
        hits: list[VectorHit] = []
        for hit in results[0] if results else []:
            entity = hit.get("entity", {})
            hits.append(
                VectorHit(
                    id=str(hit.get("id")),
                    score=float(hit.get("distance", 0.0)),
                    title=entity.get("title", ""),
                    text=entity.get("text", ""),
                    naics=entity.get("naics", ""),
                    metadata=json.loads(entity.get("metadata_json") or "{}"),
                )
            )
        return hits

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"backend": "milvus", "collections": {}}
        for collection in (self.factory_collection, self.incident_collection):
            exists = self.client.has_collection(collection_name=collection)
            item: dict[str, Any] = {"exists": exists}
            if exists:
                stats = self.client.get_collection_stats(collection_name=collection)
                item["row_count"] = int(stats.get("row_count", 0))
            result["collections"][collection] = item
        return result


class InMemoryVectorStore:
    """Small vector store used only by tests; production always uses Milvus."""

    def __init__(self) -> None:
        self.factory_collection = "memory_factory"
        self.incident_collection = "memory_incidents"
        self.dimension = 0
        self.factory: dict[str, tuple[RagDocument, list[float]]] = {}
        self.incidents: dict[str, tuple[RagDocument, list[float]]] = {}

    def ensure_collections(self, dimension: int) -> None:
        self.dimension = dimension

    @staticmethod
    def _upsert(
        target: dict[str, tuple[RagDocument, list[float]]],
        documents: list[RagDocument],
        vectors: list[list[float]],
    ) -> int:
        for document, vector in zip(documents, vectors, strict=True):
            target[document.id] = (document, vector)
        return len(documents)

    def upsert_factory(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int:
        return self._upsert(self.factory, documents, vectors)

    def upsert_incidents(
        self, documents: list[RagDocument], vectors: list[list[float]]
    ) -> int:
        return self._upsert(self.incidents, documents, vectors)

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def search_factory(self, vector: list[float], limit: int) -> list[VectorHit]:
        ranked = sorted(
            self.factory.values(),
            key=lambda item: self._cosine(vector, item[1]),
            reverse=True,
        )[:limit]
        return [
            VectorHit(
                id=document.id,
                score=self._cosine(vector, stored),
                title=document.title,
                text=document.text,
                naics=document.naics,
                metadata=document.metadata,
            )
            for document, stored in ranked
        ]

    def status(self) -> dict[str, Any]:
        return {
            "backend": "memory-test-only",
            "collections": {
                self.factory_collection: {"exists": True, "row_count": len(self.factory)},
                self.incident_collection: {"exists": True, "row_count": len(self.incidents)},
            },
        }
