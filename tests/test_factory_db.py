from __future__ import annotations

import sqlite3
from pathlib import Path

from hindsight_agent.factory_db import (
    DEFAULT_ZH_TRANSLATIONS_PATH,
    create_factory_database,
    database_summary,
)


def test_creates_expected_fictional_factory(tmp_path: Path) -> None:
    database = tmp_path / "factory.db"
    result = create_factory_database(database)

    assert result["status"] == "created"
    summary = result["summary"]
    assert summary["factory"]["fictional"] == 1
    assert summary["counts"]["areas"] == 6
    assert summary["counts"]["equipment"] == 20
    assert summary["counts"]["tasks"] == 22
    assert summary["counts"]["hazards"] == 21
    assert summary["counts"]["standards"] == 5
    assert summary["counts"]["risk_assessments"] == 97
    assert summary["counts"]["control_verifications"] == 66


def test_press_relationships_support_evidence_matching(tmp_path: Path) -> None:
    database = tmp_path / "factory.db"
    create_factory_database(database)
    connection = sqlite3.connect(database)
    try:
        tasks = {
            row[0]
            for row in connection.execute(
                """
                SELECT t.name FROM tasks AS t
                JOIN equipment_tasks AS et ON et.task_id = t.id
                WHERE et.equipment_id = 'press-01'
                """
            )
        }
        hazards = {
            row[0]
            for row in connection.execute(
                """
                SELECT h.name FROM hazards AS h
                JOIN equipment_hazards AS eh ON eh.hazard_id = h.id
                WHERE eh.equipment_id = 'press-01'
                """
            )
        }
    finally:
        connection.close()

    assert "jam clearing" in tasks
    assert "unexpected startup" in hazards
    assert "amputation" in hazards


def test_existing_database_is_not_overwritten_by_default(tmp_path: Path) -> None:
    database = tmp_path / "factory.db"
    create_factory_database(database)
    before = database.read_bytes()

    result = create_factory_database(database)

    assert result["status"] == "exists"
    assert database.read_bytes() == before
    assert database_summary(database)["counts"]["equipment"] == 20


def test_creates_chinese_database_and_views(tmp_path: Path) -> None:
    database = tmp_path / "factory_zh.db"
    result = create_factory_database(
        database,
        translations_path=DEFAULT_ZH_TRANSLATIONS_PATH,
    )

    connection = sqlite3.connect(database)
    try:
        press = connection.execute(
            'SELECT "设备名称", "所在区域", "相关任务" FROM "设备完整档案" '
            'WHERE "设备编号" = ?',
            ("Press-01",),
        ).fetchone()
        risk = connection.execute(
            'SELECT "设备名称", "危险", "初始风险分", "剩余风险分" '
            'FROM "风险登记册" WHERE "设备编号" = ? AND "危险" = ?',
            ("Press-02", "截肢"),
        ).fetchone()
        standard_count = connection.execute('SELECT COUNT(*) FROM "标准依据"').fetchone()[0]
    finally:
        connection.close()

    assert result["locale"] == "zh-CN"
    assert press[0] == "机械压力机 01"
    assert press[1] == "冲压区"
    assert "清理卡料" in press[2]
    assert risk[0] == "机械压力机 02"
    assert risk[2] > risk[3]
    assert standard_count == 5


def test_standard_alignment_is_traceable_and_risks_are_closed_loop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "factory.db"
    create_factory_database(database)
    connection = sqlite3.connect(database)
    try:
        standards = {
            row[0]: row[1]
            for row in connection.execute("SELECT code, source_url FROM standards")
        }
        asset = connection.execute(
            """
            SELECT manufacturer, model_number, serial_number, commissioned_on,
                   criticality_class, owner_role_id, iec_62264_level, data_origin
            FROM equipment WHERE id = 'press-01'
            """
        ).fetchone()
        unowned_unaccepted_risks = connection.execute(
            """
            SELECT COUNT(*)
            FROM risk_assessments AS ra
            LEFT JOIN corrective_actions AS ca ON ca.risk_assessment_id = ra.id
            WHERE ra.risk_accepted = 0 AND ca.id IS NULL
            """
        ).fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert "ISO 45001:2018" in standards
    assert standards["ISO 55000:2024"].startswith("https://www.iso.org/")
    assert all(asset)
    assert asset[6] == "work_unit"
    assert asset[7] == "synthetic"
    assert unowned_unaccepted_risks == 0
    assert violations == []
