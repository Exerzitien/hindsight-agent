"""Hybrid semantic and evidence-based incident-to-factory risk matching."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

from .milvus_store import VectorStore
from .rag_documents import RagDocument


SCORE_VERSION = "hybrid-v1.0"

HAZARD_TERMS = {
    "haz-amputation": ("amputation", "amputated", "severed", "lost finger", "lost hand"),
    "haz-arc-radiation": ("arc flash", "ultraviolet", "radiation"),
    "haz-burn": ("burn", "scald", "hot metal", "molten"),
    "haz-caught-in": ("caught in", "caught-in", "pinch point", "drawn into", "machine jam"),
    "haz-chemical": ("chemical exposure", "toxic", "solvent", "acid", "caustic"),
    "haz-collision": ("collision", "forklift struck", "vehicle struck"),
    "haz-compressed-gas": ("compressed gas", "gas cylinder", "pressure release"),
    "haz-crush": ("crush", "crushed", "compressed", "pinned between"),
    "haz-dock-fall": ("loading dock", "fell from dock", "dock edge"),
    # 「electric」这个词本身不是电气危险的证据：它命中「electric motor」
    # 「electric pallet jack」「electric cart」。在最近 500 条事故里，有 30 条
    # 仅因为这一个词被贴上电气危险——包括一起在收银台绊到风扇线的摔伤——
    # 而其中一条一路走到了红色告警，把一起皮带绞入事故当作焊接工位触电，
    # 推给焊工。这正是本项目用定向分发要避免的那种警报疲劳。
    # 用词干（electrocut 同时命中 electrocution / electrocuted）与真正表明
    # 带电接触的短语替代它。
    "haz-electrical": (
        "electrocut", "electric shock", "electrical shock", "energized",
        "power line", "powerline", "electrical line", "electrical burn",
        "exposure to electricity", "live wire", "live circuit", "volt",
    ),
    "haz-entanglement": ("entangled", "rotating shaft", "pulled into", "caught in machinery"),
    "haz-falling-object": ("falling object", "struck by object", "overhead load"),
    "haz-fire": ("fire", "explosion", "ignition"),
    "haz-flammable-vapor": ("flammable vapor", "combustible vapor", "flash fire"),
    "haz-laceration": ("laceration", "cut", "saw blade", "sharp edge"),
    "haz-spill-slip": ("slip", "spill", "wet floor"),
    "haz-struck-by": ("struck by", "hit by", "projectile"),
    "haz-tip-over": ("tip over", "tipped over", "forklift overturn", "rollover"),
    "haz-trailer-movement": ("trailer movement", "trailer pulled", "drive-away"),
    "haz-unexpected-startup": ("unexpected startup", "unexpected start", "lockout", "energized"),
    "haz-welding-fume": ("welding fume", "metal fume", "welding exposure"),
}


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(term), re.IGNORECASE)


def matched_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Which of these hazard terms the text actually uses.

    Matching is anchored at a word start, not anywhere in the string. Plain
    substring matching reads "unenergized" as "energized" - the opposite of
    what the narrative says - and that is not a rare edge: it happened in the
    first 500 rows of the real corpus. A trailing wildcard is deliberate, so
    one term still covers its inflections: "crush" matches "crushed", "burn"
    matches "burns", "electrocut" matches both "electrocution" and
    "electrocuted".
    """
    return [term for term in terms if _term_pattern(term).search(text)]


def _hazard_evidence(text: str, hazard_id: str) -> tuple[float, list[str]]:
    matched = matched_terms(text, HAZARD_TERMS.get(hazard_id, ()))
    return (1.0 if matched else 0.0), matched


def _industry_score(incident_naics: str, factory_naics: str) -> float:
    left = re.sub(r"\D", "", incident_naics or "")
    right = re.sub(r"\D", "", factory_naics or "")
    if not left or not right:
        return 0.0
    if left[:3] == right[:3]:
        return 1.0
    if left[:2] == right[:2]:
        return 0.5
    return 0.0


def _consequence_score(document: RagDocument, hazard_id: str) -> float:
    metadata = document.metadata
    if int(metadata.get("amputation", 0) or 0) > 0:
        return 1.0 if hazard_id in {
            "haz-amputation", "haz-caught-in", "haz-entanglement", "haz-crush",
        } else 0.0
    if int(metadata.get("loss_of_eye", 0) or 0) > 0:
        return 1.0 if hazard_id in {
            "haz-arc-radiation", "haz-chemical", "haz-laceration", "haz-struck-by",
        } else 0.0
    if int(metadata.get("hospitalized", 0) or 0) > 0:
        return 0.25
    return 0.0


def match_incident(
    document: RagDocument,
    query_vector: list[float],
    store: VectorStore,
    *,
    top_k: int,
    threshold: float,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for hit in store.search_factory(query_vector, top_k):
        metadata = hit.metadata
        semantic = min(1.0, max(0.0, hit.score))
        hazard, evidence_terms = _hazard_evidence(document.text, metadata["hazard_id"])
        industry = _industry_score(document.naics, hit.naics)
        consequence = _consequence_score(document, metadata["hazard_id"])
        relevance = (
            semantic * 0.70
            + hazard * 0.15
            + industry * 0.10
            + consequence * 0.05
        )
        if relevance < threshold:
            continue
        matches.append(
            {
                "incident_document_id": document.id,
                "incident_source_type": document.document_type,
                "incident_record_id": document.source_record_id,
                "incident_title": document.title,
                "incident_date": document.event_date,
                "incident_url": document.metadata.get("url"),
                "factory_risk_id": hit.id,
                "factory_id": metadata["factory_id"],
                "factory_name": metadata["factory_name"],
                "area_id": metadata["area_id"],
                "area_name": metadata["area_name"],
                "equipment_id": metadata["equipment_id"],
                "asset_code": metadata["asset_code"],
                "equipment_name": metadata["equipment_name"],
                "assessment_code": metadata["assessment_code"],
                "hazard_id": metadata["hazard_id"],
                "hazard_name": metadata["hazard_name"],
                "semantic_score": round(semantic, 6),
                "hazard_score": hazard,
                "industry_score": industry,
                "consequence_score": consequence,
                "relevance_score": round(relevance, 6),
                "score_version": SCORE_VERSION,
                "rationale": {
                    "weights": {
                        "semantic": 0.70,
                        "hazard_evidence": 0.15,
                        "industry": 0.10,
                        "consequence": 0.05,
                    },
                    "matched_hazard_terms": evidence_terms,
                    "incident_naics": document.naics,
                    "factory_naics": hit.naics,
                    "warning": "Relevance score is retrieval evidence, not accident probability.",
                },
            }
        )
    return sorted(matches, key=lambda item: item["relevance_score"], reverse=True)
