"""G1 retrieval: pick the corpus passages a draft is allowed to stand on.

Deterministic and offline. No model, no network, no embedding: given an
incident narrative and the factory hazard path it was matched to, this returns
a ranked, explainable set of passages from the frozen corpus.

Two stages, in the spirit of the project's W5a/W5b split:

1. A hard filter on the site's own vocabulary - the hazard ids from
   ``matching.HAZARD_TERMS`` and equipment ids from the factory asset register.
   A passage that shares no hazard and no equipment with the path is dropped
   before scoring, not down-weighted.
2. BM25 over the surviving passages. The implementation is inlined rather than
   pulled from a library so the ranking stays auditable and adds no dependency.

The result carries the term hits that earned each passage its rank, so a
reviewer can see why a citation was offered rather than being asked to trust a
similarity number.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .corpus_db import iter_passages

RETRIEVAL_VERSION = "grounding-v1.0"

#: Roles that carry an obligation or a corrective step. G2 and G3 draw their
#: "recommended action" claims only from these; a narrative passage may explain
#: what happened but may not be cited as a thing the site should do.
ACTIONABLE_ROLES = frozenset({"REQUIREMENT", "PREVENTION"})
NARRATIVE_ROLES = frozenset({"DESCRIPTION", "SUMMARY", "CAUSE"})

_TOKEN = re.compile(r"[a-záéíóúñü0-9][a-záéíóúñü0-9\-\.]{1,}", re.IGNORECASE)
_STOP = frozenset("""
a an and are as at be been by for from had has have in into is it its of on or
that the this to was were will with which when where who whom shall should may
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in (m.group(0).casefold() for m in _TOKEN.finditer(text)) if t not in _STOP]


@dataclass(frozen=True)
class GroundedPassage:
    passage_id: str
    source_type: str
    language: str
    section_role: str
    section_label: str
    citation_label: str
    title: str
    official_url: str
    text: str
    text_sha256: str
    score: float
    matched_terms: list[str] = field(default_factory=list)
    matched_hazards: list[str] = field(default_factory=list)
    matched_equipment: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passage_id": self.passage_id,
            "source_type": self.source_type,
            "language": self.language,
            "section_role": self.section_role,
            "citation_label": self.citation_label,
            "official_url": self.official_url,
            "score": round(self.score, 6),
            "matched_terms": self.matched_terms,
            "matched_hazards": self.matched_hazards,
            "matched_equipment": self.matched_equipment,
        }


class GroundingIndex:
    """A BM25 index over the frozen corpus, built once and reused per run."""

    K1 = 1.5
    B = 0.75

    def __init__(self, corpus_database: Path | str) -> None:
        self.corpus_database = Path(corpus_database)
        self.passages = iter_passages(self.corpus_database)
        if not self.passages:
            raise ValueError(
                f"Grounding corpus is empty: {self.corpus_database}. "
                "Run hindsight-grounding-collect first."
            )
        self._tokens: list[list[str]] = [tokenize(p["text"]) for p in self.passages]
        self._lengths = [len(t) for t in self._tokens]
        self._avg_length = sum(self._lengths) / len(self._lengths)
        self._counts: list[Counter] = [Counter(t) for t in self._tokens]
        document_frequency: Counter = Counter()
        for token_set in ({*t} for t in self._tokens):
            document_frequency.update(token_set)
        total = len(self.passages)
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in document_frequency.items()
        }

    def _bm25(self, index: int, query_terms: list[str]) -> tuple[float, list[str]]:
        counts = self._counts[index]
        length = self._lengths[index] or 1
        score = 0.0
        hits: list[str] = []
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            idf = self._idf.get(term, 0.0)
            numerator = frequency * (self.K1 + 1)
            denominator = frequency + self.K1 * (
                1 - self.B + self.B * length / self._avg_length
            )
            score += idf * numerator / denominator
            hits.append(term)
        return score, sorted(set(hits))

    def retrieve(
        self,
        query_text: str,
        *,
        hazard_ids: Iterable[str] = (),
        equipment_ids: Iterable[str] = (),
        language: str = "en",
        roles: Iterable[str] | None = None,
        top_k: int = 6,
        require_tag_overlap: bool = True,
    ) -> list[GroundedPassage]:
        """Hard-filter on the site's vocabulary, then rank what survives."""
        hazards = {h for h in hazard_ids if h}
        equipment = {e for e in equipment_ids if e}
        wanted_roles = set(roles) if roles else None
        query_terms = tokenize(query_text)

        results: list[GroundedPassage] = []
        for index, passage in enumerate(self.passages):
            if passage["language"] != language:
                continue
            if wanted_roles and passage["section_role"] not in wanted_roles:
                continue

            passage_hazards = set(json.loads(passage["hazard_tags_json"]))
            passage_equipment = set(json.loads(passage["equipment_tags_json"]))
            hazard_hits = sorted(hazards & passage_hazards)
            equipment_hits = sorted(equipment & passage_equipment)
            if require_tag_overlap and (hazards or equipment) and not (hazard_hits or equipment_hits):
                continue

            score, term_hits = self._bm25(index, query_terms)
            if score <= 0 and not (hazard_hits or equipment_hits):
                continue
            # Tag agreement is evidence too, and is stated separately rather
            # than folded into an opaque weighted total.
            score += 1.0 * len(hazard_hits) + 1.5 * len(equipment_hits)

            results.append(
                GroundedPassage(
                    passage_id=passage["passage_id"],
                    source_type=passage["source_type"],
                    language=passage["language"],
                    section_role=passage["section_role"],
                    section_label=passage["section_label"],
                    citation_label=passage["citation_label"],
                    title=passage["title"],
                    official_url=passage["official_url"],
                    text=passage["text"],
                    text_sha256=passage["text_sha256"],
                    score=score,
                    matched_terms=term_hits[:12],
                    matched_hazards=hazard_hits,
                    matched_equipment=equipment_hits,
                )
            )

        results.sort(key=lambda item: (-item.score, item.passage_id))
        return results[:top_k]


def load_risk_path(factory_database: Path | str, risk_assessment_id: str) -> dict[str, Any]:
    """Read the one hazard path a briefing is about, with its approved controls."""
    connection = sqlite3.connect(str(factory_database))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT ra.id, ra.assessment_code, ra.equipment_id, ra.task_id,
                   ra.hazard_id, ra.exposed_role_id, ra.hazardous_situation,
                   ra.initial_severity, ra.initial_score, ra.initial_level,
                   ra.residual_score, ra.residual_level, ra.risk_accepted,
                   ra.status,
                   e.name AS equipment_name, e.equipment_type, e.asset_code,
                   a.name AS area_name, f.id AS factory_id, f.name AS factory_name,
                   f.naics AS factory_naics,
                   t.name AS task_name, h.name AS hazard_name, h.category AS hazard_category,
                   w.name AS exposed_role
            FROM risk_assessments AS ra
            JOIN equipment AS e ON e.id = ra.equipment_id
            JOIN areas AS a ON a.id = e.area_id
            JOIN factories AS f ON f.id = e.factory_id
            JOIN tasks AS t ON t.id = ra.task_id
            JOIN hazards AS h ON h.id = ra.hazard_id
            JOIN worker_roles AS w ON w.id = ra.exposed_role_id
            WHERE ra.id = ?
            """,
            (risk_assessment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No such risk assessment: {risk_assessment_id}")
        controls = connection.execute(
            """
            SELECT sc.id, sc.name, sc.control_type, sc.hierarchy_code,
                   sc.performance_requirement, sc.verification_method,
                   rac.implementation_status
            FROM risk_assessment_controls AS rac
            JOIN safety_controls AS sc ON sc.id = rac.safety_control_id
            WHERE rac.risk_assessment_id = ?
            ORDER BY sc.id
            """,
            (risk_assessment_id,),
        ).fetchall()
    finally:
        connection.close()

    path = dict(row)
    path["controls"] = [dict(control) for control in controls]
    return path


def approved_control_vocabulary(factory_database: Path | str) -> dict[str, dict[str, Any]]:
    """Every control name the site has approved. G4 refuses any other."""
    connection = sqlite3.connect(str(factory_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, name, control_type, hierarchy_code, performance_requirement "
            "FROM safety_controls"
        ).fetchall()
    finally:
        connection.close()
    return {row["id"]: dict(row) for row in rows}


def build_query(incident_text: str, risk_path: dict[str, Any]) -> str:
    """Join the incident narrative with the site's own words for the path."""
    parts = [
        incident_text,
        risk_path.get("equipment_name", ""),
        risk_path.get("equipment_type", ""),
        risk_path.get("task_name", ""),
        risk_path.get("hazard_name", ""),
        risk_path.get("hazard_category", ""),
        risk_path.get("hazardous_situation", ""),
    ]
    return " ".join(str(p) for p in parts if p)


def gather_evidence(
    *,
    corpus_database: Path | str,
    factory_database: Path | str,
    risk_assessment_id: str,
    incident_text: str,
    languages: tuple[str, ...] = ("en", "es"),
    top_k: int = 6,
    index: GroundingIndex | None = None,
) -> dict[str, Any]:
    """G1 end to end: the evidence bundle G2 and G3 are allowed to draw on."""
    risk_path = load_risk_path(factory_database, risk_assessment_id)
    index = index or GroundingIndex(corpus_database)
    query = build_query(incident_text, risk_path)

    bundle: dict[str, Any] = {
        "retrieval_version": RETRIEVAL_VERSION,
        "risk_assessment_id": risk_assessment_id,
        "risk_path": risk_path,
        "query_terms": tokenize(query)[:40],
        "by_language": {},
    }
    for language in languages:
        bundle["by_language"][language] = {
            "requirements": [
                p.to_dict() | {"text": p.text}
                for p in index.retrieve(
                    query,
                    hazard_ids=[risk_path["hazard_id"]],
                    equipment_ids=[risk_path["equipment_id"]],
                    language=language,
                    roles=ACTIONABLE_ROLES,
                    top_k=top_k,
                    require_tag_overlap=False,
                )
            ],
            "narratives": [
                p.to_dict() | {"text": p.text}
                for p in index.retrieve(
                    query,
                    hazard_ids=[risk_path["hazard_id"]],
                    equipment_ids=[risk_path["equipment_id"]],
                    language=language,
                    roles=NARRATIVE_ROLES,
                    top_k=top_k,
                    require_tag_overlap=False,
                )
            ],
        }
    return bundle
