"""G1 collectors: download the grounding corpus once, then run offline.

Three adapters, all of which freeze raw bytes and a SHA-256 before any parsing:

``collect_cfr_sections_impl``
    29 CFR sections from the eCFR versioner API. The issue date is read from
    the API rather than assumed, so a run is reproducible against a named
    edition of the regulation.

``collect_fatal_facts_impl``
    OSHA Fatal Facts case sheets from the official publications listing,
    including the Spanish editions OSHA publishes for some cases. Those are
    what G3 cites when it renders the Spanish toolbox talk.

``ingest_face_reports_impl``
    NIOSH FACE investigation reports read from a local directory. CDC's edge
    blocks automated download from many networks, so FACE is deliberately a
    manual-drop adapter: place the report files yourself and this ingests them
    with the same snapshot-and-citation discipline as the other two.

Citation policy: a CFR passage is cited at section level, which is always
correct, and carries the paragraph marker exactly as printed. This module never
assembles a hierarchical paragraph path such as ``(a)(3)(ii)`` from position,
because a mis-assembled legal citation is precisely the failure this project
exists to prevent.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from strands import tool

from ..corpus_db import (
    SOURCE_CFR,
    SOURCE_FACE,
    SOURCE_FATAL_FACTS,
    ensure_corpus_schema,
    record_snapshot,
    upsert_document,
)
from ..matching import HAZARD_TERMS

PROJECT_ROOT = Path(__file__).resolve().parents[3]
USER_AGENT = "Hindsight-Agent/0.1 (grounding corpus collector)"
REQUEST_TIMEOUT = 60

ECFR_API = "https://www.ecfr.gov/api/versioner/v1"
ECFR_TITLE = 29
ECFR_SUBTITLE = "B"
ECFR_CHAPTER = "XVII"
FATAL_FACTS_PAGE = "https://www.osha.gov/publications/fatal-facts"

#: OSHA general-industry sections that cover the hazards a metal-fabrication
#: site actually registers. Extend deliberately; every added section is text a
#: safety manager may later be asked to defend.
DEFAULT_CFR_SECTIONS = (
    "1910.147",  # The control of hazardous energy (lockout/tagout)
    "1910.176",  # Handling materials - general
    "1910.178",  # Powered industrial trucks
    "1910.179",  # Overhead and gantry cranes
    "1910.212",  # General requirements for all machines
    "1910.217",  # Mechanical power presses
    "1910.219",  # Mechanical power-transmission apparatus
    "1910.252",  # Welding, cutting and brazing - general requirements
)

_LEADING_MARKER = re.compile(r"^\s*((?:\([0-9a-zA-Z]{1,4}\)\s*)+)")
_WS = re.compile(r"\s+")


class GroundingError(RuntimeError):
    """Raised when corpus material cannot be collected and audited safely."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _require_host(url: str, *hosts: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(
        host == h or host.endswith("." + h) for h in hosts
    ):
        raise GroundingError(f"Refusing download from unexpected host: {url}")


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return session


def tag_hazards(text: str) -> list[str]:
    """Tag a passage with the project's existing hazard vocabulary.

    Reuses ``matching.HAZARD_TERMS`` so the corpus, the matcher and the factory
    risk register all speak one vocabulary instead of three.
    """
    folded = text.casefold()
    return sorted(
        hazard_id
        for hazard_id, terms in HAZARD_TERMS.items()
        if any(term in folded for term in terms)
    )


def tag_equipment(text: str, equipment_terms: dict[str, list[str]] | None) -> list[str]:
    """Tag a passage with site equipment ids, using terms read from the factory DB."""
    if not equipment_terms:
        return []
    folded = text.casefold()
    return sorted(
        equipment_id
        for equipment_id, terms in equipment_terms.items()
        if any(term in folded for term in terms)
    )


def load_equipment_terms(factory_database: Path) -> dict[str, list[str]]:
    """Build equipment match terms from the factory's own names and types.

    Nothing is invented here: the terms are the equipment name and type strings
    the site already approved in its asset register.
    """
    import sqlite3

    if not Path(factory_database).exists():
        return {}
    connection = sqlite3.connect(str(factory_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, name, equipment_type FROM equipment WHERE active = 1"
        ).fetchall()
    finally:
        connection.close()

    terms: dict[str, list[str]] = {}
    for row in rows:
        candidates = {
            str(row["name"] or "").casefold(),
            str(row["equipment_type"] or "").casefold(),
        }
        keep = [c for c in candidates if len(c) >= 4]
        if keep:
            terms[row["id"]] = sorted(keep)
    return terms


# --------------------------------------------------------------------------
# G1a - 29 CFR via the eCFR versioner API
# --------------------------------------------------------------------------

def latest_cfr_issue_date(session: requests.Session | None = None) -> str:
    """Read title 29's latest issue date from the API instead of assuming one."""
    session = session or _session()
    response = session.get(f"{ECFR_API}/titles.json", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    for entry in response.json().get("titles", []):
        if entry.get("number") == ECFR_TITLE:
            issue_date = entry.get("latest_issue_date")
            if not issue_date:
                raise GroundingError("eCFR did not report a latest issue date for title 29.")
            return str(issue_date)
    raise GroundingError("eCFR titles response did not contain title 29.")


def parse_cfr_section(xml_bytes: bytes, section: str) -> dict[str, Any]:
    """Split one CFR section into individually citable paragraphs."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise GroundingError(f"eCFR returned unparseable XML for {section}") from exc

    heading = ""
    for element in root.iter():
        if element.tag in {"HEAD", "SUBJECT"}:
            heading = _clean(" ".join(element.itertext()))
            break

    passages: list[dict[str, Any]] = []
    for element in root.iter("P"):
        text = _clean(" ".join(element.itertext()))
        if not text:
            continue
        match = _LEADING_MARKER.match(text)
        marker = _clean(match.group(1)) if match else ""
        passages.append(
            {
                "section_label": marker or "(text)",
                "section_role": "REQUIREMENT",
                "citation_label": f"29 CFR {section}",
                "text": text,
                "hazard_tags": tag_hazards(text),
            }
        )
    if not passages:
        raise GroundingError(f"No paragraphs found in eCFR response for {section}")
    return {"heading": heading or f"29 CFR {section}", "passages": passages}


def collect_cfr_sections_impl(
    sections: tuple[str, ...] = DEFAULT_CFR_SECTIONS,
    *,
    corpus_database: Path | None = None,
    snapshot_root: Path | None = None,
    issue_date: str | None = None,
    factory_database: Path | None = None,
) -> dict[str, Any]:
    corpus_database = Path(corpus_database or PROJECT_ROOT / "data" / "corpus" / "grounding.db")
    snapshot_root = Path(snapshot_root or PROJECT_ROOT / "data" / "corpus" / "snapshots")
    ensure_corpus_schema(corpus_database)

    session = _session()
    issue_date = issue_date or latest_cfr_issue_date(session)
    equipment_terms = load_equipment_terms(factory_database) if factory_database else {}
    stamp = _utc_stamp()
    imported: list[dict[str, Any]] = []

    for section in sections:
        url = (
            f"{ECFR_API}/full/{issue_date}/title-{ECFR_TITLE}.xml"
            f"?subtitle={ECFR_SUBTITLE}&chapter={ECFR_CHAPTER}"
            f"&part={section.split('.')[0]}&section={section}"
        )
        _require_host(url, "ecfr.gov")
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise GroundingError(
                f"eCFR returned HTTP {response.status_code} for {section} ({issue_date})"
            )

        snapshot_key = f"cfr:{section}:{issue_date}"
        local_path = snapshot_root / "cfr" / issue_date / f"{section}.xml"
        record_snapshot(
            corpus_database,
            snapshot_key=snapshot_key,
            source_type=SOURCE_CFR,
            source_url=url,
            payload=response.content,
            local_path=local_path,
            http_status=response.status_code,
        )

        parsed = parse_cfr_section(response.content, section)
        for passage in parsed["passages"]:
            passage["equipment_tags"] = tag_equipment(passage["text"], equipment_terms)

        result = upsert_document(
            corpus_database,
            document_id=f"cfr:{section}",
            source_type=SOURCE_CFR,
            source_key=section,
            title=parsed["heading"],
            publisher="U.S. Government Publishing Office (eCFR)",
            official_url=f"https://www.ecfr.gov/current/title-29/section-{section}",
            snapshot_key=snapshot_key,
            passages=parsed["passages"],
            published_on=issue_date,
            language="en",
            edition=f"eCFR issue {issue_date}",
        )
        imported.append({"section": section, **result})

    return {
        "source_type": SOURCE_CFR,
        "issue_date": issue_date,
        "collected_at": stamp,
        "documents": imported,
        "total_passages": sum(item["passages"] for item in imported),
    }


# --------------------------------------------------------------------------
# G1b - OSHA Fatal Facts case sheets
# --------------------------------------------------------------------------

#: Section headings actually printed across three generations of Fatal Facts
#: sheets (2012, 2013-2023, 2025) in both English and Spanish, each mapped to a
#: canonical role. Every string here was read off a published PDF; none is a
#: guess at wording OSHA might use. Roles let G2 and G3 ask for "the prevention
#: section" without caring which edition or language a sheet is.
FF_HEADING_ROLES: tuple[tuple[str, str], ...] = (
    # --- English ---
    ("INCIDENT SUMMARY", "SUMMARY"),
    ("ACCIDENT SUMMARY", "SUMMARY"),
    ("BRIEF DESCRIPTION OF INCIDENT", "DESCRIPTION"),
    ("BRIEF DESCRIPTION OF ACCIDENT", "DESCRIPTION"),
    ("DESCRIPTION OF INCIDENT", "DESCRIPTION"),
    ("Likely Cause of Incident", "CAUSE"),
    ("FINDINGS", "CAUSE"),
    ("INCIDENT PREVENTION", "PREVENTION"),
    ("ACCIDENT PREVENTION", "PREVENTION"),
    ("HOW TO PREVENT THESE INCIDENTS", "PREVENTION"),
    # --- Spanish ---
    ("BREVE DESCRIPCIÓN DEL INCIDENTE", "DESCRIPTION"),
    ("DESCRIPCIÓN DEL INCIDENTE", "DESCRIPTION"),
    ("CAUSAS PROBABLES", "CAUSE"),
    ("HALLAZGOS", "CAUSE"),
    ("PREVENCIÓN DE INCIDENTES", "PREVENTION"),
    ("CÓMO PREVENIR ESTOS INCIDENTES", "PREVENTION"),
)
_FF_TITLE = re.compile(r"^No\.?\s*([0-9]+)\s*-?\s*(\d{4})\s*-\s*(.+?)\s*(?:\(\s*Espa.?ol\s*\))?$")


def parse_fatal_facts_listing(html: str, page_url: str = FATAL_FACTS_PAGE) -> list[dict[str, Any]]:
    """Read the official listing into English entries with optional Spanish editions."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("div.col-md-8") or soup
    entries: list[dict[str, Any]] = []

    for paragraph in container.find_all("p"):
        links = [a for a in paragraph.select("a[href]") if a["href"].lower().endswith(".pdf")]
        if not links:
            continue
        label = _clean(paragraph.get_text(" ", strip=True))
        match = _FF_TITLE.match(label)
        if not match:
            continue
        number, year, title = match.group(1), match.group(2), _clean(match.group(3))

        english_url = spanish_url = None
        for anchor in links:
            href = urljoin(page_url, str(anchor["href"]))
            if "español" in _clean(anchor.get_text()).casefold():
                spanish_url = href
            elif english_url is None:
                english_url = href
        if not english_url:
            continue
        entries.append(
            {
                "number": number,
                "year": year,
                "title": title,
                "english_url": english_url,
                "spanish_url": spanish_url,
            }
        )
    return entries


def split_fatal_facts_text(text: str) -> list[dict[str, str]]:
    """Split a case sheet into its published sections, tagged with a role.

    Headings are matched longest-first so ``BREVE DESCRIPCIÓN DEL INCIDENTE``
    wins over the ``DESCRIPCIÓN DEL INCIDENTE`` it contains. Text outside any
    recognised heading is kept as ``FRONT MATTER`` rather than dropped, so
    nothing quotable disappears silently.
    """
    candidates: list[tuple[int, int, str, str]] = []
    for heading, role in FF_HEADING_ROLES:
        start = 0
        while True:
            index = text.find(heading, start)
            if index < 0:
                break
            candidates.append((index, len(heading), heading, role))
            start = index + 1

    # Longest heading wins at any given position; no two headings may overlap.
    candidates.sort(key=lambda item: (item[0], -item[1]))
    accepted: list[tuple[int, int, str, str]] = []
    cursor = -1
    for index, length, heading, role in candidates:
        if index < cursor:
            continue
        accepted.append((index, length, heading, role))
        cursor = index + length

    if not accepted:
        body = _clean(text)
        return [{"section_label": "FULL TEXT", "section_role": "OTHER", "text": body}] if body else []

    sections: list[dict[str, str]] = []
    front = _clean(text[: accepted[0][0]])
    if front:
        sections.append({"section_label": "FRONT MATTER", "section_role": "OTHER", "text": front})
    for position, (index, length, heading, role) in enumerate(accepted):
        end = accepted[position + 1][0] if position + 1 < len(accepted) else len(text)
        body = _clean(text[index + length : end])
        if body:
            sections.append({"section_label": heading, "section_role": role, "text": body})
    return sections


def _pdf_text(payload: bytes, scratch: Path) -> str:
    from pypdf import PdfReader

    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(payload)
    reader = PdfReader(str(scratch))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def collect_fatal_facts_impl(
    *,
    corpus_database: Path | None = None,
    snapshot_root: Path | None = None,
    limit: int | None = None,
    include_spanish: bool = True,
    factory_database: Path | None = None,
) -> dict[str, Any]:
    corpus_database = Path(corpus_database or PROJECT_ROOT / "data" / "corpus" / "grounding.db")
    snapshot_root = Path(snapshot_root or PROJECT_ROOT / "data" / "corpus" / "snapshots")
    ensure_corpus_schema(corpus_database)

    session = _session()
    _require_host(FATAL_FACTS_PAGE, "osha.gov")
    listing = session.get(FATAL_FACTS_PAGE, timeout=REQUEST_TIMEOUT)
    listing.raise_for_status()
    entries = parse_fatal_facts_listing(listing.text)
    if limit:
        entries = entries[:limit]

    imported: list[dict[str, Any]] = []
    for entry in entries:
        editions = [("en", entry["english_url"])]
        if include_spanish and entry["spanish_url"]:
            editions.append(("es", entry["spanish_url"]))

        for language, url in editions:
            _require_host(url, "osha.gov")
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                raise GroundingError(f"OSHA returned HTTP {response.status_code} for {url}")

            source_key = f"FF-{entry['number']}-{entry['year']}"
            snapshot_key = f"fatal_facts:{source_key}:{language}"
            local_path = snapshot_root / "fatal_facts" / language / f"{source_key}.pdf"
            record_snapshot(
                corpus_database,
                snapshot_key=snapshot_key,
                source_type=SOURCE_FATAL_FACTS,
                source_url=url,
                payload=response.content,
                local_path=local_path,
                http_status=response.status_code,
            )

            text = _pdf_text(response.content, local_path)
            citation = (
                f"OSHA Fatal Facts No. {entry['number']} ({entry['year']}) — {entry['title']}"
            )
            passages = []
            equipment_terms = load_equipment_terms(factory_database) if factory_database else {}
            for section in split_fatal_facts_text(text):
                passages.append(
                    {
                        "section_label": section["section_label"],
                        "section_role": section["section_role"],
                        "citation_label": citation,
                        "text": section["text"],
                        "hazard_tags": tag_hazards(section["text"]),
                        "equipment_tags": tag_equipment(section["text"], equipment_terms),
                    }
                )
            if not passages:
                raise GroundingError(f"No extractable text in Fatal Facts PDF: {url}")

            result = upsert_document(
                corpus_database,
                document_id=f"fatal_facts:{source_key}:{language}",
                source_type=SOURCE_FATAL_FACTS,
                source_key=source_key,
                title=entry["title"],
                publisher="U.S. Occupational Safety and Health Administration",
                official_url=url,
                snapshot_key=snapshot_key,
                passages=passages,
                published_on=entry["year"],
                language=language,
                edition=f"Fatal Facts No. {entry['number']}",
            )
            imported.append({"source_key": source_key, "language": language, **result})

    return {
        "source_type": SOURCE_FATAL_FACTS,
        "collected_at": _utc_stamp(),
        "listing_entries": len(entries),
        "documents": imported,
        "total_passages": sum(item["passages"] for item in imported),
    }


# --------------------------------------------------------------------------
# G1c - NIOSH FACE reports placed by hand
# --------------------------------------------------------------------------

def ingest_face_reports_impl(
    directory: Path | str,
    *,
    corpus_database: Path | None = None,
    snapshot_root: Path | None = None,
    factory_database: Path | None = None,
) -> dict[str, Any]:
    """Ingest NIOSH FACE reports from a local folder (.pdf, .txt or .html).

    CDC's edge blocks automated download from many networks, so this adapter
    never fetches. Download the reports yourself from the NIOSH FACE program
    site, drop the files in ``directory``, and this ingests them with the same
    snapshot, digest and citation discipline as the network adapters.
    """
    directory = Path(directory)
    corpus_database = Path(corpus_database or PROJECT_ROOT / "data" / "corpus" / "grounding.db")
    snapshot_root = Path(snapshot_root or PROJECT_ROOT / "data" / "corpus" / "snapshots")
    ensure_corpus_schema(corpus_database)

    if not directory.is_dir():
        raise GroundingError(f"FACE report directory does not exist: {directory}")

    equipment_terms = load_equipment_terms(factory_database) if factory_database else {}
    imported: list[dict[str, Any]] = []

    for path in sorted(directory.iterdir()):
        if path.suffix.casefold() not in {".pdf", ".txt", ".html", ".htm"}:
            continue
        payload = path.read_bytes()
        source_key = path.stem

        if path.suffix.casefold() == ".pdf":
            text = _pdf_text(payload, snapshot_root / "face" / path.name)
        elif path.suffix.casefold() in {".html", ".htm"}:
            text = BeautifulSoup(payload.decode("utf-8", "replace"), "html.parser").get_text(" ")
        else:
            text = payload.decode("utf-8", "replace")

        snapshot_key = f"face:{source_key}"
        record_snapshot(
            corpus_database,
            snapshot_key=snapshot_key,
            source_type=SOURCE_FACE,
            source_url=path.resolve().as_uri(),
            payload=payload,
            local_path=snapshot_root / "face" / path.name,
            http_status=None,
        )

        blocks = [_clean(block) for block in re.split(r"\n\s*\n", text) if _clean(block)]
        if not blocks:
            raise GroundingError(f"No extractable text in FACE report: {path}")

        citation = f"NIOSH FACE report {source_key}"
        passages = [
            {
                "section_label": f"block {index}",
                "section_role": "OTHER",
                "citation_label": citation,
                "text": block,
                "hazard_tags": tag_hazards(block),
                "equipment_tags": tag_equipment(block, equipment_terms),
            }
            for index, block in enumerate(blocks, start=1)
        ]
        result = upsert_document(
            corpus_database,
            document_id=f"face:{source_key}",
            source_type=SOURCE_FACE,
            source_key=source_key,
            title=f"NIOSH FACE {source_key}",
            publisher="U.S. National Institute for Occupational Safety and Health",
            official_url="https://www.cdc.gov/niosh/face/",
            snapshot_key=snapshot_key,
            passages=passages,
            language="en",
        )
        imported.append({"source_key": source_key, **result})

    return {
        "source_type": SOURCE_FACE,
        "collected_at": _utc_stamp(),
        "directory": str(directory),
        "documents": imported,
        "total_passages": sum(item["passages"] for item in imported),
    }


@tool
def collect_grounding_corpus(cfr_only: bool = False) -> dict:
    """Download and freeze the official grounding corpus (29 CFR + Fatal Facts)."""
    from ..config import load_settings

    settings = load_settings()
    corpus_database = settings.project_root / "data" / "corpus" / "grounding.db"
    result: dict[str, Any] = {
        "cfr": collect_cfr_sections_impl(
            corpus_database=corpus_database,
            factory_database=settings.factory_database,
        )
    }
    if not cfr_only:
        result["fatal_facts"] = collect_fatal_facts_impl(
            corpus_database=corpus_database,
            factory_database=settings.factory_database,
        )
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="采集 G1 接地语料（29 CFR + OSHA Fatal Facts）。")
    parser.add_argument("--cfr-only", action="store_true", help="只采集 29 CFR")
    parser.add_argument("--limit", type=int, default=None, help="限制 Fatal Facts 篇数")
    parser.add_argument("--face-dir", default=None, help="本地 NIOSH FACE 报告目录")
    args = parser.parse_args()

    from ..config import load_settings

    settings = load_settings()
    corpus_database = settings.project_root / "data" / "corpus" / "grounding.db"
    output: dict[str, Any] = {
        "cfr": collect_cfr_sections_impl(
            corpus_database=corpus_database, factory_database=settings.factory_database
        )
    }
    if not args.cfr_only:
        output["fatal_facts"] = collect_fatal_facts_impl(
            corpus_database=corpus_database,
            limit=args.limit,
            factory_database=settings.factory_database,
        )
    if args.face_dir:
        output["face"] = ingest_face_reports_impl(
            args.face_dir,
            corpus_database=corpus_database,
            factory_database=settings.factory_database,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
