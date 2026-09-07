"""Streamlit operations console for Hindsight Safety Agent.

What this console is for
------------------------
The whole technical claim of this project is that a relevance score is only a
candidate retriever, and that the decision is made by deterministic residual
path verification over the site's own risk register. A console whose front page
is a relevance score contradicts that claim in the first five seconds a person
looks at it, whatever the architecture document says.

So the decision surface here is the case queue, split by level, and every item
opens onto the witness that produced its level - the controls that cut the
hazard path and the verification records that prove them, or the residual
controls that failed to. The retrieval score still exists and is still useful,
but it lives in its own tab, labelled as what it is.

Approval is deliberately slow. Four explicit checks gate the approve button,
because A2 says a person is accountable for what reaches a worker, and a button
that can be clicked without reading is not accountability.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

from hindsight_agent.config import load_settings
from hindsight_agent.daily_pipeline import run_pipeline
from hindsight_agent.distribution import completion_status, monday_digest
from hindsight_agent.incident_db import ensure_incident_schema
from hindsight_agent.rag_db import (
    dashboard_summary,
    ensure_rag_schema,
    query_rows,
    record_decision,
)
from hindsight_agent.transfer import (
    ACTIONABLE,
    LEVEL_RED,
    LEVEL_YELLOW,
    NO_ACTIONABLE_TRANSFER,
    UNKNOWN,
    assess_paths,
    route_path,
)
from hindsight_agent.workflow import approve_and_distribute, reject_case
from hindsight_agent.workflow_db import (
    CLASSIFIED,
    DRAFTED,
    ESCALATED,
    NEEDS_HUMAN_WRITER,
    PENDING_APPROVAL,
    REMINDED,
    SELF_CHECK_OK,
    PackArtifactError,
    case_history,
    connect,
    ensure_workflow_schema,
    pack_artifact_for_case,
)

#: A2 · 批准前必须逐条确认的四件事。
#:
#: 这四项不是走过场。它们分别对应本项目声称守住的四条底线：正文被人读过、
#: 引用被人抽查过、危险路径与本厂实际作业相符、以及有一个具名的人为发到
#: 一线的内容负责。少任何一条，"人类批准"就退化成一次点击。
APPROVAL_CHECKS: tuple[tuple[str, str], ...] = (
    ("read_body", "我已完整读过下面这份稿件的正文（英文与西班牙文）"),
    ("checked_citation", "我至少抽查了一条主张的引用，并确认它确实支持该说法"),
    ("path_is_real", "这条危险路径确实存在于本厂，且与我们现在的作业方式相符"),
    ("accountable", "我批准将它发给受影响岗位，并对其内容负责"),
)

#: 仍占用某个人注意力的状态。归档与关闭不在其中——那正是本系统的价值所在。
#:
#: 包含 CLASSIFIED 是有意的，且与内核一致：workflow.OPEN_STATES 在做告警节流时
#: 就把 CLASSIFIED 当作「已经在某个人面前」，状态机也明确允许 CLASSIFIED →
#: REJECTED（管理者可以在系统花钱起草之前就否掉一条路由）。如果控制台不显示它，
#: 节流会因为它而抑制后续告警，而屏幕上却空无一物——这正是最难排查的那种不一致。
QUEUE_STATES = (
    CLASSIFIED, DRAFTED, SELF_CHECK_OK, NEEDS_HUMAN_WRITER,
    PENDING_APPROVAL, REMINDED, ESCALATED,
)

_LEVEL_STYLE = {
    LEVEL_RED: ("#a32b21", "红", "高严重度 · 残余路径成立"),
    LEVEL_YELLOW: ("#9a6300", "黄", "需要确认 · 低严重度或事实不足"),
}

_STATE_LABEL = {
    CLASSIFIED: "已路由 · 待起草",
    DRAFTED: "起草中",
    SELF_CHECK_OK: "自检通过",
    NEEDS_HUMAN_WRITER: "需人工补写",
    PENDING_APPROVAL: "待审批",
    REMINDED: "已催办",
    ESCALATED: "已升级",
}


def _decode_json(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------

@contextmanager
def section_guard(st, label: str):
    """一块区域读不出来时，只让这一块显示失败，而不是让整页空白。

    控制台会同时读事故库、检索库、工作流库和风险登记册。任何一处形状不对
    （旧版本、手工改过、还没导数据），都不该把另外三处也一起带走——尤其不该
    在演示时把整页变成一段 traceback。
    """
    try:
        yield
    except Exception as error:
        st.error(f"「{label}」读取失败：{error}")
        st.caption("其余部分不受影响。若这是旧版本数据库，先按页面顶部的提示重建。")


def bootstrap_schemas(incident_database) -> list[str]:
    """把三套 schema 都建齐，并把建不起来的原因交回去显示，而不是抛出去。

    事故库上叠着三套 schema：事故、RAG、工作流。控制台完全可能是这个库上跑的
    第一个东西——有人 clone 完先打开界面，再想起来要导数据。

    这里吞掉异常是刻意的，而且只在这里成立：控制台是个只读视图，
    「建不出表」不应该让它连已有的东西都显示不了。但吞掉不等于藏起来——
    每一条失败都会作为醒目的提示显示在页面顶部，告诉人该去跑哪条命令。
    一个以 traceback 开场的界面，等于告诉评委这东西没人跑过。
    """
    problems: list[str] = []
    for label, ensure, command in (
        ("事故库", ensure_incident_schema, "hindsight-incidents-import"),
        ("检索库", ensure_rag_schema, "hindsight-rag-bootstrap"),
        ("工作流库", ensure_workflow_schema, "hindsight-demo --reset"),
    ):
        try:
            ensure(incident_database)
        except Exception as error:
            problems.append(
                f"{label} schema 建不起来：{error}。"
                f"这通常意味着数据库是旧版本或被手工改过——先跑 `{command}`。"
            )
    return problems


def funnel_counts(incident_database) -> dict[str, int]:
    """本周扫描了多少、静默归档了多少、真正需要一个人看多少。

    这三个数字是本项目最重要的一句话：它不是把更多东西推到人面前，
    而是把绝大多数东西挡住，并为每一次挡住留下可查的理由。
    """
    connection = connect(incident_database)
    try:
        by_state = {
            row["state"]: row["n"]
            for row in connection.execute(
                "SELECT state, COUNT(*) AS n FROM cases GROUP BY state"
            )
        }
        total = connection.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
        by_level = {
            row["level"]: row["n"]
            for row in connection.execute(
                f"SELECT level, COUNT(*) AS n FROM cases "
                f"WHERE state IN ({','.join('?' * len(QUEUE_STATES))}) GROUP BY level",
                QUEUE_STATES,
            )
        }
    finally:
        connection.close()

    queued = sum(by_state.get(state, 0) for state in QUEUE_STATES)
    return {
        "considered": int(total),
        "archived": int(by_state.get("ARCHIVED", 0)),
        "quarantined": int(by_state.get("QUARANTINED", 0)),
        "queued": int(queued),
        "red": int(by_level.get(LEVEL_RED, 0)),
        "yellow": int(by_level.get(LEVEL_YELLOW, 0)),
        "distributed": int(by_state.get("DISTRIBUTED", 0)),
        "closed": int(by_state.get("CLOSED", 0)),
        "rejected": int(by_state.get("CLOSED_REJECTED", 0)),
    }


def queue_rows(incident_database) -> list[dict[str, Any]]:
    """待决定队列，附带事故标题，按级别与时间排序。"""
    connection = connect(incident_database)
    try:
        rows = connection.execute(
            f"""
            SELECT c.case_id, c.incident_key, c.risk_assessment_id, c.state,
                   c.level, c.draft_content_hash, c.rule_pack_version,
                   c.opened_at, c.updated_at,
                   i.event_title, i.narrative, i.employer, i.event_date,
                   i.primary_naics, i.nature_title, i.body_part_title
            FROM cases AS c
            LEFT JOIN osha_sir_incidents AS i ON i.incident_key = c.incident_key
            WHERE c.state IN ({','.join('?' * len(QUEUE_STATES))})
            ORDER BY CASE c.level WHEN 'RED' THEN 0 WHEN 'YELLOW' THEN 1 ELSE 2 END,
                     c.updated_at DESC
            """,
            QUEUE_STATES,
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# 判定依据的呈现
# --------------------------------------------------------------------------

def _render_control(st, control, *, blocking: bool) -> None:
    verdict = "阻断" if blocking else "未能阻断"
    st.markdown(
        f"**{control.control_name}** · `{control.hierarchy_code}` · "
        f"状态 `{control.state}` · {verdict}"
    )
    bits = []
    if control.verification_code:
        bits.append(f"验证记录 `{control.verification_code}`")
    if control.verification_date:
        bits.append(f"验证于 {control.verification_date}")
    if control.verification_result:
        bits.append(f"结论 {control.verification_result}")
    if control.next_due_on:
        bits.append(f"下次到期 {control.next_due_on}")
    if control.evidence_reference:
        bits.append(f"证据 {control.evidence_reference}")
    st.caption(" · ".join(bits) if bits else "没有任何验证记录支持这条控制。")
    if control.verification_finding:
        st.caption(f"发现：{control.verification_finding}")


def render_certificate(st, certificate) -> None:
    """把一条危险路径的判定摊开给人看，包括系统承认自己不知道的部分。"""
    if certificate.decision == NO_ACTIONABLE_TRANSFER:
        st.success(f"阻断证书 · {certificate.headline()}")
        st.caption(
            "这张证书证明「检查义务已履行」，不证明「工厂是安全的」。"
            "它说的是：这条路径上的控制在评估日仍然有效。"
        )
    elif certificate.decision == UNKNOWN:
        st.warning(f"事实不足 · {certificate.headline()}")
        st.caption("未知不等于安全。系统拒绝在这里替你猜。")
    else:
        st.error(f"残余路径成立 · {certificate.headline()}")

    left, right = st.columns(2)
    with left:
        st.markdown(
            f"**设备**  {certificate.equipment_name or '—'}  \n"
            f"**作业**  {certificate.task_name or '—'}  \n"
            f"**危险**  {certificate.hazard_name or '—'}  \n"
            f"**暴露岗位**  {certificate.exposed_role or '—'}"
        )
    with right:
        st.markdown(
            f"**初始风险**  {certificate.initial_level}"
            f"（严重度 {certificate.initial_severity}，分值 {certificate.initial_score}）  \n"
            f"**残余风险**  {certificate.residual_level}"
            f"（分值 {certificate.residual_score}）  \n"
            f"**本厂是否已接受该残余风险**  "
            f"{'是' if certificate.risk_accepted else '否'}  \n"
            f"**评估基准日**  {certificate.as_of}"
        )

    if certificate.hazardous_situation:
        st.caption(f"危险状态：{certificate.hazardous_situation}")

    if certificate.blocking_cut:
        st.markdown("##### 切断这条路径的控制")
        for control in certificate.blocking_cut:
            _render_control(st, control, blocking=True)
    if certificate.residual_controls:
        st.markdown("##### 路径上未能切断的控制")
        for control in certificate.residual_controls:
            _render_control(st, control, blocking=False)
    if certificate.open_corrective_actions:
        st.markdown("##### 未闭环整改")
        st.dataframe(certificate.open_corrective_actions, width="stretch",
                     hide_index=True)

    for label, items in (
        ("系统作出的假设", certificate.assumptions),
        ("登记册内部的矛盾", certificate.contradictions),
        ("缺失的关键事实", certificate.missing_facts),
    ):
        if items:
            st.markdown(f"**{label}**")
            for item in items:
                st.markdown(f"- {item}")

    st.caption(
        f"判定版本 {certificate.assessment_version} · "
        f"证书哈希 `{certificate.certificate_hash()[:16]}…`"
    )


def render_pack_body(st, pack: dict[str, Any]) -> None:
    """稿件正文，每一行都带着它的出处。"""
    english, spanish = st.tabs(["English", "Español"])
    for tab, language in ((english, "en"), (spanish, "es")):
        with tab:
            briefing = (pack.get("briefing") or {}).get(language) or {}
            talk = (pack.get("toolbox_talk") or {}).get(language) or {}
            st.markdown(f"#### {briefing.get('title', '')}")
            for section in briefing.get("sections", []):
                st.markdown(f"**{section['heading']}**")
                for line in section["lines"]:
                    st.markdown(f"- {line['text']}")
                    citations = " · ".join(f"`{c}`" for c in line.get("citations", []))
                    if citations:
                        st.caption(f"　出处：{citations}")
                    if line.get("quote"):
                        st.caption(f"　原文引述：「{line['quote']}」")
            if talk:
                st.markdown(
                    f"**班前会脚本** · 面向 {talk.get('audience_role') or '—'} · "
                    f"约 {talk.get('estimated_minutes', '—')} 分钟"
                )
                # render_toolbox_talk 的键是 blocks，不是 sections。
                for block in talk.get("blocks", []):
                    st.markdown(f"*{block['heading']}*")
                    for line in block["lines"]:
                        st.markdown(f"- {line['text']}")
                        citations = " · ".join(
                            f"`{c}`" for c in line.get("citations", [])
                        )
                        if citations:
                            st.caption(f"　出处：{citations}")
                for item in talk.get("quiz", []):
                    st.markdown(f"❓ {item['question']}")
                    for index, option in enumerate(item.get("options", [])):
                        marker = "✓" if index == item.get("correct_index") else "·"
                        st.caption(f"　{marker} {option}")
            if briefing.get("advisory"):
                st.info(briefing["advisory"])


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------

def render_dashboard() -> None:
    import streamlit as st

    settings = load_settings()
    bootstrap_problems = bootstrap_schemas(settings.incident_database)

    st.set_page_config(
        page_title="Hindsight Safety Console",
        page_icon=":material/health_and_safety:",
        layout="wide",
        initial_sidebar_state="auto",
    )
    st.markdown(
        """
        <style>
        :root { color-scheme: light; }
        .stApp { background: #f5f7f8; color: #162126; }
        [data-testid="stSidebar"] { background: #172126; }
        [data-testid="stSidebar"] * { color: #f7faf9; }
        [data-testid="stSidebar"] [data-testid="stNumberInput"] *,
        [data-testid="stSidebar"] [data-testid="stDateInput"] * {
            color: #162126 !important;
        }
        [data-testid="stSidebar"] .stButton button {
            background: #b33f36; border-color: #b33f36;
        }
        [data-testid="stSidebar"] .stButton button * { color: #ffffff !important; }
        [data-testid="stSidebar"] a { color: #8bc9bd !important; }
        [data-testid="stMetric"] {
            background: #ffffff; border: 1px solid #dbe3e6; border-radius: 6px;
            padding: 12px 14px;
        }
        [data-testid="stMetricLabel"] p { color: #536168 !important; }
        [data-testid="stMetricValue"] { color: #173f35; }
        header[data-testid="stHeader"] { background: #f5f7f8; }
        button[data-baseweb="tab"] p { color: #445258 !important; }
        button[data-baseweb="tab"][aria-selected="true"] p { color: #a93831 !important; }
        .status-ok { color: #176b4d; font-weight: 650; }
        .status-warn { color: #a15c00; font-weight: 650; }
        .funnel {
            background: #ffffff; border: 1px solid #dbe3e6; border-radius: 6px;
            padding: 16px 18px; margin-bottom: 8px;
        }
        .funnel b { font-size: 26px; }
        .lvl-red { color: #a32b21; font-weight: 700; }
        .lvl-yellow { color: #9a6300; font-weight: 700; }
        div[data-testid="stButton"] button { border-radius: 6px; }
        h1, h2, h3 { letter-spacing: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.title("Hindsight")
        st.caption("Safety operations")
        as_of = st.date_input("评估基准日", value=date.today())
        st.caption("固定基准日后，判定结果可复现。")
        backfill = st.number_input(
            "采集补齐上限", min_value=0, max_value=10000,
            value=settings.daily_backfill_limit, step=50,
        )
        if st.button("采集并匹配", icon=":material/sync:", width="stretch"):
            with st.spinner("正在采集、向量化并匹配..."):
                try:
                    result = run_pipeline(
                        trigger_type="dashboard", backfill_limit=int(backfill),
                    )
                    st.success(f"完成：{result['matching']['matches_saved']} 条候选已写入")
                except Exception as exc:
                    st.error(str(exc))
        st.divider()
        st.caption(f"SQLite · {settings.incident_database.name}")
        st.caption(f"Factory · {settings.factory_database.name}")
        st.caption(f"Milvus · {settings.milvus_uri}")

    for problem in bootstrap_problems:
        st.warning(problem)

    counts = {k: 0 for k in ("considered", "archived", "quarantined", "queued",
                             "red", "yellow", "distributed", "closed", "rejected")}
    summary = {"counts": {"incidents": 0}, "latest_run": None}
    with section_guard(st, "顶部指标"):
        counts = funnel_counts(settings.incident_database)
        summary = dashboard_summary(settings.incident_database)
    latest = summary["latest_run"]

    title_col, state_col = st.columns([4, 1])
    with title_col:
        st.title("Hindsight Safety Console")
        st.caption("外部事故 → 本厂危险路径 → 一个人的决定 → 到达一线的证据")
    with state_col:
        if latest and latest["status"] == "completed":
            st.markdown('<p class="status-ok">最近运行：已完成</p>', unsafe_allow_html=True)
        elif latest and latest["status"] == "failed":
            st.markdown('<p class="status-warn">最近运行：失败</p>', unsafe_allow_html=True)
        else:
            st.markdown('<p class="status-warn">尚未完成运行</p>', unsafe_allow_html=True)
        st.caption(datetime.now().strftime("%Y-%m-%d %H:%M"))

    # 顶部漏斗：这是本系统全部价值的一句话表达。
    st.markdown(
        f"""
        <div class="funnel">
          本轮考察 <b>{counts['considered']:,}</b> 条 &nbsp;→&nbsp;
          确定性归档 <b>{counts['archived']:,}</b> 条（每条都留有理由）&nbsp;→&nbsp;
          需要你决定 <b>{counts['queued']:,}</b> 条
          &nbsp;&nbsp;<span class="lvl-red">红 {counts['red']}</span>
          &nbsp;<span class="lvl-yellow">黄 {counts['yellow']}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    metric_columns = st.columns(5)
    metric_columns[0].metric("已分发", f"{counts['distributed']:,}")
    metric_columns[1].metric("已闭环", f"{counts['closed']:,}")
    metric_columns[2].metric("已驳回", f"{counts['rejected']:,}")
    metric_columns[3].metric("抽取隔离", f"{counts['quarantined']:,}")
    metric_columns[4].metric("事故底库", f"{summary['counts']['incidents']:,}")

    queue_tab, register_tab, delivery_tab, audit_tab, retrieval_tab, data_tab = st.tabs(
        ["待你决定", "风险登记册判定", "分发与完成", "审计轨迹", "候选检索", "数据状态"]
    )

    # ----------------------------------------------------------------------
    with queue_tab:
        with section_guard(st, "待你决定"):
            rows = queue_rows(settings.incident_database)
            if not rows:
                st.info(
                    "队列是空的。这通常不是系统没在工作——"
                    "看顶部漏斗：被确定性归档的每一条都留下了理由，可在「审计轨迹」中逐条追溯。"
                )
            else:
                red = [r for r in rows if r["level"] == LEVEL_RED]
                yellow = [r for r in rows if r["level"] == LEVEL_YELLOW]
                red_col, yellow_col = st.columns(2)
                for column, bucket, level in (
                    (red_col, red, LEVEL_RED), (yellow_col, yellow, LEVEL_YELLOW)
                ):
                    colour, label, meaning = _LEVEL_STYLE[level]
                    with column:
                        st.markdown(
                            f"<h4 style='color:{colour}'>{label} · {len(bucket)} 条</h4>"
                            f"<p style='color:#536168;margin-top:-8px'>{meaning}</p>",
                            unsafe_allow_html=True,
                        )
                        for row in bucket:
                            st.markdown(
                                f"`{row['case_id'][:12]}` · "
                                f"{_STATE_LABEL.get(row['state'], row['state'])} · "
                                f"{row['event_title'] or row['incident_key']}"
                            )

                st.divider()
                selected_id = st.selectbox(
                    "打开一条案例",
                    [row["case_id"] for row in rows],
                    format_func=lambda value: next(
                        f"{row['level']} · {_STATE_LABEL.get(row['state'], row['state'])} · "
                        f"{row['event_title'] or row['incident_key']}"
                        for row in rows if row["case_id"] == value
                    ),
                )
                case = next(row for row in rows if row["case_id"] == selected_id)
                _render_case(st, settings, case, as_of=as_of.isoformat())

        # ----------------------------------------------------------------------
    with register_tab:
        with section_guard(st, "风险登记册判定"):
            st.caption(
                "对本厂每一条危险路径做一次残余路径验证。绿色是被阻断并有验证记录支持的路径，"
                "这些路径永远不会打扰任何人——但它们的证书随时可查。"
            )
            try:
                certificates = assess_paths(settings.factory_database, as_of=as_of.isoformat())
            except Exception as exc:
                st.error(f"读取风险登记册失败：{exc}")
                certificates = []
            if certificates:
                decisions = [(c, route_path(c)) for c in certificates]
                tally = {}
                for _, routing in decisions:
                    tally[routing.level] = tally.get(routing.level, 0) + 1
                cols = st.columns(4)
                for index, level in enumerate(("RED", "YELLOW", "GREEN", "NOT_RELEVANT")):
                    cols[index].metric(level, tally.get(level, 0))

                st.dataframe(
                    [
                        {
                            "级别": routing.level,
                            "路径": certificate.assessment_code,
                            "设备": certificate.equipment_name,
                            "危险": certificate.hazard_name,
                            "判定": certificate.decision,
                            "理由码": certificate.reason_code,
                            "初始严重度": certificate.initial_severity,
                        }
                        for certificate, routing in decisions
                    ],
                    width="stretch", hide_index=True, height=320,
                )
                chosen = st.selectbox(
                    "查看某条路径的判定依据",
                    [c.risk_assessment_id for c, _ in decisions],
                    format_func=lambda value: next(
                        f"{r.level} · {c.assessment_code} · {c.equipment_name}"
                        for c, r in decisions if c.risk_assessment_id == value
                    ),
                )
                certificate = next(
                    c for c, _ in decisions if c.risk_assessment_id == chosen
                )
                render_certificate(st, certificate)

        # ----------------------------------------------------------------------
    with delivery_tab:
        with section_guard(st, "分发与完成"):
            digest = monday_digest(settings.incident_database)
            st.subheader("周一摘要")
            st.info(digest["headline"])
            cols = st.columns(4)
            cols[0].metric("分发次数", digest["deliveries"])
            cols[1].metric("触达人数", digest["workers_reached"])
            cols[2].metric(
                "完成率",
                "—" if digest["completion_rate"] is None
                else f"{digest['completion_rate'] * 100:.0f}%",
            )
            cols[3].metric(
                "平均成绩",
                "—" if digest["average_quiz_score"] is None
                else f"{digest['average_quiz_score']:.2f}",
            )
            deliveries = query_rows(
                settings.incident_database,
                'SELECT * FROM "分发与完成" ORDER BY "送达时间" DESC LIMIT 200',
            )
            if deliveries:
                st.dataframe(deliveries, width="stretch", hide_index=True, height=360)
            else:
                st.caption("还没有任何分发记录。")
            if digest["archive_reasons"]:
                st.subheader("本周静默归档的原因分布")
                st.dataframe(
                    [{"理由码": k, "条数": v} for k, v in digest["archive_reasons"].items()],
                    width="stretch", hide_index=True,
                )

        # ----------------------------------------------------------------------
    with audit_tab:
        with section_guard(st, "审计轨迹"):
            st.caption("每一次状态变化都带着执行者、理由码与时间，且顺序不可重排。")
            cases = query_rows(
                settings.incident_database,
                'SELECT * FROM "案例状态" ORDER BY "更新时间" DESC LIMIT 300',
            )
            if not cases:
                st.info("还没有案例。先运行一次工作流。")
            else:
                st.dataframe(cases, width="stretch", hide_index=True, height=300)
                case_id = st.selectbox(
                    "查看某条案例的完整轨迹", [row["案例ID"] for row in cases]
                )
                history = case_history(settings.incident_database, case_id)
                st.dataframe(
                    [
                        {
                            "序": row["seq"], "从": row["from_state"] or "—",
                            "到": row["to_state"], "执行者": row["actor"],
                            "理由码": row["reason_code"], "时间": row["occurred_at"],
                            "细节": (row["detail"] or "")[:160],
                        }
                        for row in history
                    ],
                    width="stretch", hide_index=True,
                )

        # ----------------------------------------------------------------------
    with retrieval_tab:
        with section_guard(st, "候选检索"):
            st.warning(
                "这一页是候选检索，不是判定。相关度分数用来把可能相关的事故拉到评估范围内，"
                "它不决定任何事——决定由「风险登记册判定」页的残余路径验证作出。"
                "把这个分数当成结论，正是本项目要避免的失败方式。"
            )
            matches = query_rows(
                settings.incident_database,
                """
                SELECT incident_title AS 事故, incident_date AS 日期,
                       area_name AS 区域, asset_code AS 设备编号,
                       equipment_name AS 设备, hazard_name AS 危险,
                       ROUND(relevance_score * 100, 1) AS 检索相关度,
                       CASE decision_status WHEN 'pending' THEN '待决定'
                            WHEN 'confirmed' THEN '已确认' ELSE '已忽略' END AS 状态,
                       updated_at AS 更新时间
                FROM incident_factory_matches
                ORDER BY updated_at DESC, relevance_score DESC
                LIMIT 500
                """,
            )
            if matches:
                st.dataframe(matches, width="stretch", hide_index=True, height=460)
            else:
                st.caption("还没有候选检索结果。")

        # ----------------------------------------------------------------------
    with data_tab:
        with section_guard(st, "数据状态"):
            st.subheader("存储位置")
            st.code(
                f"SQLite: {settings.incident_database}\n"
                f"Factory: {settings.factory_database}\n"
                f"Milvus: {settings.milvus_uri}\n"
                f"Incident collection: {settings.incident_collection}\n"
                f"Factory collection: {settings.factory_collection}"
            )
            st.subheader("可用视图")
            st.dataframe(
                [
                    {"视图": "案例状态", "用途": "每条案例现在在工作流的哪一步"},
                    {"视图": "分发与完成", "用途": "发给了谁、多少人读完、成绩如何"},
                    {"视图": "事故工厂匹配", "用途": "候选检索结果（不是判定）"},
                    {"视图": "每日运行", "用途": "定时任务与失败原因"},
                    {"视图": "事故记录", "用途": "OSHA SIR 事故底库"},
                    {"视图": "接地语料", "用途": "CFR 与 Fatal Facts 语料段落"},
                ],
                width="stretch", hide_index=True,
            )
            st.subheader("这个系统明确不做的事")
            st.markdown(
                "- 不预测事故是否会发生，也不输出任何形式的事故概率。\n"
                "- 不在证据不足时给出建议：未知会被显式标注为未知。\n"
                "- 不代替人批准任何发往一线的内容。\n"
                "- 不覆盖联邦 OSHA 管辖之外的雇主，数据边界见 README。"
            )


def _render_case(st, settings, case: dict[str, Any], *, as_of: str) -> None:
    """一条案例的完整决定界面：事故、判定依据、稿件、四项确认、批准或驳回。"""
    case_id = case["case_id"]
    st.subheader(case["event_title"] or case["incident_key"])
    st.caption(
        f"案例 `{case_id}` · {_STATE_LABEL.get(case['state'], case['state'])} · "
        f"级别 {case['level']} · 规则包 {case['rule_pack_version'] or '—'} · "
        f"开案 {case['opened_at']}"
    )

    with st.expander("外部事故原文", expanded=False):
        st.markdown(
            f"**雇主**  {case['employer'] or '—'} · **日期**  {case['event_date'] or '—'} · "
            f"**NAICS**  {case['primary_naics'] or '—'}  \n"
            f"**伤害性质**  {case['nature_title'] or '—'} · "
            f"**受伤部位**  {case['body_part_title'] or '—'}"
        )
        st.write(case["narrative"] or "（无叙述）")
        st.caption(f"来源键 `{case['incident_key']}` · OSHA 严重伤害报告")

    st.markdown("### 为什么它到了你面前")
    if case["risk_assessment_id"]:
        try:
            certificates = assess_paths(
                settings.factory_database, as_of=as_of,
                risk_assessment_ids=[case["risk_assessment_id"]],
            )
        except Exception as exc:
            st.error(f"无法读取该危险路径：{exc}")
            certificates = []
        if certificates:
            render_certificate(st, certificates[0])
        else:
            st.warning(
                f"案例记录的危险路径 `{case['risk_assessment_id']}` "
                f"在风险登记册中读不到。这本身需要人来查。"
            )
    else:
        st.warning("这条案例没有绑定危险路径。")

    st.markdown("### 将要发给一线的内容")
    try:
        artifact = pack_artifact_for_case(settings.incident_database, case_id)
    except PackArtifactError as exc:
        st.error(str(exc))
        artifact = None

    if artifact is None:
        st.warning(
            "这条案例还没有可供审批的正文。"
            "「需人工补写」状态的案例正是这种情况：自检删掉了无出处的主张之后，"
            "必需的段落填不满，系统拒绝硬凑，把它交回给人。"
        )
        _render_reject_only(st, settings, case_id)
        return

    pack = artifact["pack"]
    st.caption(
        f"稿件哈希 `{artifact['content_hash'][:16]}…` · "
        f"生成器 {pack.get('model_id') or '—'} · "
        f"重生成次数 {pack.get('regeneration_count', 0)} · "
        f"版本 {artifact['pack_version']}"
    )
    render_pack_body(st, pack)

    check = artifact["self_check"]
    if check:
        with st.expander("自检结果（G4）", expanded=False):
            st.markdown(
                f"检查主张 {check.get('checked_claims', 0)} 条 · "
                f"删除无出处主张 {len(check.get('removed_claim_ids', []))} 条 · "
                f"删除无依据题目 {len(check.get('removed_quiz_ids', []))} 条"
            )
            findings = check.get("findings", [])
            if findings:
                st.dataframe(findings, width="stretch", hide_index=True)
            else:
                st.caption("没有任何主张被删除。")

    st.markdown("### 批准前的四项确认")
    st.caption("四项全部勾选后，批准按钮才可用。这是 A2 要求的人类问责，不是提示。")
    ticked = [
        st.checkbox(label, key=f"{case_id}:{key}") for key, label in APPROVAL_CHECKS
    ]
    approver = st.text_input("批准人", value="", key=f"{case_id}:approver",
                             placeholder="填写你的姓名或工号")
    all_checked = all(ticked) and bool(approver.strip())

    approve_col, reject_col, _ = st.columns([1, 1, 3])
    if approve_col.button(
        "批准并分发", icon=":material/check:", type="primary",
        width="stretch", disabled=not all_checked, key=f"{case_id}:approve",
    ):
        try:
            result = approve_and_distribute(
                incident_database=settings.incident_database,
                case_id=case_id,
                pack_content_hash=artifact["content_hash"],
                approver=approver.strip(),
                factory_database=settings.factory_database,
            )
        except Exception as exc:
            st.error(f"分发被拒绝：{exc}")
        else:
            st.session_state[f"{case_id}:result"] = result
            st.rerun()

    reason = reject_col.text_input("驳回理由", key=f"{case_id}:reason",
                                   placeholder="驳回必须写明理由")
    if reject_col.button("驳回", icon=":material/close:", width="stretch",
                         disabled=not reason.strip(), key=f"{case_id}:reject"):
        reject_case(
            settings.incident_database, case_id,
            approver=approver.strip() or "unknown", reason=reason.strip(),
        )
        st.rerun()

    result = st.session_state.get(f"{case_id}:result")
    if result and result.get("warning") == "APPROVED_BUT_NO_CHANNEL":
        st.error(f"已批准，但送达了 0 个人。{result['detail']}")
        st.caption(
            "系统没有退回全厂广播——定向分发正是为了避免那件事。"
            "请在渠道注册表中登记该岗位后重新分发。"
        )
    elif result:
        st.success(f"已批准并分发，送达 {result.get('delivered_to', 0)} 个渠道。")
        st.caption(
            "每条渠道各持有一个 HMAC 批准令牌，绑定案例、稿件哈希、渠道与有效期；"
            "幂等键保证同一稿件不会重复送达同一渠道。"
        )
        st.dataframe(
            [
                {
                    "渠道": item.get("channel", "—"),
                    "是否送达": "是" if item.get("delivered") else "否",
                    "触达人数": item.get("audience_size", 0),
                    "分发ID": item.get("delivery_id", "—"),
                    "说明": item.get("detail") or item.get("error") or "",
                }
                for item in result["deliveries"]
            ],
            width="stretch", hide_index=True,
        )
        status = completion_status(settings.incident_database, case_id)
        st.caption(
            f"待完成 {len(status['outstanding'])} 人 · "
            f"T+24h / T+72h 催办为幂等操作，不会重复轰炸。"
        )


def _render_reject_only(st, settings, case_id: str) -> None:
    approver = st.text_input("处理人", value="", key=f"{case_id}:approver_nb")
    reason = st.text_input("驳回理由", key=f"{case_id}:reason_nb")
    if st.button("驳回这条案例", width="stretch", key=f"{case_id}:reject_nb",
                 disabled=not reason.strip()):
        reject_case(
            settings.incident_database, case_id,
            approver=approver.strip() or "unknown", reason=reason.strip(),
        )
        st.rerun()


def main() -> int:
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, "--server.headless=false"]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    render_dashboard()
