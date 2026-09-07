"""End-to-end collection, incremental vectorization, retrieval, and audit pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .embeddings import EmbeddingProvider, OllamaEmbeddingProvider
from .incident_db import _connect as connect_incident_database
from .incident_db import import_current_sources
from .matching import match_incident
from .milvus_store import MilvusVectorStore, VectorStore
from .news_enrichment import enrich_news_entries
from .rag_db import (
    documents_needing_sync,
    ensure_rag_schema,
    finish_run,
    mark_documents_synced,
    save_matches,
    start_run,
    unsynced_incident_keys,
)
from .rag_documents import build_factory_risk_documents, load_incident_documents
from .tools.collect_osha import collect_osha_sir_impl
from .tools.collect_osha_news import collect_osha_news_impl


def _provider(settings: Settings) -> EmbeddingProvider:
    return OllamaEmbeddingProvider(
        host=settings.ollama_host,
        model_id=settings.embedding_model,
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
    )


def _store(settings: Settings) -> VectorStore:
    return MilvusVectorStore(
        uri=settings.milvus_uri,
        token=settings.milvus_token,
        factory_collection=settings.factory_collection,
        incident_collection=settings.incident_collection,
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def run_pipeline(
    *,
    settings: Settings | None = None,
    trigger_type: str = "cli",
    collect: bool = True,
    import_data: bool = True,
    backfill_limit: int | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store: VectorStore | None = None,
    screen: bool = True,
    screen_limit: int = 500,
) -> dict[str, Any]:
    settings = settings or load_settings()
    backfill_limit = (
        settings.daily_backfill_limit if backfill_limit is None else backfill_limit
    )
    if not settings.factory_database.exists():
        raise FileNotFoundError(
            f"Factory database does not exist: {settings.factory_database}; "
            "run hindsight-factory-create-zh first"
        )

    base_connection = connect_incident_database(settings.incident_database)
    base_connection.close()
    ensure_rag_schema(settings.incident_database)
    run_id = start_run(settings.incident_database, trigger_type)
    collection_result: dict[str, Any] = {}
    import_result: dict[str, Any] = {
        "changed": {"osha_sir": [], "osha_news": []}
    }
    sync_result: dict[str, Any] = {}
    match_result: dict[str, Any] = {}
    try:
        if collect:
            collection_result = {
                "osha_sir": collect_osha_sir_impl(),
                "osha_news": collect_osha_news_impl(),
            }
        if import_data:
            import_result = import_current_sources(
                database_path=settings.incident_database,
                base_dir=settings.project_root,
            )

        provider = embedding_provider or _provider(settings)
        store = vector_store or _store(settings)
        store.ensure_collections(provider.dimension)

        factory_documents = build_factory_risk_documents(settings.factory_database)
        factory_pending = documents_needing_sync(
            settings.incident_database,
            factory_documents,
            provider.model_id,
            store.factory_collection,
        )
        factory_vectors = provider.embed_documents(
            [document.text for document in factory_pending]
        )
        store.upsert_factory(factory_pending, factory_vectors)
        mark_documents_synced(
            settings.incident_database,
            factory_pending,
            provider.model_id,
            store.factory_collection,
        )

        changed = import_result.get("changed", {})
        changed_sir = list(changed.get("osha_sir", []))
        changed_news = list(changed.get("osha_news", []))
        bulk_first_import = backfill_limit > 0 and len(changed_sir) > backfill_limit
        if bulk_first_import:
            changed_sir = []

        backlog = unsynced_incident_keys(settings.incident_database, backfill_limit)
        sir_keys = _unique(changed_sir + backlog["osha_sir"])
        news_keys = _unique(changed_news + backlog["osha_news"])

        enrichment_result = enrich_news_entries(
            settings.incident_database,
            news_keys,
        )
        incident_documents = load_incident_documents(
            settings.incident_database,
            sir_keys=sir_keys,
            news_keys=news_keys,
        )
        incident_pending = documents_needing_sync(
            settings.incident_database,
            incident_documents,
            provider.model_id,
            store.incident_collection,
        )
        incident_vectors = provider.embed_documents(
            [document.text for document in incident_pending]
        )
        store.upsert_incidents(incident_pending, incident_vectors)
        mark_documents_synced(
            settings.incident_database,
            incident_pending,
            provider.model_id,
            store.incident_collection,
        )

        query_vectors = provider.embed_queries(
            [document.text for document in incident_pending]
        )
        matches: list[dict[str, Any]] = []
        matched_incidents = 0
        for document, query_vector in zip(
            incident_pending,
            query_vectors,
            strict=True,
        ):
            document_matches = match_incident(
                document,
                query_vector,
                store,
                top_k=settings.top_k,
                threshold=settings.relevance_threshold,
            )
            if document_matches:
                matched_incidents += 1
                matches.extend(document_matches)
        saved_matches = save_matches(settings.incident_database, run_id, matches)

        sync_result = {
            "embedding_model": provider.model_id,
            "embedding_dimension": provider.dimension,
            "factory_documents_total": len(factory_documents),
            "factory_vectors_upserted": len(factory_pending),
            "incident_vectors_upserted": len(incident_pending),
            "bulk_first_import_limited": bulk_first_import,
            "backfill_limit": backfill_limit,
            "news_enrichment": enrichment_result,
            "milvus": store.status(),
        }
        # ③④⑤ 层：筛选、残余路径判定、四级路由、静默归档与人工队列。
        # 它只读 SQLite，不依赖 Milvus/Ollama，所以即使向量层降级也照常运行。
        screen_result: dict[str, Any] = {"skipped": True}
        if screen:
            from .workflow import screen_and_route, fetch_incidents

            screen_result = screen_and_route(
                incident_database=settings.incident_database,
                factory_database=settings.factory_database,
                records=fetch_incidents(settings.incident_database, limit=screen_limit),
                run_id=run_id,
            )
            screen_result = {
                "counts": screen_result["counts"],
                "levels": screen_result["levels"],
                "rule_pack_version": screen_result["rule_pack_version"],
                "alert_budget": screen_result["alert_budget"],
                "demoted_over_budget": screen_result["demoted_over_budget"],
            }

        match_result = {
            "screening": screen_result,
            "incidents_evaluated": len(incident_pending),
            "incidents_with_matches": matched_incidents,
            "matches_saved": saved_matches,
            "top_k": settings.top_k,
            "relevance_threshold": settings.relevance_threshold,
            "score_is_probability": False,
        }
        result = {
            "status": "completed",
            "run_id": run_id,
            "collection": collection_result,
            "import": import_result,
            "sync": sync_result,
            "matching": match_result,
        }
        finish_run(
            settings.incident_database,
            run_id,
            "completed",
            collection_result=collection_result,
            import_result=import_result,
            sync_result=sync_result,
            match_result=match_result,
        )
        return result
    except Exception as exc:
        finish_run(
            settings.incident_database,
            run_id,
            "failed",
            collection_result=collection_result,
            import_result=import_result,
            sync_result=sync_result,
            match_result=match_result,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def _parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=None,
        help="每次最多补齐多少条未向量化历史记录；0 表示全部",
    )
    return parser


def main() -> int:
    parser = _parser("运行 Hindsight 每日巡检与工厂风险匹配。")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    args = parser.parse_args()
    result = run_pipeline(
        collect=not args.skip_collect,
        import_data=not args.skip_import,
        backfill_limit=args.backfill_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def bootstrap_main() -> int:
    parser = _parser("为已有事故和工厂数据建立 Milvus 向量索引。")
    args = parser.parse_args()
    result = run_pipeline(
        trigger_type="bootstrap",
        collect=False,
        import_data=False,
        backfill_limit=args.backfill_limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
