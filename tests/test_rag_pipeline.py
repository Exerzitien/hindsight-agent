from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from hindsight_agent.config import Settings
from hindsight_agent.daily_pipeline import run_pipeline
from hindsight_agent.embeddings import HashEmbeddingProvider
from hindsight_agent.factory_db import create_factory_database
from hindsight_agent.incident_db import _connect, import_sir_snapshot
from hindsight_agent.milvus_store import InMemoryVectorStore
from hindsight_agent.news_enrichment import extract_article_text
from hindsight_agent.rag_db import dashboard_summary


SIR_ROW = {
    "ID": "2026090001",
    "UPA": "123456",
    "EventDate": "9/1/2026",
    "Employer": "Demo Metal Works",
    "Address1": "1 Test St",
    "Address2": "",
    "City": "TEST",
    "State": "OHIO",
    "Zip": "00001",
    "Latitude": "40.1",
    "Longitude": "-82.1",
    "Primary NAICS": "332111",
    "Hospitalized": "1.00",
    "Amputation": "1.00",
    "Loss of Eye": "0.00",
    "Inspection": "",
    "Final Narrative": "Worker's hand was amputated while clearing a press machine jam.",
    "Nature": "111",
    "NatureTitle": "Amputations",
    "Part of Body": "230",
    "Part of Body Title": "Hand",
    "Event": "6412",
    "EventTitle": "Caught in machinery",
    "Source": "3300",
    "SourceTitle": "Machinery",
    "Secondary Source": "",
    "Secondary Source Title": "",
    "FederalState": "1",
}


def _seed_incident_database(path: Path, source_dir: Path) -> None:
    snapshot = source_dir / "sir-snapshot"
    snapshot.mkdir()
    csv_path = snapshot / "sir.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(SIR_ROW))
        writer.writeheader()
        writer.writerow(SIR_ROW)
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": "https://www.osha.gov/test.zip",
                "collected_at": "2026-09-01T00:00:00Z",
                "sha256": "abc",
                "snapshot_directory": snapshot.name,
            }
        ),
        encoding="utf-8",
    )
    connection = _connect(path)
    try:
        with connection:
            import_sir_snapshot(connection, csv_path, manifest)
    finally:
        connection.close()


def test_pipeline_is_incremental_and_writes_match_audit_tables(tmp_path: Path) -> None:
    incident_db = tmp_path / "incidents.db"
    factory_db = tmp_path / "factory.db"
    _seed_incident_database(incident_db, tmp_path)
    create_factory_database(factory_db)
    settings = Settings(
        project_root=tmp_path,
        incident_database=incident_db,
        factory_database=factory_db,
        milvus_uri="memory://",
        milvus_token="",
        milvus_prefix="test",
        ollama_host="http://localhost:11434",
        embedding_model="hash-embedding-test-only",
        embedding_dimension=128,
        embedding_batch_size=16,
        top_k=8,
        relevance_threshold=0.0,
        daily_backfill_limit=10,
    )
    provider = HashEmbeddingProvider(dimension=128)
    store = InMemoryVectorStore()

    first = run_pipeline(
        settings=settings,
        collect=False,
        import_data=False,
        embedding_provider=provider,
        vector_store=store,
    )
    second = run_pipeline(
        settings=settings,
        collect=False,
        import_data=False,
        embedding_provider=provider,
        vector_store=store,
    )

    assert first["sync"]["factory_vectors_upserted"] == 97
    assert first["sync"]["incident_vectors_upserted"] == 1
    assert first["matching"]["matches_saved"] == 8
    assert second["sync"]["factory_vectors_upserted"] == 0
    assert second["sync"]["incident_vectors_upserted"] == 0
    summary = dashboard_summary(incident_db)
    assert summary["counts"]["factory_vectors"] == 97
    assert summary["counts"]["incident_vectors"] == 1
    connection = sqlite3.connect(incident_db)
    try:
        assert connection.execute('SELECT COUNT(*) FROM "待人工决定"').fetchone()[0] == 8
        warning = json.loads(
            connection.execute(
                "SELECT rationale_json FROM incident_factory_matches LIMIT 1"
            ).fetchone()[0]
        )["warning"]
    finally:
        connection.close()
    assert "not accident probability" in warning


def test_article_extraction_uses_main_content() -> None:
    html = """
    <html><header>Navigation</header><main><h1>Release</h1>
    <p>A worker was injured while clearing a machine jam.</p></main>
    <footer>Footer</footer></html>
    """
    text = extract_article_text(html)
    assert "worker was injured" in text
    assert "Navigation" not in text
    assert "Footer" not in text
