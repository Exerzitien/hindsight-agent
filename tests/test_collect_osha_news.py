from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hindsight_agent.tools.collect_osha import CollectionError
from hindsight_agent.tools.collect_osha_news import (
    OSHA_NEWS_FEED,
    OshaNewsCollector,
    parse_osha_news_feed,
)


def make_feed(*items: tuple[str, str, str]) -> bytes:
    item_xml = "".join(
        f"""
        <item>
          <title>{title}</title>
          <link>{url}</link>
          <guid>{url}</guid>
          <description>September 1, 2026</description>
          <pubDate>{published}</pubDate>
        </item>
        """
        for title, url, published in items
    )
    return f"<rss version=\"2.0\"><channel>{item_xml}</channel></rss>".encode()


ITEM_A = (
    "OSHA release A",
    "https://www.osha.gov/news/newsreleases/test/20260901",
    "Tue, 01 Sep 2026 12:00:00 +0000",
)
ITEM_B = (
    "OSHA release B",
    "https://www.osha.gov/news/newsreleases/test/20260902",
    "Wed, 02 Sep 2026 12:00:00 +0000",
)


class FakeResponse:
    def __init__(self, content: bytes, url: str = OSHA_NEWS_FEED):
        self.content = content
        self.url = url

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, content: bytes):
        self.content = content

    def get(self, url: str, **kwargs) -> FakeResponse:
        return FakeResponse(self.content)


def collector(tmp_path: Path, feed: bytes) -> OshaNewsCollector:
    return OshaNewsCollector(
        base_dir=tmp_path,
        session=FakeSession(feed),
        clock=lambda: datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc),
    )


def test_parses_feed_into_stable_entry() -> None:
    entries = parse_osha_news_feed(make_feed(ITEM_A), "2026-09-01T03:00:00Z")

    assert len(entries) == 1
    assert entries[0]["source"] == "osha_news_releases"
    assert entries[0]["published_at"] == "2026-09-01T12:00:00Z"
    assert len(entries[0]["entry_id"]) == 64


def test_first_collection_preserves_snapshot_and_marks_all_new(tmp_path: Path) -> None:
    result = collector(tmp_path, make_feed(ITEM_A, ITEM_B)).collect()

    assert result["status"] == "downloaded"
    assert result["entry_count"] == 2
    assert result["new_count"] == 2
    snapshot = tmp_path / result["snapshot_directory"]
    assert (snapshot / "feed.xml").exists()
    assert len((snapshot / "entries.jsonl").read_text().splitlines()) == 2


def test_identical_feed_returns_no_change(tmp_path: Path) -> None:
    feed = make_feed(ITEM_A)
    collector(tmp_path, feed).collect()
    result = collector(tmp_path, feed).collect()

    assert result["status"] == "no_change"
    assert result["new_count"] == 0
    assert len(list((tmp_path / "data" / "raw" / "osha_news").iterdir())) == 1


def test_changed_feed_reports_new_and_updated_entries(tmp_path: Path) -> None:
    collector(tmp_path, make_feed(ITEM_A)).collect()
    changed_a = (ITEM_A[0] + " corrected", ITEM_A[1], ITEM_A[2])

    result = collector(tmp_path, make_feed(changed_a, ITEM_B)).collect()

    assert result["new_count"] == 1
    assert result["updated_count"] == 1
    assert result["new_entries"][0]["title"] == ITEM_B[0]
    state = json.loads((tmp_path / "state" / "osha_news_state.json").read_text())
    assert len(state["known_entries"]) == 2


def test_invalid_xml_does_not_create_state(tmp_path: Path) -> None:
    with pytest.raises(CollectionError, match="not valid XML"):
        collector(tmp_path, b"<rss><broken>").collect()

    assert not (tmp_path / "state" / "osha_news_state.json").exists()

