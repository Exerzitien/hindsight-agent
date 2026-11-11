"""W4 · W5a · W6: extract with a confidence, filter hard, archive the rest.

Three jobs the collection pipeline never did, all deterministic:

**W4 抽取置信度与隔离队列.** The SIR feed carries OSHA's own classification
codes, so extraction here is field completeness and cross-field agreement, not
a model's self-reported certainty. A record whose narrative is too thin, whose
codes are missing, or whose narrative contradicts its own event title scores
low and goes to ``quarantine`` instead of into the pipeline. The spec's F2
fallback - 每日人工抽检 10 条 - is a sampling flag on that table.

**W5a 硬过滤.** Deterministic, zero model cost, and the reason the rest of the
pipeline is affordable: an incident survives only if its industry is a declared
neighbour, its hazard is not muted, and its narrative uses at least one term
from the site's own hazard vocabulary. Every rejection carries a reason code.

**W6 静默归档.** Archiving is a state a case reaches, not an absence of work.
Each archived case contributes one line to the weekly digest, which is how the
site can tell the difference between "nothing was relevant" and "nothing ran".

The vocabulary comes from ``matching.HAZARD_TERMS`` and from the site's own
hazard register - nothing here invents a mapping between OSHA codes and site
hazards, because a mapping nobody can check is worse than no mapping.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .matching import HAZARD_TERMS, matched_terms
from .workflow_db import connect, new_id, utc_now

SCREENING_VERSION = "screening-v1.0"

# --- W4 ---------------------------------------------------------------------
#: Below this a record is quarantined rather than screened. Set high enough to
#: catch truncated or code-less rows, low enough that ordinary SIR records pass.
CONFIDENCE_FLOOR = 0.55
#: F2: how many quarantined records get flagged for the daily human spot-check.
DAILY_SAMPLE = 10
MIN_NARRATIVE_CHARS = 60

R_LOW_CONFIDENCE = "LOW_EXTRACTION_CONFIDENCE"
R_NARRATIVE_TOO_SHORT = "NARRATIVE_TOO_SHORT"

# --- W5a --------------------------------------------------------------------
R_PASS = "PASS"
R_INDUSTRY_NOT_NEIGHBOUR = "INDUSTRY_NOT_NEIGHBOUR"
R_HAZARD_MUTED = "HAZARD_MUTED"
R_NO_SITE_HAZARD_TERM = "NO_SITE_HAZARD_TERM"
R_NO_NAICS = "NO_NAICS"

_DIGITS = re.compile(r"\D")


@dataclass(frozen=True)
class ExtractionResult:
    incident_key: str
    confidence: float
    fields_present: list[str]
    fields_missing: list[str]
    weak_label_agrees: bool | None
    quarantined: bool
    reason_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_key": self.incident_key,
            "confidence": round(self.confidence, 3),
            "fields_present": self.fields_present,
            "fields_missing": self.fields_missing,
            "weak_label_agrees": self.weak_label_agrees,
            "quarantined": self.quarantined,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ScreenResult:
    incident_key: str
    passed: bool
    reason_code: str
    matched_hazards: list[str] = field(default_factory=list)
    industry_relation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_key": self.incident_key,
            "passed": self.passed,
            "reason_code": self.reason_code,
            "matched_hazards": self.matched_hazards,
            "industry_relation": self.industry_relation,
            "screening_version": SCREENING_VERSION,
        }


# --------------------------------------------------------------------------
# W4 · 结构化抽取置信度
# --------------------------------------------------------------------------

#: Weights sum to 1.0. Narrative carries the most because everything downstream
#: quotes it; the codes are OSHA's own and are treated as corroboration.
_FIELD_WEIGHTS = {
    "narrative": 0.40,
    "primary_naics": 0.20,
    "event_title": 0.15,
    "nature_title": 0.10,
    "body_part_title": 0.10,
    "source_title": 0.05,
}


def _weak_label_agreement(narrative: str, event_title: str | None) -> bool | None:
    """Does the narrative use any word from OSHA's own event label?

    A weak check on purpose: it catches rows whose narrative and classification
    have drifted apart, without pretending to validate the classification.
    """
    if not event_title:
        return None
    words = [w for w in re.split(r"\W+", event_title.casefold()) if len(w) >= 4]
    if not words:
        return None
    folded = narrative.casefold()
    return any(word in folded for word in words)


def extract_with_confidence(record: dict[str, Any]) -> ExtractionResult:
    narrative = str(record.get("narrative") or "")
    present: list[str] = []
    missing: list[str] = []
    score = 0.0
    for name, weight in _FIELD_WEIGHTS.items():
        value = str(record.get(name) or "").strip()
        if name == "narrative":
            ok = len(value) >= MIN_NARRATIVE_CHARS
        else:
            ok = bool(value)
        if ok:
            present.append(name)
            score += weight
        else:
            missing.append(name)

    agrees = _weak_label_agreement(narrative, record.get("event_title"))
    if agrees is False:
        # Disagreement is a reason to look, not a reason to discard: it costs a
        # tenth of the score, which is enough to quarantine a borderline row.
        score -= 0.10

    score = max(0.0, min(1.0, score))
    if len(narrative) < MIN_NARRATIVE_CHARS:
        reason = R_NARRATIVE_TOO_SHORT
    elif score < CONFIDENCE_FLOOR:
        reason = R_LOW_CONFIDENCE
    else:
        reason = None

    return ExtractionResult(
        incident_key=str(record.get("incident_key")),
        confidence=score,
        fields_present=present,
        fields_missing=missing,
        weak_label_agrees=agrees,
        quarantined=reason is not None,
        reason_code=reason,
    )


def quarantine_record(
    database_path: Path | str, result: ExtractionResult, *, sampled: bool = False
) -> str:
    quarantine_id = new_id("q")
    connection = connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO quarantine(quarantine_id, incident_key, reason_code, "
                "detail, extraction_confidence, sampled_for_review, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (quarantine_id, result.incident_key, result.reason_code or "",
                 json.dumps(result.to_dict(), ensure_ascii=False),
                 result.confidence, 1 if sampled else 0, utc_now()),
            )
    finally:
        connection.close()
    return quarantine_id


def daily_spot_check(
    database_path: Path | str, limit: int = DAILY_SAMPLE
) -> list[dict[str, Any]]:
    """F2: the day's quarantine sample a person is expected to eyeball."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM quarantine WHERE reviewed_at IS NULL "
            "ORDER BY sampled_for_review DESC, extraction_confidence ASC, created_at "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# W5a · 确定性硬过滤
# --------------------------------------------------------------------------

def site_hazard_vocabulary(factory_database: Path | str) -> dict[str, tuple[str, ...]]:
    """Only the hazard terms this site actually registered.

    A site with no forklift does not get forklift alerts, because its hazard
    register never mentions one - the filter is driven by the site's own data,
    not by a list somebody wrote once.
    """
    connection = sqlite3.connect(str(factory_database))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT DISTINCT hazard_id FROM risk_assessments"
        ).fetchall()
    finally:
        connection.close()
    site_hazards = {row["hazard_id"] for row in rows}
    return {h: terms for h, terms in HAZARD_TERMS.items() if h in site_hazards}


def industry_relation(incident_naics: str | None, neighbours: Iterable[str]) -> str:
    """'exact' / 'neighbour' / 'unrelated' / 'unknown' — never a number."""
    digits = _DIGITS.sub("", incident_naics or "")
    if not digits:
        return "unknown"
    prefixes = [str(n) for n in neighbours if str(n)]
    if not prefixes:
        return "unrelated"
    if any(digits.startswith(p) for p in prefixes):
        return "exact" if digits.startswith(prefixes[0]) else "neighbour"
    if any(digits[:2] == p[:2] for p in prefixes):
        return "neighbour"
    return "unrelated"


def hard_filter(
    record: dict[str, Any],
    *,
    vocabulary: dict[str, tuple[str, ...]],
    rule_pack_payload: dict[str, Any],
) -> ScreenResult:
    """Zero model cost. Every rejection names its reason."""
    key = str(record.get("incident_key"))
    relation = industry_relation(
        record.get("primary_naics"), rule_pack_payload.get("naics_neighbours", [])
    )
    if relation == "unknown":
        return ScreenResult(key, False, R_NO_NAICS, industry_relation=relation)
    if relation == "unrelated":
        return ScreenResult(key, False, R_INDUSTRY_NOT_NEIGHBOUR, industry_relation=relation)

    text = " ".join(
        str(record.get(name) or "")
        for name in ("narrative", "source_title", "event_title", "secondary_source_title")
    ).casefold()

    muted = set(rule_pack_payload.get("muted_hazards", []))
    matched = sorted(
        hazard for hazard, terms in vocabulary.items()
        if matched_terms(text, terms)
    )
    if not matched:
        return ScreenResult(key, False, R_NO_SITE_HAZARD_TERM, industry_relation=relation)

    surviving = [h for h in matched if h not in muted]
    if not surviving:
        return ScreenResult(key, False, R_HAZARD_MUTED, matched, relation)

    return ScreenResult(key, True, R_PASS, surviving, relation)


def screen_batch(
    records: list[dict[str, Any]],
    *,
    vocabulary: dict[str, tuple[str, ...]],
    rule_pack_payload: dict[str, Any],
) -> dict[str, Any]:
    """Screen a batch and report the reduction, which is the point of W5a."""
    results = [
        hard_filter(record, vocabulary=vocabulary, rule_pack_payload=rule_pack_payload)
        for record in records
    ]
    passed = [r for r in results if r.passed]
    by_reason: dict[str, int] = {}
    for result in results:
        by_reason[result.reason_code] = by_reason.get(result.reason_code, 0) + 1
    return {
        "screening_version": SCREENING_VERSION,
        "considered": len(records),
        "passed": len(passed),
        "rejected": len(records) - len(passed),
        "reduction": round(1 - len(passed) / len(records), 4) if records else 0.0,
        "by_reason_code": by_reason,
        "results": [r.to_dict() for r in results],
        "surviving_keys": [r.incident_key for r in passed],
    }


# --------------------------------------------------------------------------
# W6 · 静默归档与周报
# --------------------------------------------------------------------------

def archive_line(case: dict[str, Any], reason_code: str, detail: str = "") -> str:
    """One line per archived case. This is what proves the agent was looking."""
    label = {
        R_INDUSTRY_NOT_NEIGHBOUR: "行业不相邻",
        R_NO_SITE_HAZARD_TERM: "未命中本厂任何已登记危险",
        R_HAZARD_MUTED: "该危险类别已被本厂静音",
        R_NO_NAICS: "缺少行业代码",
        "GREEN": "危险路径已被本厂已验证的控制阻断",
        "NOT_RELEVANT": "没有匹配到本厂危险路径",
    }.get(reason_code, reason_code)
    tail = f"　{detail}" if detail else ""
    return f"{case.get('incident_key', '?')}　{label}{tail}"


def weekly_archive_digest(
    database_path: Path | str, *, limit: int = 500
) -> dict[str, Any]:
    """W6 + D5 的数据源：本周静默归档了什么，为什么。

    ``limit`` 只截断给人读的明细行，不截断统计。此前 ``archived`` 直接取被
    LIMIT 截断后的行数，于是归档 495 条时晨报显示 200 条——用查询上限冒充计数，
    会让「我们替你挡掉了多少」这个本系统最核心的数字系统性偏小。
    """
    connection = connect(database_path)
    try:
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM cases AS c "
            "JOIN case_transitions AS t ON t.case_id = c.case_id AND t.to_state='ARCHIVED'"
        ).fetchone()["n"]
        reason_rows = connection.execute(
            "SELECT t.reason_code AS reason_code, COUNT(*) AS n FROM cases AS c "
            "JOIN case_transitions AS t ON t.case_id = c.case_id AND t.to_state='ARCHIVED' "
            "GROUP BY t.reason_code ORDER BY n DESC"
        ).fetchall()
        rows = connection.execute(
            "SELECT c.incident_key, c.level, c.case_id, t.reason_code, t.detail, t.occurred_at "
            "FROM cases AS c "
            "JOIN case_transitions AS t ON t.case_id = c.case_id AND t.to_state='ARCHIVED' "
            "ORDER BY t.occurred_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    lines = [archive_line(dict(row), row["reason_code"], row["detail"] or "") for row in rows]
    by_reason = {row["reason_code"]: int(row["n"]) for row in reason_rows}
    return {
        "archived": int(total),
        "by_reason_code": by_reason,
        "lines": lines,
        "lines_truncated": int(total) > len(lines),
    }
