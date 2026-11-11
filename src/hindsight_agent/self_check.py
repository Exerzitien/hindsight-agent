"""G4 grounding self-check: delete every sentence that cannot show its source.

This is the enforcement point for the project's third iron rule. It is
deliberately written as ordinary code with no model in the loop, because a
model checking a model is not a control - it is the same failure twice.

Ten checks run against every claim. Nine of them are answerable by lookup or
string comparison; none requires judgement:

======================== =================================================
CITATION_PRESENT         the claim cites at least one source
CITATION_RESOLVES        every citation resolves to a real stored record
CITATION_TYPE            the source kind matches what the claim asserts
QUOTE_VERBATIM           a quoted span appears character-for-character
NUMERALS_SUPPORTED       every number in the claim appears in a source
BILINGUAL_PRESENT        the Spanish rendering exists
BILINGUAL_NUMERALS       Spanish and English carry the same numbers
CONTROL_VOCABULARY       a control claim names a control the site approved
SITE_SCOPE               site references belong to this hazard path
QUIZ_BACKED              every quiz item rests on a surviving claim
======================== =================================================

``NUMERALS_SUPPORTED`` is the one that matters most in practice. A generated
safety instruction that invents a clearance, a torque or an interval reads
perfectly and is dangerous; requiring every numeral to appear in a cited source
kills that class of error outright.

Failure handling follows the F4 fallback: unsupported claims are removed, the
caller may regenerate once, and a draft that still cannot fill its required
sections is marked NEEDS_HUMAN_WRITER and goes to the queue as a writing task
rather than as training material.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .corpus_db import get_passage
from .grounding import approved_control_vocabulary
from .learning_pack import (
    ACTION_CLAIM_TYPES,
    CLAIM_CONTROL,
    CLAIM_INCIDENT_FACT,
    CLAIM_REQUIREMENT,
    CLAIM_SITE_FACT,
    CLAIM_UNKNOWN,
    STATE_NEEDS_HUMAN_WRITER,
    STATE_SELF_CHECK_FAIL,
    STATE_SELF_CHECK_OK,
    Claim,
    LearningDraft,
    normalize_numerals,
)

SELF_CHECK_VERSION = "self-check-v1.0"

CHECK_CITATION_PRESENT = "CITATION_PRESENT"
CHECK_CITATION_RESOLVES = "CITATION_RESOLVES"
CHECK_CITATION_TYPE = "CITATION_TYPE"
CHECK_QUOTE_VERBATIM = "QUOTE_VERBATIM"
CHECK_NUMERALS_SUPPORTED = "NUMERALS_SUPPORTED"
CHECK_BILINGUAL_PRESENT = "BILINGUAL_PRESENT"
CHECK_BILINGUAL_NUMERALS = "BILINGUAL_NUMERALS"
CHECK_CONTROL_VOCABULARY = "CONTROL_VOCABULARY"
CHECK_SITE_SCOPE = "SITE_SCOPE"
CHECK_QUIZ_BACKED = "QUIZ_BACKED"

#: A briefing without these is not a briefing. If repair empties any of them the
#: draft becomes a human writing task instead of being shipped thin.
REQUIRED_CLAIM_TYPES = (CLAIM_INCIDENT_FACT, CLAIM_SITE_FACT)
REQUIRED_ANY_OF = ACTION_CLAIM_TYPES


@dataclass
class ClaimFinding:
    claim_id: str
    check: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"claim_id": self.claim_id, "check": self.check, "detail": self.detail}


@dataclass
class SelfCheckReport:
    draft_id: str
    passed: bool
    state: str
    checked_claims: int
    removed_claim_ids: list[str] = field(default_factory=list)
    removed_quiz_ids: list[str] = field(default_factory=list)
    findings: list[ClaimFinding] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    self_check_version: str = SELF_CHECK_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "passed": self.passed,
            "state": self.state,
            "checked_claims": self.checked_claims,
            "removed_claim_ids": self.removed_claim_ids,
            "removed_quiz_ids": self.removed_quiz_ids,
            "missing_required": self.missing_required,
            "findings": [f.to_dict() for f in self.findings],
            "self_check_version": self.self_check_version,
        }


class SourceResolver:
    """Resolves every citation namespace against the stores that own them."""

    def __init__(
        self,
        *,
        corpus_database: Path | str,
        factory_database: Path | str,
        incident_database: Path | str,
        risk_assessment_id: str,
    ) -> None:
        self.corpus_database = Path(corpus_database)
        self.factory_database = Path(factory_database)
        self.incident_database = Path(incident_database)
        self.risk_assessment_id = risk_assessment_id
        self.controls = approved_control_vocabulary(self.factory_database)
        self._path_controls = self._load_path_controls()
        self._cache: dict[str, dict[str, Any] | None] = {}

    def _load_path_controls(self) -> set[str]:
        connection = sqlite3.connect(str(self.factory_database))
        try:
            rows = connection.execute(
                "SELECT safety_control_id FROM risk_assessment_controls "
                "WHERE risk_assessment_id = ?",
                (self.risk_assessment_id,),
            ).fetchall()
        finally:
            connection.close()
        return {row[0] for row in rows}

    def resolve(self, citation: str) -> dict[str, Any] | None:
        if citation in self._cache:
            return self._cache[citation]
        resolved = self._resolve_uncached(citation)
        self._cache[citation] = resolved
        return resolved

    def _resolve_uncached(self, citation: str) -> dict[str, Any] | None:
        namespace, _, identifier = citation.partition(":")
        if not identifier:
            return None

        if namespace == "corpus":
            passage = get_passage(self.corpus_database, identifier)
            if passage is None:
                return None
            return {
                "namespace": "corpus",
                "text": passage["text"],
                "source_type": passage["source_type"],
                "section_role": passage["section_role"],
                "language": passage["language"],
                "label": passage["citation_label"],
            }

        if namespace == "incident":
            connection = sqlite3.connect(str(self.incident_database))
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT incident_key, narrative, employer, event_date, "
                    "hospitalized, amputation, loss_of_eye, nature_title, "
                    "body_part_title, event_title, source_title "
                    "FROM osha_sir_incidents WHERE incident_key = ?",
                    (identifier,),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                return None
            data = dict(row)
            text = " ".join(str(v) for v in data.values() if v not in (None, ""))
            return {"namespace": "incident", "text": text, "label": identifier, "row": data}

        if namespace == "site":
            connection = sqlite3.connect(str(self.factory_database))
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT * FROM risk_assessments WHERE id = ?", (identifier,)
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                return None
            data = dict(row)
            text = " ".join(str(v) for v in data.values() if v not in (None, ""))
            return {
                "namespace": "site",
                "text": text,
                "label": data["assessment_code"],
                "on_path": identifier == self.risk_assessment_id,
            }

        if namespace == "control":
            control = self.controls.get(identifier)
            if control is None:
                return None
            text = " ".join(
                str(v) for v in control.values() if v not in (None, "")
            )
            return {
                "namespace": "control",
                "text": text,
                "label": control["name"],
                "on_path": identifier in self._path_controls,
            }

        return None


def _check_claim(claim: Claim, resolver: SourceResolver) -> list[ClaimFinding]:
    findings: list[ClaimFinding] = []

    # UNKNOWN claims are the one type that needs no source: their whole content
    # is the absence of one. They must still say what is missing.
    if claim.claim_type == CLAIM_UNKNOWN:
        if not claim.text_en.strip():
            findings.append(ClaimFinding(claim.claim_id, CHECK_CITATION_PRESENT, "空的未知项"))
        if not claim.text_es.strip():
            findings.append(ClaimFinding(claim.claim_id, CHECK_BILINGUAL_PRESENT, "缺西语文本"))
        return findings

    if not claim.citations:
        findings.append(ClaimFinding(claim.claim_id, CHECK_CITATION_PRESENT, "无任何出处"))
        return findings

    resolved: list[dict[str, Any]] = []
    for citation in claim.citations:
        source = resolver.resolve(citation)
        if source is None:
            findings.append(
                ClaimFinding(claim.claim_id, CHECK_CITATION_RESOLVES, f"出处无法解析: {citation}")
            )
            continue
        resolved.append(source)
    if not resolved:
        return findings

    namespaces = {s["namespace"] for s in resolved}

    if claim.claim_type == CLAIM_INCIDENT_FACT and "incident" not in namespaces:
        findings.append(
            ClaimFinding(claim.claim_id, CHECK_CITATION_TYPE, "事故事实必须引用事故记录")
        )
    if claim.claim_type == CLAIM_SITE_FACT and not (namespaces & {"site", "control"}):
        findings.append(
            ClaimFinding(claim.claim_id, CHECK_CITATION_TYPE, "本厂事实必须引用风险登记册或已批准控制")
        )
    if claim.claim_type == CLAIM_REQUIREMENT:
        cfr = [s for s in resolved if s["namespace"] == "corpus" and s.get("source_type") == "cfr"]
        if not cfr:
            findings.append(
                ClaimFinding(claim.claim_id, CHECK_CITATION_TYPE, "法规主张必须引用 29 CFR 段落")
            )
    if claim.claim_type == CLAIM_CONTROL:
        actionable = [
            s for s in resolved
            if s["namespace"] == "control"
            or (s["namespace"] == "corpus" and s.get("section_role") in {"REQUIREMENT", "PREVENTION"})
        ]
        if not actionable:
            findings.append(
                ClaimFinding(
                    claim.claim_id, CHECK_CITATION_TYPE,
                    "控制建议只能引用已批准控制或法规/预防章节，不能引用事故叙述",
                )
            )
        if claim.control_id is None:
            findings.append(
                ClaimFinding(claim.claim_id, CHECK_CONTROL_VOCABULARY, "控制建议未指明 control_id")
            )
        elif claim.control_id not in resolver.controls:
            findings.append(
                ClaimFinding(
                    claim.claim_id, CHECK_CONTROL_VOCABULARY,
                    f"控制 {claim.control_id} 不在本厂已批准控制词表中",
                )
            )

    for source in resolved:
        if source["namespace"] in {"site", "control"} and not source.get("on_path", False):
            findings.append(
                ClaimFinding(
                    claim.claim_id, CHECK_SITE_SCOPE,
                    f"引用了不属于本危险路径的本厂记录: {source['label']}",
                )
            )

    if claim.quote:
        target = claim.quote_citation or claim.citations[0]
        source = resolver.resolve(target)
        if source is None or claim.quote not in source["text"]:
            findings.append(
                ClaimFinding(claim.claim_id, CHECK_QUOTE_VERBATIM, "引文与来源不逐字一致")
            )

    # 来源的标签也算来源。一条写着「29 CFR 1910.147 要求……」并且确实引了
    # 1910.147 那一段的主张，此前会因为 29 和 1910.147 不在正文里而被当成
    # 编造数字删掉——删的正好是最该留下的那种主张：说清楚自己依据哪一条。
    # 标签是这份来源的名字，不是模型编的。
    combined = " ".join(
        f"{s['text']} {s.get('label') or ''}" for s in resolved
    )
    supported = set(normalize_numerals(combined))
    english_numerals = set(normalize_numerals(claim.text_en))
    unsupported = sorted(english_numerals - supported)
    if unsupported:
        findings.append(
            ClaimFinding(
                claim.claim_id, CHECK_NUMERALS_SUPPORTED,
                f"数值在来源中不存在: {', '.join(unsupported)}",
            )
        )

    if not claim.text_es.strip():
        findings.append(ClaimFinding(claim.claim_id, CHECK_BILINGUAL_PRESENT, "缺西语文本"))
    else:
        spanish_numerals = set(normalize_numerals(claim.text_es))
        if spanish_numerals != english_numerals:
            findings.append(
                ClaimFinding(
                    claim.claim_id, CHECK_BILINGUAL_NUMERALS,
                    f"中西数值不一致 en={sorted(english_numerals)} es={sorted(spanish_numerals)}",
                )
            )

    return findings


def run_self_check(
    draft: LearningDraft,
    *,
    corpus_database: Path | str,
    factory_database: Path | str,
    incident_database: Path | str,
    repair: bool = True,
) -> SelfCheckReport:
    """Check every claim; with ``repair`` on, delete the ones that fail.

    Deleting rather than flagging is deliberate: a sentence nobody can source
    must not reach a shift briefing while somebody decides what to do about it.
    """
    resolver = SourceResolver(
        corpus_database=corpus_database,
        factory_database=factory_database,
        incident_database=incident_database,
        risk_assessment_id=draft.risk_assessment_id,
    )

    findings: list[ClaimFinding] = []
    failed: set[str] = set()
    claims = draft.all_claims()
    for claim in claims:
        claim_findings = _check_claim(claim, resolver)
        if claim_findings:
            findings.extend(claim_findings)
            failed.add(claim.claim_id)

    removed_quiz: list[str] = []
    if repair and failed:
        draft.briefing_claims = [c for c in draft.briefing_claims if c.claim_id not in failed]
        draft.talk_claims = [c for c in draft.talk_claims if c.claim_id not in failed]

    surviving = {c.claim_id for c in draft.all_claims()}
    kept_quiz = []
    for item in draft.quiz:
        if item.claim_id in surviving:
            kept_quiz.append(item)
        else:
            removed_quiz.append(item.question_id)
            findings.append(
                ClaimFinding(item.claim_id, CHECK_QUIZ_BACKED, f"测验题 {item.question_id} 失去依据")
            )
    if repair:
        draft.quiz = kept_quiz

    present_types = {c.claim_type for c in draft.all_claims()}
    missing = [t for t in REQUIRED_CLAIM_TYPES if t not in present_types]
    if not (present_types & REQUIRED_ANY_OF):
        missing.append("REQUIREMENT_OR_CONTROL")

    if missing:
        state = STATE_NEEDS_HUMAN_WRITER
        passed = False
    elif failed:
        state = STATE_SELF_CHECK_FAIL if not repair else STATE_SELF_CHECK_OK
        passed = repair
    else:
        state = STATE_SELF_CHECK_OK
        passed = True

    draft.state = state
    return SelfCheckReport(
        draft_id=draft.draft_id,
        passed=passed,
        state=state,
        checked_claims=len(claims),
        removed_claim_ids=sorted(failed) if repair else [],
        removed_quiz_ids=removed_quiz,
        findings=findings,
        missing_required=missing,
    )
