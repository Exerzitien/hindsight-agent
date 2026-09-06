"""Create and inspect the fictional Hindsight demo factory database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "factory" / "factory.db"
DEFAULT_ZH_DATABASE_PATH = PROJECT_ROOT / "data" / "factory" / "factory_zh.db"
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "seed" / "demo_metal_factory.json"
DEFAULT_ZH_TRANSLATIONS_PATH = (
    Path(__file__).resolve().parent / "seed" / "demo_metal_factory_zh.json"
)
DEFAULT_STANDARDS_PROFILE_PATH = (
    Path(__file__).resolve().parent / "seed" / "demo_metal_factory_standards.json"
)

CATALOG_TABLES = {
    "tasks": "tasks",
    "hazards": "hazards",
    "worker_roles": "worker_roles",
    "energy_sources": "energy_sources",
    "materials": "materials",
    "safety_controls": "safety_controls",
}

RELATION_TABLES = {
    "tasks": ("equipment_tasks", "task_id"),
    "hazards": ("equipment_hazards", "hazard_id"),
    "worker_roles": ("equipment_worker_roles", "worker_role_id"),
    "energy_sources": ("equipment_energy_sources", "energy_source_id"),
    "materials": ("equipment_materials", "material_id"),
    "safety_controls": ("equipment_safety_controls", "safety_control_id"),
}


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE database_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE standards (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    edition TEXT NOT NULL,
    publisher TEXT NOT NULL,
    status TEXT NOT NULL,
    source_url TEXT NOT NULL,
    alignment_scope TEXT NOT NULL
);

CREATE TABLE schema_standard_mappings (
    id TEXT PRIMARY KEY,
    standard_id TEXT NOT NULL REFERENCES standards(id) ON DELETE CASCADE,
    schema_object TEXT NOT NULL,
    concept TEXT NOT NULL,
    alignment_note TEXT NOT NULL,
    claim_level TEXT NOT NULL CHECK (claim_level = 'concept_alignment')
);

CREATE TABLE control_hierarchy_levels (
    code TEXT PRIMARY KEY,
    rank INTEGER NOT NULL UNIQUE CHECK (rank BETWEEN 1 AND 5),
    name_en TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    principle_en TEXT NOT NULL,
    principle_zh TEXT NOT NULL
);

CREATE TABLE risk_matrix_bands (
    code TEXT PRIMARY KEY,
    minimum_score INTEGER NOT NULL CHECK (minimum_score BETWEEN 1 AND 25),
    maximum_score INTEGER NOT NULL CHECK (maximum_score BETWEEN 1 AND 25),
    name_en TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    response_en TEXT NOT NULL,
    response_zh TEXT NOT NULL,
    CHECK (minimum_score <= maximum_score)
);

CREATE TABLE factories (
    id TEXT PRIMARY KEY,
    site_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    naics TEXT NOT NULL,
    country_code TEXT NOT NULL CHECK (length(country_code) = 2),
    timezone TEXT NOT NULL,
    iec_62264_level TEXT NOT NULL CHECK (iec_62264_level = 'site'),
    description TEXT NOT NULL,
    profile_type TEXT NOT NULL CHECK (profile_type = 'synthetic_composite'),
    source_note TEXT NOT NULL,
    fictional INTEGER NOT NULL CHECK (fictional = 1)
);

CREATE TABLE areas (
    id TEXT PRIMARY KEY,
    factory_id TEXT NOT NULL REFERENCES factories(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    iec_62264_level TEXT NOT NULL CHECK (iec_62264_level = 'area'),
    map_x REAL NOT NULL,
    map_y REAL NOT NULL,
    map_width REAL NOT NULL,
    map_height REAL NOT NULL,
    UNIQUE (factory_id, code)
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

CREATE TABLE hazards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE worker_roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

CREATE TABLE energy_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

CREATE TABLE materials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL
);

CREATE TABLE safety_controls (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    control_type TEXT NOT NULL,
    hierarchy_code TEXT NOT NULL REFERENCES control_hierarchy_levels(code),
    description TEXT NOT NULL,
    performance_requirement TEXT NOT NULL,
    verification_method TEXT NOT NULL,
    verification_interval_days INTEGER NOT NULL CHECK (verification_interval_days > 0)
);

CREATE TABLE equipment (
    id TEXT PRIMARY KEY,
    factory_id TEXT NOT NULL REFERENCES factories(id) ON DELETE CASCADE,
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE RESTRICT,
    owner_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    asset_code TEXT NOT NULL,
    name TEXT NOT NULL,
    equipment_type TEXT NOT NULL,
    iec_62264_level TEXT NOT NULL CHECK (iec_62264_level = 'work_unit'),
    manufacturer TEXT NOT NULL,
    model_number TEXT NOT NULL,
    serial_number TEXT NOT NULL UNIQUE,
    commissioned_on TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('active', 'standby', 'retired')),
    criticality_class TEXT NOT NULL CHECK (criticality_class IN ('A', 'B', 'C')),
    inspection_interval_days INTEGER NOT NULL CHECK (inspection_interval_days > 0),
    last_inspected_on TEXT NOT NULL,
    next_inspection_due TEXT NOT NULL,
    data_origin TEXT NOT NULL CHECK (data_origin = 'synthetic'),
    description TEXT NOT NULL,
    map_x REAL NOT NULL,
    map_y REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE (factory_id, asset_code)
);

CREATE TABLE equipment_tasks (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, task_id)
);

CREATE TABLE equipment_hazards (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    hazard_id TEXT NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, hazard_id)
);

CREATE TABLE equipment_worker_roles (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    worker_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, worker_role_id)
);

CREATE TABLE equipment_energy_sources (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    energy_source_id TEXT NOT NULL REFERENCES energy_sources(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, energy_source_id)
);

CREATE TABLE equipment_materials (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, material_id)
);

CREATE TABLE equipment_safety_controls (
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    safety_control_id TEXT NOT NULL REFERENCES safety_controls(id) ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, safety_control_id)
);

CREATE TABLE risk_assessments (
    id TEXT PRIMARY KEY,
    assessment_code TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    hazard_id TEXT NOT NULL REFERENCES hazards(id) ON DELETE RESTRICT,
    exposed_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    lifecycle_phase TEXT NOT NULL,
    operating_mode TEXT NOT NULL,
    hazardous_situation TEXT NOT NULL,
    foreseeable_misuse TEXT NOT NULL,
    initial_likelihood INTEGER NOT NULL CHECK (initial_likelihood BETWEEN 1 AND 5),
    initial_severity INTEGER NOT NULL CHECK (initial_severity BETWEEN 1 AND 5),
    initial_score INTEGER NOT NULL CHECK (initial_score BETWEEN 1 AND 25),
    initial_level TEXT NOT NULL REFERENCES risk_matrix_bands(code),
    residual_likelihood INTEGER NOT NULL CHECK (residual_likelihood BETWEEN 1 AND 5),
    residual_severity INTEGER NOT NULL CHECK (residual_severity BETWEEN 1 AND 5),
    residual_score INTEGER NOT NULL CHECK (residual_score BETWEEN 1 AND 25),
    residual_level TEXT NOT NULL REFERENCES risk_matrix_bands(code),
    risk_accepted INTEGER NOT NULL CHECK (risk_accepted IN (0, 1)),
    assessor_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    approved_by_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    assessment_date TEXT NOT NULL,
    review_due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('approved', 'action_required', 'superseded')),
    assessment_method TEXT NOT NULL,
    evidence_basis TEXT NOT NULL,
    source_basis TEXT NOT NULL CHECK (source_basis = 'synthetic_rule_based_demo'),
    UNIQUE (equipment_id, task_id, hazard_id, version)
);

CREATE TABLE risk_assessment_controls (
    risk_assessment_id TEXT NOT NULL REFERENCES risk_assessments(id) ON DELETE CASCADE,
    safety_control_id TEXT NOT NULL REFERENCES safety_controls(id) ON DELETE RESTRICT,
    implementation_status TEXT NOT NULL CHECK (implementation_status IN ('implemented', 'planned', 'not_applicable')),
    effectiveness_rating INTEGER NOT NULL CHECK (effectiveness_rating BETWEEN 1 AND 5),
    PRIMARY KEY (risk_assessment_id, safety_control_id)
);

CREATE TABLE control_verifications (
    id TEXT PRIMARY KEY,
    verification_code TEXT NOT NULL UNIQUE,
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    safety_control_id TEXT NOT NULL REFERENCES safety_controls(id) ON DELETE RESTRICT,
    verification_date TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('pass', 'conditional', 'fail')),
    finding TEXT NOT NULL,
    verified_by_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    evidence_reference TEXT NOT NULL,
    next_due_on TEXT NOT NULL,
    source_basis TEXT NOT NULL CHECK (source_basis = 'synthetic_verification_demo'),
    UNIQUE (equipment_id, safety_control_id, verification_date)
);

CREATE TABLE corrective_actions (
    id TEXT PRIMARY KEY,
    action_code TEXT NOT NULL UNIQUE,
    equipment_id TEXT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    risk_assessment_id TEXT REFERENCES risk_assessments(id) ON DELETE SET NULL,
    safety_control_id TEXT REFERENCES safety_controls(id) ON DELETE SET NULL,
    source_verification_id TEXT REFERENCES control_verifications(id) ON DELETE SET NULL,
    standard_id TEXT NOT NULL REFERENCES standards(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    owner_role_id TEXT NOT NULL REFERENCES worker_roles(id) ON DELETE RESTRICT,
    created_on TEXT NOT NULL,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'completed', 'cancelled')),
    completed_on TEXT
);

CREATE INDEX idx_equipment_area ON equipment(area_id);
CREATE INDEX idx_equipment_type ON equipment(equipment_type);
CREATE INDEX idx_equipment_tasks_task ON equipment_tasks(task_id);
CREATE INDEX idx_equipment_hazards_hazard ON equipment_hazards(hazard_id);
CREATE INDEX idx_equipment_roles_role ON equipment_worker_roles(worker_role_id);
CREATE INDEX idx_equipment_energy_energy ON equipment_energy_sources(energy_source_id);
CREATE INDEX idx_risk_equipment ON risk_assessments(equipment_id);
CREATE INDEX idx_risk_hazard ON risk_assessments(hazard_id);
CREATE INDEX idx_risk_residual_level ON risk_assessments(residual_level);
CREATE INDEX idx_verification_due ON control_verifications(next_due_on);
CREATE INDEX idx_action_status_due ON corrective_actions(status, due_on);
"""


CHINESE_VIEWS_SQL = """
CREATE VIEW "工厂" AS
SELECT id AS "工厂ID", site_code AS "站点代码", name AS "工厂名称",
       industry AS "行业", naics AS "NAICS", country_code AS "国家代码",
       timezone AS "时区", iec_62264_level AS "IEC62264层级",
       profile_type AS "档案类型", source_note AS "来源说明"
FROM factories;

CREATE VIEW "区域" AS
SELECT code AS "区域代码", name AS "区域名称", description AS "区域说明",
       iec_62264_level AS "IEC62264层级",
       map_x AS "平面图X", map_y AS "平面图Y",
       map_width AS "宽度", map_height AS "高度"
FROM areas;

CREATE VIEW "设备" AS
SELECT e.asset_code AS "设备编号", e.name AS "设备名称",
       e.equipment_type AS "设备类型", a.code AS "区域代码",
       a.name AS "所在区域", e.manufacturer AS "制造商",
       e.model_number AS "型号", e.serial_number AS "序列号",
       e.criticality_class AS "关键度", e.lifecycle_status AS "生命周期状态",
       e.next_inspection_due AS "下次检查日期", e.description AS "设备说明"
FROM equipment AS e
JOIN areas AS a ON a.id = e.area_id;

CREATE VIEW "设备完整档案" AS
SELECT e.asset_code AS "设备编号", e.name AS "设备名称", a.name AS "所在区域",
       e.criticality_class AS "关键度", e.lifecycle_status AS "生命周期状态",
       (SELECT group_concat(t.name, '、') FROM tasks t
        JOIN equipment_tasks et ON et.task_id=t.id WHERE et.equipment_id=e.id) AS "相关任务",
       (SELECT group_concat(h.name, '、') FROM hazards h
        JOIN equipment_hazards eh ON eh.hazard_id=h.id WHERE eh.equipment_id=e.id) AS "相关危险",
       (SELECT group_concat(r.name, '、') FROM worker_roles r
        JOIN equipment_worker_roles er ON er.worker_role_id=r.id WHERE er.equipment_id=e.id) AS "相关岗位",
       (SELECT group_concat(s.name, '、') FROM energy_sources s
        JOIN equipment_energy_sources es ON es.energy_source_id=s.id WHERE es.equipment_id=e.id) AS "能源来源",
       (SELECT group_concat(c.name, '、') FROM safety_controls c
        JOIN equipment_safety_controls ec ON ec.safety_control_id=c.id WHERE ec.equipment_id=e.id) AS "安全控制",
       (SELECT max(ra.initial_score) FROM risk_assessments ra
        WHERE ra.equipment_id=e.id AND ra.status != 'superseded') AS "最高初始风险分",
       (SELECT max(ra.residual_score) FROM risk_assessments ra
        WHERE ra.equipment_id=e.id AND ra.status != 'superseded') AS "最高剩余风险分",
       e.next_inspection_due AS "下次设备检查日期"
FROM equipment e
JOIN areas a ON a.id=e.area_id;

CREATE VIEW "标准依据" AS
SELECT code AS "标准编号", title AS "标准名称", edition AS "版本",
       publisher AS "发布机构", status AS "状态", alignment_scope AS "本项目对齐范围",
       source_url AS "官方来源"
FROM standards;

CREATE VIEW "资产台账" AS
SELECT e.asset_code AS "资产编号", e.name AS "资产名称", a.name AS "所在区域",
       e.equipment_type AS "设备类型", e.manufacturer AS "制造商",
       e.model_number AS "型号", e.serial_number AS "序列号",
       e.commissioned_on AS "投用日期", e.lifecycle_status AS "生命周期状态",
       e.criticality_class AS "关键度", r.name AS "责任岗位",
       e.inspection_interval_days AS "检查周期天数",
       e.last_inspected_on AS "上次检查日期", e.next_inspection_due AS "下次检查日期",
       e.iec_62264_level AS "IEC62264层级", e.data_origin AS "数据来源类型"
FROM equipment e
JOIN areas a ON a.id=e.area_id
JOIN worker_roles r ON r.id=e.owner_role_id;

CREATE VIEW "风险登记册" AS
SELECT ra.assessment_code AS "评估编号", ra.version AS "版本",
       e.asset_code AS "设备编号", e.name AS "设备名称", a.name AS "所在区域",
       t.name AS "作业任务", h.name AS "危险", exposed.name AS "暴露岗位",
       ra.lifecycle_phase AS "生命周期阶段", ra.operating_mode AS "运行模式",
       ra.hazardous_situation AS "危险情境", ra.foreseeable_misuse AS "可合理预见的误用",
       ra.initial_likelihood AS "初始可能性", ra.initial_severity AS "初始严重度",
       ra.initial_score AS "初始风险分",
       CASE ra.initial_level WHEN 'low' THEN '低' WHEN 'medium' THEN '中'
            WHEN 'high' THEN '高' WHEN 'critical' THEN '重大' END AS "初始风险等级",
       ra.residual_likelihood AS "剩余可能性", ra.residual_severity AS "剩余严重度",
       ra.residual_score AS "剩余风险分",
       CASE ra.residual_level WHEN 'low' THEN '低' WHEN 'medium' THEN '中'
            WHEN 'high' THEN '高' WHEN 'critical' THEN '重大' END AS "剩余风险等级",
       CASE ra.risk_accepted WHEN 1 THEN '可接受' ELSE '需要进一步处置' END AS "风险接受状态",
       assessor.name AS "评估岗位", approver.name AS "审批岗位",
       ra.assessment_date AS "评估日期", ra.review_due_date AS "复审日期",
       CASE ra.status WHEN 'approved' THEN '已批准' WHEN 'action_required' THEN '需要整改'
            ELSE '已被新版本替代' END AS "评估状态",
       ra.assessment_method AS "评估方法", ra.evidence_basis AS "评估依据"
FROM risk_assessments ra
JOIN equipment e ON e.id=ra.equipment_id
JOIN areas a ON a.id=e.area_id
JOIN tasks t ON t.id=ra.task_id
JOIN hazards h ON h.id=ra.hazard_id
JOIN worker_roles exposed ON exposed.id=ra.exposed_role_id
JOIN worker_roles assessor ON assessor.id=ra.assessor_role_id
JOIN worker_roles approver ON approver.id=ra.approved_by_role_id;

CREATE VIEW "控制措施验证" AS
SELECT cv.verification_code AS "验证编号", e.asset_code AS "设备编号",
       e.name AS "设备名称", c.name AS "控制措施", chl.name_zh AS "控制层级",
       c.performance_requirement AS "性能要求", c.verification_method AS "验证方法",
       cv.verification_date AS "验证日期",
       CASE cv.result WHEN 'pass' THEN '通过' WHEN 'conditional' THEN '有条件通过'
            ELSE '不通过' END AS "验证结果",
       cv.finding AS "发现", verifier.name AS "验证岗位",
       cv.evidence_reference AS "证据编号", cv.next_due_on AS "下次验证日期"
FROM control_verifications cv
JOIN equipment e ON e.id=cv.equipment_id
JOIN safety_controls c ON c.id=cv.safety_control_id
JOIN control_hierarchy_levels chl ON chl.code=c.hierarchy_code
JOIN worker_roles verifier ON verifier.id=cv.verified_by_role_id;

CREATE VIEW "整改行动" AS
SELECT ca.action_code AS "行动编号", e.asset_code AS "设备编号", e.name AS "设备名称",
       ca.title AS "整改标题", ca.description AS "整改说明",
       CASE ca.priority WHEN 'low' THEN '低' WHEN 'medium' THEN '中'
            WHEN 'high' THEN '高' ELSE '重大' END AS "优先级",
       owner.name AS "责任岗位", ca.created_on AS "创建日期", ca.due_on AS "截止日期",
       CASE ca.status WHEN 'open' THEN '待处理' WHEN 'in_progress' THEN '处理中'
            WHEN 'completed' THEN '已完成' ELSE '已取消' END AS "状态",
       s.code AS "依据标准"
FROM corrective_actions ca
JOIN equipment e ON e.id=ca.equipment_id
JOIN worker_roles owner ON owner.id=ca.owner_role_id
JOIN standards s ON s.id=ca.standard_id;
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_standards_profile(
    seed: dict[str, Any],
    profile_path: Path,
) -> tuple[dict[str, Any], str]:
    profile_path = Path(profile_path).resolve()
    profile_bytes = profile_path.read_bytes()
    try:
        profile = json.loads(profile_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid standards profile JSON: {profile_path}") from exc

    required = {
        "profile_version",
        "profile_verified_on",
        "data_classification",
        "factory_profile",
        "standards",
        "schema_standard_mappings",
        "control_hierarchy_levels",
        "risk_matrix_bands",
        "asset_profiles",
        "control_profiles",
        "hazard_risk_defaults",
        "verification_exceptions",
    }
    missing = required.difference(profile)
    if missing:
        raise ValueError(f"Standards profile is missing sections: {sorted(missing)}")

    equipment_ids = {item["id"] for item in seed["equipment"]}
    control_ids = {item["id"] for item in seed["safety_controls"]}
    hazard_ids = {item["id"] for item in seed["hazards"]}
    task_ids = {item["id"] for item in seed["tasks"]}
    role_ids = {item["id"] for item in seed["worker_roles"]}

    for section, expected in (
        ("asset_profiles", equipment_ids),
        ("control_profiles", control_ids),
        ("hazard_risk_defaults", hazard_ids),
    ):
        actual = set(profile[section])
        if actual != expected:
            raise ValueError(
                f"Standards profile section {section} does not match the seed; "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )

    hierarchy_codes = {item["code"] for item in profile["control_hierarchy_levels"]}
    for control_id, control in profile["control_profiles"].items():
        if control["hierarchy_code"] not in hierarchy_codes:
            raise ValueError(f"Unknown control hierarchy for {control_id}")

    for asset_id, asset_profile in profile["asset_profiles"].items():
        if asset_profile["owner_role_id"] not in role_ids:
            raise ValueError(f"Unknown asset owner role for {asset_id}")

    for hazard_id, risk_default in profile["hazard_risk_defaults"].items():
        unknown_tasks = set(risk_default["preferred_tasks"]).difference(task_ids)
        unknown_controls = set(risk_default["control_candidates"]).difference(control_ids)
        if unknown_tasks or unknown_controls:
            raise ValueError(
                f"Unknown standards-profile references for {hazard_id}: "
                f"tasks={sorted(unknown_tasks)}, controls={sorted(unknown_controls)}"
            )

    equipment_by_id = {item["id"]: item for item in seed["equipment"]}
    for exception in profile["verification_exceptions"]:
        equipment_id = exception["equipment_id"]
        control_id = exception["safety_control_id"]
        if equipment_id not in equipment_by_id:
            raise ValueError(f"Unknown verification equipment: {equipment_id}")
        if control_id not in equipment_by_id[equipment_id]["safety_controls"]:
            raise ValueError(
                f"Verification exception {equipment_id}:{control_id} is not an asset control"
            )
        if exception["owner_role_id"] not in role_ids:
            raise ValueError(f"Unknown corrective-action owner for {equipment_id}:{control_id}")

    return profile, hashlib.sha256(profile_bytes).hexdigest()


def _risk_band(score: int, profile: dict[str, Any]) -> str:
    for band in profile["risk_matrix_bands"]:
        if band["minimum_score"] <= score <= band["maximum_score"]:
            return band["code"]
    raise ValueError(f"Risk score is outside the configured matrix: {score}")


def _operating_mode(task_id: str) -> str:
    if task_id == "task-die-change":
        return "setup"
    if "maintenance" in task_id:
        return "maintenance"
    if task_id in {"task-jam-clear", "task-spill-cleanup", "task-cylinder-change"}:
        return "intervention"
    if task_id in {
        "task-forklift-driving",
        "task-pallet-stacking",
        "task-loading",
        "task-trailer-entry",
    }:
        return "material_handling"
    return "normal_operation"


def _build_risk_records(
    seed: dict[str, Any],
    profile: dict[str, Any],
    locale: str,
) -> list[dict[str, Any]]:
    tasks = {item["id"]: item for item in seed["tasks"]}
    hazards = {item["id"]: item for item in seed["hazards"]}
    controls = profile["control_profiles"]
    is_zh = locale.casefold().startswith("zh")
    records: list[dict[str, Any]] = []

    for asset in seed["equipment"]:
        for hazard_id in asset["hazards"]:
            default = profile["hazard_risk_defaults"][hazard_id]
            task_id = next(
                (
                    candidate
                    for candidate in default["preferred_tasks"]
                    if candidate in asset["tasks"]
                ),
                asset["tasks"][0],
            )
            exposed_role_id = next(
                (
                    role_id
                    for role_id in asset["worker_roles"]
                    if role_id != "role-maintenance"
                ),
                asset["worker_roles"][0],
            )
            applicable_controls = [
                control_id
                for control_id in asset["safety_controls"]
                if control_id in default["control_candidates"]
            ]
            engineering_count = sum(
                controls[control_id]["hierarchy_code"] == "engineering"
                for control_id in applicable_controls
            )
            administrative_count = sum(
                controls[control_id]["hierarchy_code"] == "administrative"
                for control_id in applicable_controls
            )
            if engineering_count >= 2:
                likelihood_reduction = 3
            elif engineering_count == 1:
                likelihood_reduction = 2
            elif administrative_count:
                likelihood_reduction = 1
            else:
                likelihood_reduction = 0

            initial_likelihood = default["likelihood"]
            initial_severity = default["severity"]
            initial_score = initial_likelihood * initial_severity
            residual_likelihood = max(1, initial_likelihood - likelihood_reduction)
            residual_severity = initial_severity
            residual_score = residual_likelihood * residual_severity
            risk_accepted = residual_score <= 9
            assessment_number = len(records) + 1

            if is_zh:
                hazardous_situation = (
                    f"在{asset['name']}执行{tasks[task_id]['name']}时，作业人员可能暴露于"
                    f"{hazards[hazard_id]['name']}。"
                )
                foreseeable_misuse = (
                    "未按规定停机、隔离能源或绕过现有防护后进行干预。"
                    if _operating_mode(task_id) in {"maintenance", "intervention", "setup"}
                    else "偏离规定作业方法、超载或绕过现有控制措施。"
                )
                evidence_basis = (
                    "依据合成设备-任务-危险关系和现有控制措施，使用示例性 5x5 风险矩阵估算；"
                    "必须由现场有资质人员复核后才能用于企业决策。"
                )
            else:
                hazardous_situation = (
                    f"While performing {tasks[task_id]['name']} on {asset['name']}, "
                    f"a worker may be exposed to {hazards[hazard_id]['name']}."
                )
                foreseeable_misuse = (
                    "Intervention without the required stop, energy isolation, or safeguarding."
                    if _operating_mode(task_id) in {"maintenance", "intervention", "setup"}
                    else "Departure from the defined method, overloading, or bypass of controls."
                )
                evidence_basis = (
                    "Estimated from synthetic asset-task-hazard relationships and existing "
                    "controls using a demonstrative 5x5 matrix; site validation by competent "
                    "personnel is required before business use."
                )

            records.append(
                {
                    "id": f"risk-{asset['id']}-{hazard_id.removeprefix('haz-')}",
                    "assessment_code": f"RA-2026-{assessment_number:03d}",
                    "version": 1,
                    "equipment_id": asset["id"],
                    "task_id": task_id,
                    "hazard_id": hazard_id,
                    "exposed_role_id": exposed_role_id,
                    "lifecycle_phase": "use",
                    "operating_mode": _operating_mode(task_id),
                    "hazardous_situation": hazardous_situation,
                    "foreseeable_misuse": foreseeable_misuse,
                    "initial_likelihood": initial_likelihood,
                    "initial_severity": initial_severity,
                    "initial_score": initial_score,
                    "initial_level": _risk_band(initial_score, profile),
                    "residual_likelihood": residual_likelihood,
                    "residual_severity": residual_severity,
                    "residual_score": residual_score,
                    "residual_level": _risk_band(residual_score, profile),
                    "risk_accepted": int(risk_accepted),
                    "assessor_role_id": "role-ehs-manager",
                    "approved_by_role_id": "role-production-supervisor",
                    "assessment_date": "2026-08-25",
                    "review_due_date": "2027-08-25",
                    "status": "approved" if risk_accepted else "action_required",
                    "assessment_method": "organizational_demo_5x5_likelihood_times_severity",
                    "evidence_basis": evidence_basis,
                    "source_basis": "synthetic_rule_based_demo",
                    "control_ids": applicable_controls,
                }
            )
    return records


def _build_verification_records(
    seed: dict[str, Any],
    profile: dict[str, Any],
    locale: str,
) -> list[dict[str, Any]]:
    exceptions = {
        (item["equipment_id"], item["safety_control_id"]): item
        for item in profile["verification_exceptions"]
    }
    is_zh = locale.casefold().startswith("zh")
    records: list[dict[str, Any]] = []
    for asset in seed["equipment"]:
        asset_profile = profile["asset_profiles"][asset["id"]]
        verification_date = date.fromisoformat(asset_profile["last_inspected_on"])
        for control_id in asset["safety_controls"]:
            control_profile = profile["control_profiles"][control_id]
            exception = exceptions.get((asset["id"], control_id))
            finding = (
                exception["finding_zh" if is_zh else "finding_en"]
                if exception
                else (
                    "本次示例验证未发现影响控制功能的异常。"
                    if is_zh
                    else "No condition affecting the control function was identified in this demo verification."
                )
            )
            number = len(records) + 1
            records.append(
                {
                    "id": f"verification-{asset['id']}-{control_id.removeprefix('control-')}",
                    "verification_code": f"CV-2026-{number:03d}",
                    "equipment_id": asset["id"],
                    "safety_control_id": control_id,
                    "verification_date": verification_date.isoformat(),
                    "result": exception["result"] if exception else "pass",
                    "finding": finding,
                    "verified_by_role_id": "role-ehs-manager",
                    "evidence_reference": (
                        f"SYN-CV-{asset['asset_code'].upper()}-"
                        f"{control_id.removeprefix('control-').upper()}-"
                        f"{verification_date.strftime('%Y%m%d')}"
                    ),
                    "next_due_on": (
                        verification_date
                        + timedelta(days=control_profile["verification_interval_days"])
                    ).isoformat(),
                    "source_basis": "synthetic_verification_demo",
                }
            )
    return records


def _load_seed(
    seed_path: Path,
    translations_path: Path | None = None,
) -> tuple[dict[str, Any], str, str]:
    seed_bytes = seed_path.read_bytes()
    try:
        seed = json.loads(seed_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid factory seed JSON: {seed_path}") from exc

    required = {"schema_version", "factory", "areas", "equipment", *CATALOG_TABLES}
    missing = required.difference(seed)
    if missing:
        raise ValueError(f"Factory seed is missing sections: {sorted(missing)}")

    for section in ["areas", "equipment", *CATALOG_TABLES]:
        ids = [item["id"] for item in seed[section]]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate IDs in factory seed section: {section}")

    area_ids = {area["id"] for area in seed["areas"]}
    catalog_ids = {
        key: {item["id"] for item in seed[key]}
        for key in CATALOG_TABLES
    }
    for asset in seed["equipment"]:
        if asset["area_id"] not in area_ids:
            raise ValueError(f"Unknown area {asset['area_id']} for {asset['id']}")
        for relation in RELATION_TABLES:
            unknown = set(asset.get(relation, [])).difference(catalog_ids[relation])
            if unknown:
                raise ValueError(
                    f"Unknown {relation} references for {asset['id']}: {sorted(unknown)}"
                )

    locale = "en-US"
    if translations_path is not None:
        translations_path = Path(translations_path).resolve()
        try:
            translations = json.loads(translations_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid factory translation JSON: {translations_path}") from exc
        locale = translations.get("locale", "zh-CN")
        seed["factory"].update(translations.get("factory", {}))
        for section in ["areas", "equipment", *CATALOG_TABLES]:
            section_translations = translations.get(section, {})
            for item in seed[section]:
                item.update(section_translations.get(item["id"], {}))

    effective_bytes = json.dumps(
        seed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return seed, hashlib.sha256(effective_bytes).hexdigest(), locale


def create_factory_database(
    output_path: Path = DEFAULT_DATABASE_PATH,
    seed_path: Path = DEFAULT_SEED_PATH,
    replace: bool = False,
    translations_path: Path | None = None,
    standards_profile_path: Path = DEFAULT_STANDARDS_PROFILE_PATH,
) -> dict[str, Any]:
    """Create the standard-aligned demo factory atomically from versioned seeds."""
    output_path = Path(output_path).resolve()
    seed_path = Path(seed_path).resolve()
    standards_profile_path = Path(standards_profile_path).resolve()
    if output_path.exists() and not replace:
        return {
            "status": "exists",
            "message": "Factory database already exists; it was not overwritten.",
            "database": str(output_path),
            "summary": database_summary(output_path),
        }

    seed, seed_hash, locale = _load_seed(seed_path, translations_path)
    profile, profile_hash = _load_standards_profile(seed, standards_profile_path)
    factory = seed["factory"]
    risk_records = _build_risk_records(seed, profile, locale)
    verification_records = _build_verification_records(seed, profile, locale)

    risk_control_index: dict[tuple[str, str], str] = {}
    for record in risk_records:
        for control_id in record["control_ids"]:
            risk_control_index.setdefault(
                (record["equipment_id"], control_id),
                record["id"],
            )
    verification_index = {
        (record["equipment_id"], record["safety_control_id"]): record
        for record in verification_records
    }
    is_zh = locale.casefold().startswith("zh")
    corrective_actions = []
    for number, exception in enumerate(profile["verification_exceptions"], start=1):
        key = (exception["equipment_id"], exception["safety_control_id"])
        verification = verification_index[key]
        corrective_actions.append(
            {
                "id": f"corrective-action-{number:03d}",
                "action_code": exception["action_code"],
                "equipment_id": exception["equipment_id"],
                "risk_assessment_id": risk_control_index.get(key),
                "safety_control_id": exception["safety_control_id"],
                "source_verification_id": verification["id"],
                "standard_id": "iso-45001-2018",
                "title": exception["action_title_zh" if is_zh else "action_title_en"],
                "description": exception[
                    "action_description_zh" if is_zh else "action_description_en"
                ],
                "priority": exception["priority"],
                "owner_role_id": exception["owner_role_id"],
                "created_on": verification["verification_date"],
                "due_on": exception["due_on"],
                "status": exception["status"],
                "completed_on": None,
            }
        )
    equipment_by_id = {item["id"]: item for item in seed["equipment"]}
    hazards_by_id = {item["id"]: item for item in seed["hazards"]}
    action_number = len(corrective_actions)
    for record in risk_records:
        if record["risk_accepted"]:
            continue
        action_number += 1
        asset = equipment_by_id[record["equipment_id"]]
        hazard = hazards_by_id[record["hazard_id"]]
        if is_zh:
            title = f"降低{asset['name']}的{hazard['name']}剩余风险"
            description = (
                "由责任岗位组织现场复评，优先考虑消除、替代或工程控制；实施后重新验证"
                "可能性、严重度和风险接受状态。"
            )
        else:
            title = f"Reduce residual {hazard['name']} risk on {asset['name']}"
            description = (
                "Perform a competent site review, prioritize elimination, substitution, "
                "or engineering controls, and reassess likelihood, severity, and acceptance "
                "after implementation."
            )
        corrective_actions.append(
            {
                "id": f"corrective-action-{action_number:03d}",
                "action_code": f"CA-2026-{action_number:03d}",
                "equipment_id": record["equipment_id"],
                "risk_assessment_id": record["id"],
                "safety_control_id": None,
                "source_verification_id": None,
                "standard_id": "iso-45001-2018",
                "title": title,
                "description": description,
                "priority": "critical" if record["residual_score"] >= 17 else "high",
                "owner_role_id": profile["asset_profiles"][record["equipment_id"]][
                    "owner_role_id"
                ],
                "created_on": record["assessment_date"],
                "due_on": "2026-10-31",
                "status": "open",
                "completed_on": None,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.unlink(missing_ok=True)

    connection = sqlite3.connect(temporary_path)
    try:
        connection.executescript(SCHEMA_SQL)
        with connection:
            connection.executemany(
                "INSERT INTO database_metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", seed["schema_version"]),
                    ("seed_sha256", seed_hash),
                    ("standards_profile_sha256", profile_hash),
                    ("created_at", _utc_now()),
                    ("seed_file", seed_path.name),
                    ("standards_profile_file", standards_profile_path.name),
                    ("locale", locale),
                    ("data_classification", profile["data_classification"]),
                    ("standards_verified_on", profile["profile_verified_on"]),
                ],
            )
            connection.executemany(
                """
                INSERT INTO standards(
                    id, code, title, edition, publisher, status,
                    source_url, alignment_scope
                ) VALUES (
                    :id, :code, :title, :edition, :publisher, :status,
                    :source_url, :alignment_scope
                )
                """,
                profile["standards"],
            )
            connection.executemany(
                """
                INSERT INTO schema_standard_mappings(
                    id, standard_id, schema_object, concept, alignment_note, claim_level
                ) VALUES (
                    :id, :standard_id, :schema_object, :concept, :alignment_note, :claim_level
                )
                """,
                profile["schema_standard_mappings"],
            )
            connection.executemany(
                """
                INSERT INTO control_hierarchy_levels(
                    code, rank, name_en, name_zh, principle_en, principle_zh
                ) VALUES (
                    :code, :rank, :name_en, :name_zh, :principle_en, :principle_zh
                )
                """,
                profile["control_hierarchy_levels"],
            )
            connection.executemany(
                """
                INSERT INTO risk_matrix_bands(
                    code, minimum_score, maximum_score, name_en, name_zh,
                    response_en, response_zh
                ) VALUES (
                    :code, :minimum_score, :maximum_score, :name_en, :name_zh,
                    :response_en, :response_zh
                )
                """,
                profile["risk_matrix_bands"],
            )
            factory_profile = profile["factory_profile"]
            connection.execute(
                """
                INSERT INTO factories(
                    id, site_code, name, industry, naics, country_code,
                    timezone, iec_62264_level, description,
                    profile_type, source_note, fictional
                ) VALUES (
                    :id, :site_code, :name, :industry, :naics, :country_code,
                    :timezone, :iec_62264_level, :description,
                    :profile_type, :source_note, 1
                )
                """,
                dict(factory, **factory_profile),
            )
            connection.executemany(
                """
                INSERT INTO areas(
                    id, factory_id, code, name, description,
                    iec_62264_level, map_x, map_y, map_width, map_height
                ) VALUES (
                    :id, :factory_id, :code, :name, :description,
                    'area', :map_x, :map_y, :map_width, :map_height
                )
                """,
                [dict(area, factory_id=factory["id"]) for area in seed["areas"]],
            )
            connection.executemany(
                "INSERT INTO tasks(id, name, description) VALUES (:id, :name, :description)",
                seed["tasks"],
            )
            connection.executemany(
                """
                INSERT INTO hazards(id, name, category, description)
                VALUES (:id, :name, :category, :description)
                """,
                seed["hazards"],
            )
            for table in ("worker_roles", "energy_sources", "materials"):
                connection.executemany(
                    f"INSERT INTO {table}(id, name, description) VALUES (:id, :name, :description)",
                    seed[table],
                )
            connection.executemany(
                """
                INSERT INTO safety_controls(
                    id, name, control_type, hierarchy_code, description,
                    performance_requirement, verification_method,
                    verification_interval_days
                ) VALUES (
                    :id, :name, :control_type, :hierarchy_code, :description,
                    :performance_requirement, :verification_method,
                    :verification_interval_days
                )
                """,
                [
                    dict(
                        control,
                        hierarchy_code=profile["control_profiles"][control["id"]][
                            "hierarchy_code"
                        ],
                        performance_requirement=control.get(
                            "performance_requirement",
                            profile["control_profiles"][control["id"]][
                                "performance_requirement"
                            ],
                        ),
                        verification_method=control.get(
                            "verification_method",
                            profile["control_profiles"][control["id"]][
                                "verification_method"
                            ],
                        ),
                        verification_interval_days=profile["control_profiles"][
                            control["id"]
                        ]["verification_interval_days"],
                    )
                    for control in seed["safety_controls"]
                ],
            )

            for asset in seed["equipment"]:
                asset_profile = profile["asset_profiles"][asset["id"]]
                connection.execute(
                    """
                    INSERT INTO equipment(
                        id, factory_id, area_id, owner_role_id, asset_code, name,
                        equipment_type, iec_62264_level, manufacturer, model_number,
                        serial_number, commissioned_on, lifecycle_status,
                        criticality_class, inspection_interval_days,
                        last_inspected_on, next_inspection_due, data_origin,
                        description, map_x, map_y, active
                    ) VALUES (
                        :id, :factory_id, :area_id, :owner_role_id, :asset_code, :name,
                        :equipment_type, 'work_unit', :manufacturer, :model_number,
                        :serial_number, :commissioned_on, :lifecycle_status,
                        :criticality_class, :inspection_interval_days,
                        :last_inspected_on, :next_inspection_due, 'synthetic',
                        :description, :map_x, :map_y, 1
                    )
                    """,
                    dict(asset, factory_id=factory["id"], **asset_profile),
                )
                for relation, (table, target_column) in RELATION_TABLES.items():
                    connection.executemany(
                        f"INSERT INTO {table}(equipment_id, {target_column}) VALUES (?, ?)",
                        [(asset["id"], target_id) for target_id in asset.get(relation, [])],
                    )

            connection.executemany(
                """
                INSERT INTO risk_assessments(
                    id, assessment_code, version, equipment_id, task_id, hazard_id,
                    exposed_role_id, lifecycle_phase, operating_mode,
                    hazardous_situation, foreseeable_misuse,
                    initial_likelihood, initial_severity, initial_score, initial_level,
                    residual_likelihood, residual_severity, residual_score, residual_level,
                    risk_accepted, assessor_role_id, approved_by_role_id,
                    assessment_date, review_due_date, status, assessment_method,
                    evidence_basis, source_basis
                ) VALUES (
                    :id, :assessment_code, :version, :equipment_id, :task_id, :hazard_id,
                    :exposed_role_id, :lifecycle_phase, :operating_mode,
                    :hazardous_situation, :foreseeable_misuse,
                    :initial_likelihood, :initial_severity, :initial_score, :initial_level,
                    :residual_likelihood, :residual_severity, :residual_score, :residual_level,
                    :risk_accepted, :assessor_role_id, :approved_by_role_id,
                    :assessment_date, :review_due_date, :status, :assessment_method,
                    :evidence_basis, :source_basis
                )
                """,
                risk_records,
            )
            risk_control_rows = []
            for record in risk_records:
                for control_id in record["control_ids"]:
                    hierarchy_code = profile["control_profiles"][control_id][
                        "hierarchy_code"
                    ]
                    effectiveness = 4 if hierarchy_code == "engineering" else 3
                    risk_control_rows.append(
                        (record["id"], control_id, "implemented", effectiveness)
                    )
            connection.executemany(
                """
                INSERT INTO risk_assessment_controls(
                    risk_assessment_id, safety_control_id,
                    implementation_status, effectiveness_rating
                ) VALUES (?, ?, ?, ?)
                """,
                risk_control_rows,
            )
            connection.executemany(
                """
                INSERT INTO control_verifications(
                    id, verification_code, equipment_id, safety_control_id,
                    verification_date, result, finding, verified_by_role_id,
                    evidence_reference, next_due_on, source_basis
                ) VALUES (
                    :id, :verification_code, :equipment_id, :safety_control_id,
                    :verification_date, :result, :finding, :verified_by_role_id,
                    :evidence_reference, :next_due_on, :source_basis
                )
                """,
                verification_records,
            )
            connection.executemany(
                """
                INSERT INTO corrective_actions(
                    id, action_code, equipment_id, risk_assessment_id,
                    safety_control_id, source_verification_id, standard_id,
                    title, description, priority, owner_role_id,
                    created_on, due_on, status, completed_on
                ) VALUES (
                    :id, :action_code, :equipment_id, :risk_assessment_id,
                    :safety_control_id, :source_verification_id, :standard_id,
                    :title, :description, :priority, :owner_role_id,
                    :created_on, :due_on, :status, :completed_on
                )
                """,
                corrective_actions,
            )

            if locale.casefold().startswith("zh"):
                connection.executescript(CHINESE_VIEWS_SQL)

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"Factory database has foreign-key violations: {violations}")
    except Exception:
        connection.close()
        temporary_path.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    os.replace(temporary_path, output_path)
    return {
        "status": "created" if not replace else "replaced",
        "message": "Fictional demo factory database created successfully.",
        "database": str(output_path),
        "seed_sha256": seed_hash,
        "standards_profile_sha256": profile_hash,
        "locale": locale,
        "summary": database_summary(output_path),
    }


def database_summary(database_path: Path = DEFAULT_DATABASE_PATH) -> dict[str, Any]:
    database_path = Path(database_path).resolve()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        factory = dict(
            connection.execute(
                """
                SELECT id, site_code, name, industry, naics, country_code, timezone,
                       iec_62264_level, profile_type, source_note, fictional
                FROM factories
                """
            ).fetchone()
        )
        count_tables = [
            "areas",
            "equipment",
            "tasks",
            "hazards",
            "worker_roles",
            "energy_sources",
            "materials",
            "safety_controls",
            "standards",
            "schema_standard_mappings",
            "risk_assessments",
            "risk_assessment_controls",
            "control_verifications",
            "corrective_actions",
        ]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in count_tables
        }
        hierarchy_rows = connection.execute(
            """
            SELECT a.code, a.name AS area_name, e.asset_code, e.name AS equipment_name,
                   e.equipment_type
            FROM areas AS a
            LEFT JOIN equipment AS e ON e.area_id = a.id
            ORDER BY a.code, e.asset_code
            """
        ).fetchall()
    finally:
        connection.close()

    hierarchy: dict[str, dict[str, Any]] = {}
    for row in hierarchy_rows:
        area = hierarchy.setdefault(
            row["code"],
            {"name": row["area_name"], "equipment": []},
        )
        if row["asset_code"]:
            area["equipment"].append(
                {
                    "asset_code": row["asset_code"],
                    "name": row["equipment_name"],
                    "type": row["equipment_type"],
                }
            )
    return {"factory": factory, "counts": counts, "areas": hierarchy}


def _parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser


def main() -> int:
    parser = _parser("Create the standard-aligned fictional Hindsight demo factory database.")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED_PATH)
    parser.add_argument("--translations", type=Path)
    parser.add_argument(
        "--standards-profile",
        type=Path,
        default=DEFAULT_STANDARDS_PROFILE_PATH,
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    result = create_factory_database(
        args.database,
        args.seed,
        args.replace,
        args.translations,
        args.standards_profile,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def zh_main() -> int:
    parser = argparse.ArgumentParser(description="创建中文版 Hindsight 虚拟工厂数据库。")
    parser.add_argument("--database", type=Path, default=DEFAULT_ZH_DATABASE_PATH)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    result = create_factory_database(
        output_path=args.database,
        seed_path=DEFAULT_SEED_PATH,
        replace=args.replace,
        translations_path=DEFAULT_ZH_TRANSLATIONS_PATH,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def show_main() -> int:
    parser = _parser("Show the fictional Hindsight demo factory hierarchy.")
    args = parser.parse_args()
    if not args.database.exists():
        parser.error(f"Factory database does not exist: {args.database}")
    print(json.dumps(database_summary(args.database), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
