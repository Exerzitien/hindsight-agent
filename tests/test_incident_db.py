from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from hindsight_agent.incident_db import (
    _connect,
    import_news_snapshot,
    import_sir_snapshot,
)


SIR_ROW = {
    "ID": "2026090001", "UPA": "123456", "EventDate": "9/1/2026",
    "Employer": "Demo Employer", "Address1": "1 Test St", "Address2": "",
    "City": "TEST", "State": "OHIO", "Zip": "00001", "Latitude": "40.1",
    "Longitude": "-82.1", "Primary NAICS": "332111", "Hospitalized": "1.00",
    "Amputation": "0.00", "Loss of Eye": "0.00", "Inspection": "",
    "Final Narrative": "Worker was injured while clearing a machine jam.",
    "Nature": "111", "NatureTitle": "Fractures", "Part of Body": "230",
    "Part of Body Title": "Hand", "Event": "6412", "EventTitle": "Caught in machinery",
    "Source": "3300", "SourceTitle": "Machinery", "Secondary Source": "",
    "Secondary Source Title": "", "FederalState": "1",
}


def write_manifest(directory: Path, source: str, sha: str) -> Path:
    path = directory / "manifest.json"
    path.write_text(json.dumps({
        "source": source,
        "collected_at": "2026-09-01T00:00:00Z",
        "sha256": sha,
        "snapshot_directory": directory.name,
    }), encoding="utf-8")
    return path


def test_imports_sir_with_typed_values_and_is_idempotent(tmp_path: Path) -> None:
    snapshot = tmp_path / "sir-snapshot"
    snapshot.mkdir()
    csv_path = snapshot / "sir.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(SIR_ROW))
        writer.writeheader()
        writer.writerow(SIR_ROW)
    manifest = write_manifest(snapshot, "https://www.osha.gov/test.zip", "abc")
    connection = _connect(tmp_path / "incidents.db")
    try:
        with connection:
            first = import_sir_snapshot(connection, csv_path, manifest)
            second = import_sir_snapshot(connection, csv_path, manifest)
        row = connection.execute(
            "SELECT incident_key, event_date, zip_code, hospitalized FROM osha_sir_incidents"
        ).fetchone()
    finally:
        connection.close()

    assert first == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert second == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert row == ("2026090001:123456", "2026-09-01", "00001", 1)


def test_imports_news_jsonl_without_duplicates(tmp_path: Path) -> None:
    snapshot = tmp_path / "news-snapshot"
    snapshot.mkdir()
    entry = {
        "entry_id": "entry-1", "source_id": "source-1", "title": "OSHA release",
        "url": "https://www.osha.gov/news/test", "published_at": "2026-09-01T00:00:00Z",
        "summary": "September 1, 2026", "content_hash": "hash-1",
        "collected_at": "2026-09-01T01:00:00Z",
    }
    entries = snapshot / "entries.jsonl"
    entries.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    manifest = write_manifest(snapshot, "https://www.osha.gov/news/newsreleases.xml", "feed")
    connection = _connect(tmp_path / "incidents.db")
    try:
        with connection:
            first = import_news_snapshot(connection, entries, manifest)
            second = import_news_snapshot(connection, entries, manifest)
        count = connection.execute("SELECT COUNT(*) FROM osha_news_releases").fetchone()[0]
    finally:
        connection.close()

    assert first["inserted"] == 1
    assert second["unchanged"] == 1
    assert count == 1
