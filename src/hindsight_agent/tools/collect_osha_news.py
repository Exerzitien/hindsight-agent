"""Collect newly published releases from the official OSHA RSS feed."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from strands import tool

from .collect_osha import (
    PROJECT_ROOT,
    USER_AGENT,
    CollectionError,
    _validate_osha_url,
    _write_json_atomic,
)


OSHA_NEWS_FEED = "https://www.osha.gov/news/newsreleases.xml"
MAX_FEED_BYTES = 5 * 1024 * 1024


def _text(element: ElementTree.Element, name: str) -> str:
    child = element.find(name)
    return (child.text or "").strip() if child is not None else ""


def _parse_publication_date(value: str) -> str | None:
    if not value:
        return None
    try:
        published_at = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise CollectionError(f"Invalid RSS publication date: {value}") from exc

    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_osha_news_feed(xml_bytes: bytes, collected_at: str) -> list[dict[str, Any]]:
    """Convert OSHA RSS items into stable, source-preserving dictionaries."""
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise CollectionError("OSHA news feed is not valid XML.") from exc

    if root.tag.casefold() != "rss":
        raise CollectionError(f"Unexpected OSHA feed root element: {root.tag}")

    entries: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid")
        published_raw = _text(item, "pubDate")
        description_html = _text(item, "description")

        if not title or not link:
            raise CollectionError("OSHA RSS item is missing title or link.")
        _validate_osha_url(link)

        source_id = guid or link
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)

        summary = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)
        canonical = json.dumps(
            {
                "source_id": source_id,
                "title": title,
                "url": link,
                "published_at": published_raw,
                "summary": summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        content_hash = hashlib.sha256(canonical).hexdigest()
        entries.append(
            {
                "schema_version": "1.0",
                "source": "osha_news_releases",
                "source_id": source_id,
                "entry_id": hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
                "title": title,
                "url": link,
                "published_at": _parse_publication_date(published_raw),
                "summary": summary,
                "content_hash": content_hash,
                "collected_at": collected_at,
            }
        )

    if not entries:
        raise CollectionError("OSHA news feed contains no entries.")
    return entries


class OshaNewsCollector:
    """Preserve OSHA RSS snapshots and identify unseen or changed releases."""

    def __init__(
        self,
        base_dir: Path = PROJECT_ROOT,
        session: requests.Session | None = None,
        clock: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.raw_dir = self.base_dir / "data" / "raw" / "osha_news"
        self.state_file = self.base_dir / "state" / "osha_news_state.json"
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self) -> dict[str, Any]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            response = self.session.get(
                OSHA_NEWS_FEED,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CollectionError(f"OSHA news RSS request failed: {exc}") from exc

        final_url = getattr(response, "url", None) or OSHA_NEWS_FEED
        _validate_osha_url(final_url)
        xml_bytes = response.content
        if not xml_bytes:
            raise CollectionError("OSHA news feed is empty.")
        if len(xml_bytes) > MAX_FEED_BYTES:
            raise CollectionError("OSHA news feed exceeds the 5 MB safety limit.")

        feed_hash = hashlib.sha256(xml_bytes).hexdigest()
        old_state = self._load_state()
        if old_state.get("feed_sha256") == feed_hash:
            return {
                "status": "no_change",
                "message": "OSHA news feed has not changed.",
                "feed_sha256": feed_hash,
                "new_count": 0,
                "updated_count": 0,
                "source": final_url,
                "existing_snapshot": old_state.get("snapshot_directory"),
                "new_entries": [],
            }

        collected_at_dt = self.clock().astimezone(timezone.utc)
        collected_at = collected_at_dt.isoformat().replace("+00:00", "Z")
        entries = parse_osha_news_feed(xml_bytes, collected_at)
        known_entries = old_state.get("known_entries", {})
        if not isinstance(known_entries, dict):
            raise CollectionError("OSHA news state has invalid known_entries data.")

        new_entries = [entry for entry in entries if entry["source_id"] not in known_entries]
        updated_entries = [
            entry
            for entry in entries
            if entry["source_id"] in known_entries
            and known_entries[entry["source_id"]] != entry["content_hash"]
        ]
        known_entries.update({entry["source_id"]: entry["content_hash"] for entry in entries})

        timestamp = collected_at_dt.strftime("%Y%m%dT%H%M%SZ")
        snapshot_dir = self.raw_dir / f"{timestamp}-{feed_hash[:12]}"
        if snapshot_dir.exists():
            raise CollectionError(f"OSHA news snapshot already exists: {snapshot_dir}")

        snapshot_dir.mkdir(parents=True)
        try:
            (snapshot_dir / "feed.xml").write_bytes(xml_bytes)
            entries_path = snapshot_dir / "entries.jsonl"
            entries_path.write_text(
                "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
                encoding="utf-8",
            )
            snapshot_relative = snapshot_dir.relative_to(self.base_dir).as_posix()
            manifest = {
                "source": final_url,
                "collected_at": collected_at,
                "feed_sha256": feed_hash,
                "snapshot_directory": snapshot_relative,
                "entry_count": len(entries),
                "new_count": len(new_entries),
                "updated_count": len(updated_entries),
                "files": ["feed.xml", "entries.jsonl"],
            }
            _write_json_atomic(snapshot_dir / "manifest.json", manifest)
            state = {
                **manifest,
                "known_entries": known_entries,
            }
            _write_json_atomic(self.state_file, state)
        except Exception:
            import shutil

            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        return {
            "status": "downloaded",
            "message": "OSHA news feed snapshot preserved.",
            **manifest,
            "new_entries": new_entries,
            "updated_entries": updated_entries,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectionError(f"Cannot read OSHA news state: {self.state_file}") from exc
        if not isinstance(state, dict):
            raise CollectionError("OSHA news state must be a JSON object.")
        return state


def collect_osha_news_impl(base_dir: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Collect OSHA news RSS without going through an LLM."""
    return OshaNewsCollector(base_dir=base_dir).collect()


@tool
def collect_osha_news() -> dict[str, Any]:
    """Collect newly published releases from the official OSHA news RSS feed.

    The tool preserves the raw feed, normalizes every current item, and reports
    entries that have not been observed in earlier successful collections.
    """
    return collect_osha_news_impl()

