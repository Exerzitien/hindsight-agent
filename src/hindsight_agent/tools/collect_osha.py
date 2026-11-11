"""Download and preserve the official OSHA Severe Injury Report dataset."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from strands import tool


OSHA_SIR_PAGE = "https://www.osha.gov/severe-injury-reports"
from ..config import PROJECT_ROOT
#: osha.gov 的边缘防护会对不带 Mozilla 记号的 User-Agent 直接回 403，
#: 于是「clone 完跑采集」这一步在任何新机器上都是失败的。这里用的是
#: 爬虫惯用的 compatible 写法：仍然如实自报家门（名字、版本、来源），
#: 只是把过滤器要求的记号带上——不伪装成浏览器。
USER_AGENT = (
    "Mozilla/5.0 (compatible; Hindsight-Agent/0.2; "
    "+https://github.com/hindsight-agent/hindsight-agent)"
)


class CollectionError(RuntimeError):
    """Raised when a collection cannot be completed and audited safely."""


def find_full_dataset_url(html: str, page_url: str = OSHA_SIR_PAGE) -> str:
    """Find and validate the official full SIR dataset link in an OSHA page."""
    soup = BeautifulSoup(html, "html.parser")

    for link in soup.select("a[href]"):
        label = " ".join(link.get_text(" ", strip=True).split()).casefold()
        if "download the full sir data set" not in label:
            continue

        download_url = urljoin(page_url, str(link["href"]))
        _validate_osha_url(download_url)
        return download_url

    raise CollectionError("Could not find the full OSHA SIR dataset link.")


def _validate_osha_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        hostname == "osha.gov" or hostname.endswith(".osha.gov")
    ):
        raise CollectionError(f"Refusing non-OSHA download URL: {url}")


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"Cannot read collection state: {path}") from exc

    if not isinstance(state, dict):
        raise CollectionError(f"Collection state must be a JSON object: {path}")
    return state


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _safe_extract(archive_path: Path, destination: Path) -> list[str]:
    destination_root = destination.resolve()
    extracted_files: list[str] = []

    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise CollectionError("Downloaded OSHA file is not a valid ZIP archive.") from exc

    with archive:
        for member in archive.infolist():
            member_path = Path(member.filename)
            unix_mode = (member.external_attr >> 16) & 0o170000
            if member_path.is_absolute() or ".." in member_path.parts:
                raise CollectionError(f"Unsafe ZIP member path: {member.filename}")
            if unix_mode == stat.S_IFLNK:
                raise CollectionError(f"ZIP symlinks are not allowed: {member.filename}")

            target = (destination / member_path).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise CollectionError(f"ZIP member escapes snapshot directory: {member.filename}")

            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted_files.append(member_path.as_posix())

    return extracted_files


class OshaSirCollector:
    """Deterministic collector used by both the CLI and the Strands tool."""

    def __init__(
        self,
        base_dir: Path = PROJECT_ROOT,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.raw_dir = self.base_dir / "data" / "raw" / "osha_sir"
        self.state_file = self.base_dir / "state" / "osha_sir_state.json"
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self) -> dict[str, Any]:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": USER_AGENT}

        try:
            page_response = self.session.get(
                OSHA_SIR_PAGE,
                headers=headers,
                timeout=30,
            )
            page_response.raise_for_status()
            download_url = find_full_dataset_url(page_response.text)

            download_response = self.session.get(
                download_url,
                headers=headers,
                timeout=180,
                stream=True,
            )
            download_response.raise_for_status()
            final_url = getattr(download_response, "url", None) or download_url
            _validate_osha_url(final_url)
        except requests.RequestException as exc:
            raise CollectionError(f"OSHA request failed: {exc}") from exc

        temporary_path = self._download_to_temporary_file(download_response)
        try:
            dataset_hash = self._sha256(temporary_path)
            file_size = temporary_path.stat().st_size
            old_state = _load_state(self.state_file)

            if old_state.get("sha256") == dataset_hash:
                return {
                    "status": "no_change",
                    "message": "OSHA SIR dataset has not changed.",
                    "sha256": dataset_hash,
                    "bytes": file_size,
                    "source_page": OSHA_SIR_PAGE,
                    "download_url": final_url,
                    "existing_snapshot": old_state.get("snapshot_directory"),
                }

            return self._preserve_snapshot(
                temporary_path=temporary_path,
                dataset_hash=dataset_hash,
                file_size=file_size,
                download_url=final_url,
            )
        finally:
            temporary_path.unlink(missing_ok=True)

    def _download_to_temporary_file(self, response: Any) -> Path:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".osha-sir-",
            suffix=".zip.part",
            dir=self.raw_dir,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        try:
            with temporary_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        if temporary_path.stat().st_size == 0:
            temporary_path.unlink(missing_ok=True)
            raise CollectionError("OSHA returned an empty dataset file.")
        return temporary_path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _preserve_snapshot(
        self,
        temporary_path: Path,
        dataset_hash: str,
        file_size: int,
        download_url: str,
    ) -> dict[str, Any]:
        collected_at = self.clock().astimezone(timezone.utc)
        timestamp = collected_at.strftime("%Y%m%dT%H%M%SZ")
        snapshot_id = f"{timestamp}-{dataset_hash[:12]}"
        snapshot_dir = self.raw_dir / snapshot_id
        zip_path = snapshot_dir / "osha_sir.zip"

        if snapshot_dir.exists():
            raise CollectionError(f"Snapshot directory already exists: {snapshot_dir}")

        snapshot_dir.mkdir(parents=True)
        try:
            shutil.copyfile(temporary_path, zip_path)
            extracted_files = _safe_extract(zip_path, snapshot_dir)
            snapshot_relative = snapshot_dir.relative_to(self.base_dir).as_posix()
            manifest = {
                "source_page": OSHA_SIR_PAGE,
                "download_url": download_url,
                "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
                "sha256": dataset_hash,
                "bytes": file_size,
                "snapshot_directory": snapshot_relative,
                "archive": "osha_sir.zip",
                "extracted_files": extracted_files,
            }
            _write_json_atomic(snapshot_dir / "manifest.json", manifest)
        except Exception:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        _write_json_atomic(self.state_file, manifest)
        return {
            "status": "downloaded",
            "message": "New OSHA SIR dataset downloaded and preserved.",
            **manifest,
        }


def collect_osha_sir_impl(base_dir: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Collect OSHA SIR data without going through an LLM."""
    return OshaSirCollector(base_dir=base_dir).collect()


@tool
def collect_osha_sir() -> dict[str, Any]:
    """Download and preserve the latest official OSHA Severe Injury Report dataset.

    Use this tool only for collecting the official full OSHA SIR dataset. It reports
    whether a new immutable snapshot was saved or the published dataset was unchanged.
    """
    return collect_osha_sir_impl()

