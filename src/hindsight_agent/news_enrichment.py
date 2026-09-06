"""Fetch full text for newly observed OSHA news releases."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .rag_db import connect, utc_now


def extract_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("script, style, nav, footer, header, form, aside"):
        element.decompose()
    main = soup.select_one("main") or soup.select_one("article") or soup.body
    if main is None:
        return ""
    lines = [" ".join(part.split()) for part in main.stripped_strings]
    return "\n".join(line for line in lines if line)


def enrich_news_entries(
    database_path: Path,
    entry_ids: list[str],
    session: requests.Session | None = None,
) -> dict[str, object]:
    if not entry_ids:
        return {"requested": 0, "fetched": 0, "failed": []}
    session = session or requests.Session()
    connection = connect(database_path)
    placeholders = ",".join("?" for _ in entry_ids)
    try:
        entries = connection.execute(
            f"SELECT entry_id, url FROM osha_news_releases WHERE entry_id IN ({placeholders})",
            entry_ids,
        ).fetchall()
        fetched = 0
        failed: list[dict[str, str]] = []
        for entry in entries:
            parsed = urlparse(entry["url"])
            if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("osha.gov"):
                failed.append({"entry_id": entry["entry_id"], "error": "non-OSHA URL"})
                continue
            try:
                response = session.get(
                    entry["url"],
                    timeout=(15, 45),
                    headers={"User-Agent": "Hindsight-Agent/0.2 (+safety-research)"},
                )
                response.raise_for_status()
                body = extract_article_text(response.text)
                if len(body) < 80:
                    raise ValueError("article text was unexpectedly short")
                body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                with connection:
                    connection.execute(
                        """
                        INSERT INTO osha_news_documents(
                            entry_id, source_url, body_text, body_hash, fetched_at, parse_method
                        ) VALUES (?, ?, ?, ?, ?, 'html-main-v1')
                        ON CONFLICT(entry_id) DO UPDATE SET
                            source_url=excluded.source_url,
                            body_text=excluded.body_text,
                            body_hash=excluded.body_hash,
                            fetched_at=excluded.fetched_at,
                            parse_method=excluded.parse_method
                        """,
                        (entry["entry_id"], entry["url"], body, body_hash, utc_now()),
                    )
                fetched += 1
            except (requests.RequestException, ValueError) as exc:
                failed.append({"entry_id": entry["entry_id"], "error": str(exc)})
    finally:
        connection.close()
    return {"requested": len(entry_ids), "fetched": fetched, "failed": failed}
