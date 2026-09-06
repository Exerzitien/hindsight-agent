"""Import preserved OSHA source snapshots into a queryable SQLite database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "incidents" / "incidents.db"


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS database_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    source_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    snapshot_directory TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (source_type, snapshot_id)
);

CREATE TABLE IF NOT EXISTS osha_sir_incidents (
    incident_key TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    upa TEXT NOT NULL,
    event_date TEXT NOT NULL,
    employer TEXT NOT NULL,
    address1 TEXT,
    address2 TEXT,
    city TEXT,
    state TEXT NOT NULL,
    zip_code TEXT,
    latitude REAL,
    longitude REAL,
    primary_naics TEXT,
    hospitalized INTEGER NOT NULL,
    amputation INTEGER NOT NULL,
    loss_of_eye INTEGER NOT NULL,
    inspection TEXT,
    narrative TEXT NOT NULL,
    nature_code TEXT NOT NULL,
    nature_title TEXT,
    body_part_code TEXT NOT NULL,
    body_part_title TEXT,
    event_code TEXT NOT NULL,
    event_title TEXT,
    source_code TEXT NOT NULL,
    source_title TEXT,
    secondary_source_code TEXT,
    secondary_source_title TEXT,
    federal_state INTEGER,
    content_hash TEXT NOT NULL,
    source_snapshot_key TEXT NOT NULL REFERENCES source_snapshots(source_key),
    imported_at TEXT NOT NULL,
    UNIQUE (incident_id, upa)
);

CREATE TABLE IF NOT EXISTS osha_news_releases (
    entry_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    summary TEXT,
    content_hash TEXT NOT NULL,
    first_collected_at TEXT NOT NULL,
    last_collected_at TEXT NOT NULL,
    source_snapshot_key TEXT NOT NULL REFERENCES source_snapshots(source_key),
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sir_event_date ON osha_sir_incidents(event_date);
CREATE INDEX IF NOT EXISTS idx_sir_state ON osha_sir_incidents(state);
CREATE INDEX IF NOT EXISTS idx_sir_naics ON osha_sir_incidents(primary_naics);
CREATE INDEX IF NOT EXISTS idx_sir_employer ON osha_sir_incidents(employer);
CREATE INDEX IF NOT EXISTS idx_news_published ON osha_news_releases(published_at);

CREATE VIEW IF NOT EXISTS "事故记录" AS
SELECT incident_id AS "事故ID", event_date AS "事故日期", employer AS "企业",
       incident_key AS "记录键", upa AS "UPA",
       city AS "城市", state AS "州", primary_naics AS "NAICS",
       hospitalized AS "住院人数", amputation AS "截肢人数",
       loss_of_eye AS "失眼人数", narrative AS "事故描述",
       nature_title AS "伤害性质", body_part_title AS "受伤部位",
       event_title AS "事故类型", source_title AS "伤害来源",
       inspection AS "OSHA调查编号", source_snapshot_key AS "来源快照"
FROM osha_sir_incidents;

CREATE VIEW IF NOT EXISTS "官方公告" AS
SELECT entry_id AS "公告ID", title AS "标题", published_at AS "发布日期",
       url AS "链接", summary AS "摘要", source_snapshot_key AS "来源快照"
FROM osha_news_releases;
"""


SIR_COLUMNS = [
    "incident_key", "incident_id", "upa", "event_date", "employer", "address1", "address2",
    "city", "state", "zip_code", "latitude", "longitude", "primary_naics",
    "hospitalized", "amputation", "loss_of_eye", "inspection", "narrative",
    "nature_code", "nature_title", "body_part_code", "body_part_title",
    "event_code", "event_title", "source_code", "source_title",
    "secondary_source_code", "secondary_source_title", "federal_state",
    "content_hash", "source_snapshot_key", "imported_at",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _nullable(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _integer(value: str | None) -> int:
    value = (value or "0").strip() or "0"
    try:
        return int(Decimal(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid incident count: {value}") from exc


def _float(value: str | None) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def _date(value: str) -> str:
    value = value.strip()
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid OSHA event date: {value}") from exc


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT OR REPLACE INTO database_metadata(key, value) VALUES (?, ?)",
        ("schema_version", "1.1"),
    )
    return connection


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read snapshot manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Snapshot manifest must be an object: {path}")
    return manifest


def _register_snapshot(
    connection: sqlite3.Connection,
    source_type: str,
    snapshot_dir: Path,
    manifest: dict[str, Any],
    imported_at: str,
) -> str:
    snapshot_id = snapshot_dir.name
    source_key = f"{source_type}:{snapshot_id}"
    source_url = (
        manifest.get("download_url")
        or manifest.get("source")
        or manifest.get("source_page")
        or "unknown"
    )
    sha256 = manifest.get("sha256") or manifest.get("feed_sha256") or "unknown"
    connection.execute(
        """
        INSERT INTO source_snapshots(
            source_key, source_type, snapshot_id, source_url, collected_at,
            sha256, snapshot_directory, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            source_url=excluded.source_url,
            collected_at=excluded.collected_at,
            sha256=excluded.sha256,
            snapshot_directory=excluded.snapshot_directory,
            imported_at=excluded.imported_at
        """,
        (
            source_key,
            source_type,
            snapshot_id,
            source_url,
            manifest.get("collected_at") or manifest.get("last_successful_collection") or "unknown",
            sha256,
            manifest.get("snapshot_directory") or snapshot_dir.as_posix(),
            imported_at,
        ),
    )
    return source_key


def _normalize_sir_row(
    row: dict[str, str],
    source_snapshot_key: str,
    imported_at: str,
) -> dict[str, Any]:
    normalized = {
        "incident_key": f"{row['ID'].strip()}:{row['UPA'].strip()}",
        "incident_id": row["ID"].strip(),
        "upa": row["UPA"].strip(),
        "event_date": _date(row["EventDate"]),
        "employer": row["Employer"].strip(),
        "address1": _nullable(row.get("Address1")),
        "address2": _nullable(row.get("Address2")),
        "city": _nullable(row.get("City")),
        "state": row["State"].strip(),
        "zip_code": _nullable(row.get("Zip")),
        "latitude": _float(row.get("Latitude")),
        "longitude": _float(row.get("Longitude")),
        "primary_naics": _nullable(row.get("Primary NAICS")),
        "hospitalized": _integer(row.get("Hospitalized")),
        "amputation": _integer(row.get("Amputation")),
        "loss_of_eye": _integer(row.get("Loss of Eye")),
        "inspection": _nullable(row.get("Inspection")),
        "narrative": row["Final Narrative"].strip(),
        "nature_code": row["Nature"].strip(),
        "nature_title": _nullable(row.get("NatureTitle")),
        "body_part_code": row["Part of Body"].strip(),
        "body_part_title": _nullable(row.get("Part of Body Title")),
        "event_code": row["Event"].strip(),
        "event_title": _nullable(row.get("EventTitle")),
        "source_code": row["Source"].strip(),
        "source_title": _nullable(row.get("SourceTitle")),
        "secondary_source_code": _nullable(row.get("Secondary Source")),
        "secondary_source_title": _nullable(row.get("Secondary Source Title")),
        "federal_state": _integer(row["FederalState"]) if row.get("FederalState", "").strip() else None,
    }
    normalized["content_hash"] = _content_hash(normalized)
    normalized["source_snapshot_key"] = source_snapshot_key
    normalized["imported_at"] = imported_at
    return normalized


def import_sir_snapshot(
    connection: sqlite3.Connection,
    csv_path: Path,
    manifest_path: Path,
    changed_keys: list[str] | None = None,
) -> dict[str, int]:
    imported_at = _utc_now()
    manifest = _read_manifest(manifest_path)
    source_key = _register_snapshot(
        connection,
        "osha_sir",
        manifest_path.parent,
        manifest,
        imported_at,
    )
    existing = dict(connection.execute("SELECT incident_key, content_hash FROM osha_sir_incidents"))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    placeholders = ", ".join(f":{column}" for column in SIR_COLUMNS)
    updates = ", ".join(
        f"{column}=excluded.{column}"
        for column in SIR_COLUMNS
        if column != "incident_key"
    )
    sql = (
        f"INSERT INTO osha_sir_incidents({', '.join(SIR_COLUMNS)}) "
        f"VALUES ({placeholders}) ON CONFLICT(incident_key) DO UPDATE SET {updates}"
    )
    pending: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            record = _normalize_sir_row(row, source_key, imported_at)
            old_hash = existing.get(record["incident_key"])
            if old_hash == record["content_hash"]:
                counts["unchanged"] += 1
                continue
            counts["inserted" if old_hash is None else "updated"] += 1
            if changed_keys is not None:
                changed_keys.append(record["incident_key"])
            pending.append(record)
            existing[record["incident_key"]] = record["content_hash"]
            if len(pending) >= 1000:
                connection.executemany(sql, pending)
                pending.clear()
        if pending:
            connection.executemany(sql, pending)
    return counts


def import_news_snapshot(
    connection: sqlite3.Connection,
    entries_path: Path,
    manifest_path: Path,
    changed_keys: list[str] | None = None,
) -> dict[str, int]:
    imported_at = _utc_now()
    manifest = _read_manifest(manifest_path)
    source_key = _register_snapshot(
        connection,
        "osha_news",
        manifest_path.parent,
        manifest,
        imported_at,
    )
    existing = dict(connection.execute("SELECT entry_id, content_hash FROM osha_news_releases"))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with entries_path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            entry = json.loads(line)
            old_hash = existing.get(entry["entry_id"])
            if old_hash == entry["content_hash"]:
                counts["unchanged"] += 1
                continue
            counts["inserted" if old_hash is None else "updated"] += 1
            if changed_keys is not None:
                changed_keys.append(entry["entry_id"])
            connection.execute(
                """
                INSERT INTO osha_news_releases(
                    entry_id, source_id, title, url, published_at, summary,
                    content_hash, first_collected_at, last_collected_at,
                    source_snapshot_key, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    source_id=excluded.source_id,
                    title=excluded.title,
                    url=excluded.url,
                    published_at=excluded.published_at,
                    summary=excluded.summary,
                    content_hash=excluded.content_hash,
                    last_collected_at=excluded.last_collected_at,
                    source_snapshot_key=excluded.source_snapshot_key,
                    imported_at=excluded.imported_at
                """,
                (
                    entry["entry_id"], entry["source_id"], entry["title"], entry["url"],
                    entry.get("published_at"), entry.get("summary"), entry["content_hash"],
                    entry["collected_at"], entry["collected_at"], source_key, imported_at,
                ),
            )
    return counts


def _latest_sir_snapshot(base_dir: Path) -> tuple[Path, Path]:
    state = json.loads((base_dir / "state" / "osha_sir_state.json").read_text(encoding="utf-8"))
    snapshot_dir = base_dir / state["snapshot_directory"]
    csv_files = list(snapshot_dir.glob("*.csv"))
    if len(csv_files) != 1:
        raise ValueError(f"Expected one OSHA SIR CSV in {snapshot_dir}, found {len(csv_files)}")
    return csv_files[0], snapshot_dir / "manifest.json"


def import_current_sources(
    database_path: Path = DEFAULT_DATABASE_PATH,
    base_dir: Path = PROJECT_ROOT,
    replace: bool = False,
) -> dict[str, Any]:
    database_path = Path(database_path).resolve()
    base_dir = Path(base_dir).resolve()
    if replace:
        database_path.unlink(missing_ok=True)
    connection = _connect(database_path)
    sir_changed: list[str] = []
    news_changed: list[str] = []
    try:
        with connection:
            csv_path, sir_manifest = _latest_sir_snapshot(base_dir)
            sir_counts = import_sir_snapshot(
                connection,
                csv_path,
                sir_manifest,
                changed_keys=sir_changed,
            )
            news_counts = {"inserted": 0, "updated": 0, "unchanged": 0}
            for entries_path in sorted((base_dir / "data" / "raw" / "osha_news").glob("*/entries.jsonl")):
                result = import_news_snapshot(
                    connection,
                    entries_path,
                    entries_path.parent / "manifest.json",
                    changed_keys=news_changed,
                )
                for key in news_counts:
                    news_counts[key] += result[key]
    finally:
        connection.close()
    return {
        "status": "imported",
        "database": str(database_path),
        "osha_sir": sir_counts,
        "osha_news": news_counts,
        "changed": {
            "osha_sir": sorted(set(sir_changed)),
            "osha_news": sorted(set(news_changed)),
        },
        "summary": database_summary(database_path),
    }


def database_summary(database_path: Path = DEFAULT_DATABASE_PATH) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    try:
        sir_count = connection.execute("SELECT COUNT(*) FROM osha_sir_incidents").fetchone()[0]
        news_count = connection.execute("SELECT COUNT(*) FROM osha_news_releases").fetchone()[0]
        date_range = connection.execute(
            "SELECT MIN(event_date), MAX(event_date) FROM osha_sir_incidents"
        ).fetchone()
        snapshots = connection.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
    finally:
        connection.close()
    return {
        "osha_sir_incidents": sir_count,
        "osha_news_releases": news_count,
        "source_snapshots": snapshots,
        "sir_date_range": {"from": date_range[0], "to": date_range[1]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把已下载的 OSHA 数据导入 SQLite。")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--replace", action="store_true", help="重建自动生成的事故数据库")
    args = parser.parse_args()
    print(
        json.dumps(
            import_current_sources(args.database, replace=args.replace),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def show_main() -> int:
    parser = argparse.ArgumentParser(description="查看 OSHA 事故数据库统计。")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    if not args.database.exists():
        parser.error(f"事故数据库不存在：{args.database}")
    print(json.dumps(database_summary(args.database), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
