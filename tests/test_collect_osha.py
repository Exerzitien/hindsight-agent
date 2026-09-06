from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hindsight_agent.tools.collect_osha import (
    CollectionError,
    OSHA_SIR_PAGE,
    OshaSirCollector,
    find_full_dataset_url,
)


DOWNLOAD_URL = "https://www.osha.gov/sites/default/files/example.zip"
PAGE_HTML = f'<html><a href="{DOWNLOAD_URL}">Download the full SIR data set</a></html>'


class FakeResponse:
    def __init__(self, *, text: str = "", body: bytes = b"", url: str = OSHA_SIR_PAGE):
        self.text = text
        self.body = body
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[str] = []

    def get(self, url: str, **kwargs):
        self.requests.append(url)
        return self.responses.pop(0)


def make_zip(filename: str = "sir.csv", contents: bytes = b"id,event\n1,test\n") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, contents)
    return output.getvalue()


def collector(tmp_path: Path, responses: list[FakeResponse]) -> OshaSirCollector:
    return OshaSirCollector(
        base_dir=tmp_path,
        session=FakeSession(responses),
        clock=lambda: datetime(2026, 9, 1, 2, 15, tzinfo=timezone.utc),
    )


def response_pair(dataset: bytes) -> list[FakeResponse]:
    return [
        FakeResponse(text=PAGE_HTML),
        FakeResponse(body=dataset, url=DOWNLOAD_URL),
    ]


def test_finds_relative_full_dataset_link() -> None:
    html = '<a href="/sites/default/files/sir.zip"> Download the full SIR data set </a>'
    assert find_full_dataset_url(html) == "https://www.osha.gov/sites/default/files/sir.zip"


def test_rejects_download_link_outside_osha() -> None:
    html = '<a href="https://example.com/sir.zip">Download the full SIR data set</a>'
    with pytest.raises(CollectionError, match="non-OSHA"):
        find_full_dataset_url(html)


def test_downloads_extracts_and_writes_audit_state(tmp_path: Path) -> None:
    dataset = make_zip()
    result = collector(tmp_path, response_pair(dataset)).collect()

    assert result["status"] == "downloaded"
    snapshot = tmp_path / result["snapshot_directory"]
    assert (snapshot / "osha_sir.zip").read_bytes() == dataset
    assert (snapshot / "sir.csv").read_bytes() == b"id,event\n1,test\n"
    assert json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))["sha256"]
    assert json.loads((tmp_path / "state" / "osha_sir_state.json").read_text())["sha256"]


def test_same_dataset_returns_no_change_without_new_snapshot(tmp_path: Path) -> None:
    dataset = make_zip()
    first = collector(tmp_path, response_pair(dataset)).collect()
    second = collector(tmp_path, response_pair(dataset)).collect()

    assert first["status"] == "downloaded"
    assert second["status"] == "no_change"
    snapshots = [path for path in (tmp_path / "data" / "raw" / "osha_sir").iterdir() if path.is_dir()]
    assert len(snapshots) == 1


def test_rejects_zip_path_traversal_and_does_not_write_state(tmp_path: Path) -> None:
    dataset = make_zip("../escaped.csv", b"unsafe")

    with pytest.raises(CollectionError, match="Unsafe ZIP member"):
        collector(tmp_path, response_pair(dataset)).collect()

    assert not (tmp_path / "escaped.csv").exists()
    assert not (tmp_path / "state" / "osha_sir_state.json").exists()
    snapshots = [path for path in (tmp_path / "data" / "raw" / "osha_sir").iterdir() if path.is_dir()]
    assert snapshots == []

