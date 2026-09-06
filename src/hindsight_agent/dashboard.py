"""Streamlit operations console for Hindsight Safety Agent."""

from __future__ import annotations

import json
import sys
from datetime import datetime

from hindsight_agent.config import load_settings
from hindsight_agent.daily_pipeline import run_pipeline
from hindsight_agent.rag_db import dashboard_summary, query_rows, record_decision


def _decode_json(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def render_dashboard() -> None:
    import streamlit as st

    settings = load_settings()
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
        [data-testid="stSidebar"] [data-testid="stNumberInput"] * {
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
        button[data-baseweb="tab"][aria-selected="true"] p {
            color: #a93831 !important;
        }
        .status-ok { color: #176b4d; font-weight: 650; }
        .status-warn { color: #a15c00; font-weight: 650; }
        .score-line { height: 8px; background: #dfe6e8; border-radius: 4px; overflow: hidden; }
        .score-fill { height: 100%; background: #c1483d; }
        div[data-testid="stButton"] button { border-radius: 6px; }
        h1, h2, h3 { letter-spacing: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.title("Hindsight")
        st.caption("Safety operations")
        backfill = st.number_input(
            "本次补齐上限",
            min_value=0,
            max_value=10000,
            value=settings.daily_backfill_limit,
            step=50,
        )
        if st.button("立即检查", icon=":material/sync:", width="stretch"):
            with st.spinner("正在采集、向量化并匹配..."):
                try:
                    result = run_pipeline(
                        trigger_type="dashboard",
                        backfill_limit=int(backfill),
                    )
                    st.success(
                        f"完成：{result['matching']['matches_saved']} 条匹配已写入"
                    )
                except Exception as exc:
                    st.error(str(exc))
        st.divider()
        st.caption(f"Milvus · {settings.milvus_uri}")
        st.caption(f"Embedding · {settings.embedding_model}")
        st.caption(f"SQLite · {settings.incident_database.name}")

    summary = dashboard_summary(settings.incident_database)
    counts = summary["counts"]
    latest = summary["latest_run"]

    title_col, state_col = st.columns([4, 1])
    with title_col:
        st.title("Hindsight Safety Console")
        st.caption("OSHA 事故资料与本厂风险登记册")
    with state_col:
        if latest and latest["status"] == "completed":
            st.markdown('<p class="status-ok">最近运行：已完成</p>', unsafe_allow_html=True)
        elif latest and latest["status"] == "failed":
            st.markdown('<p class="status-warn">最近运行：失败</p>', unsafe_allow_html=True)
        else:
            st.markdown('<p class="status-warn">尚未完成运行</p>', unsafe_allow_html=True)
        st.caption(datetime.now().strftime("%Y-%m-%d %H:%M"))

    metric_columns = st.columns(5)
    metric_columns[0].metric("事故记录", f"{counts['incidents']:,}")
    metric_columns[1].metric("官方公告", f"{counts['news']:,}")
    metric_columns[2].metric("事故向量", f"{counts['incident_vectors']:,}")
    metric_columns[3].metric("工厂风险向量", f"{counts['factory_vectors']:,}")
    metric_columns[4].metric("待决定", f"{counts['pending']:,}")

    pending_tab, matches_tab, runs_tab, data_tab = st.tabs(
        ["待决定", "匹配结果", "每日运行", "数据状态"]
    )

    with pending_tab:
        pending = query_rows(
            settings.incident_database,
            """
            SELECT match_id, incident_title, incident_date, incident_url,
                   factory_name, area_name, asset_code, equipment_name,
                   hazard_name, assessment_code, relevance_score,
                   semantic_score, hazard_score, industry_score,
                   consequence_score, rationale_json, updated_at
            FROM incident_factory_matches
            WHERE decision_status='pending'
            ORDER BY relevance_score DESC, updated_at DESC
            LIMIT 200
            """,
        )
        if not pending:
            st.info("当前没有需要人工决定的匹配。")
        else:
            selected_id = st.selectbox(
                "选择匹配",
                [row["match_id"] for row in pending],
                format_func=lambda value: next(
                    f"{row['relevance_score'] * 100:.1f} · {row['equipment_name']} · "
                    f"{row['incident_title']}"
                    for row in pending
                    if row["match_id"] == value
                ),
            )
            selected = next(row for row in pending if row["match_id"] == selected_id)
            left, right = st.columns([3, 2])
            with left:
                st.subheader(selected["incident_title"])
                st.write(
                    f"{selected['incident_date'] or '日期未知'} · "
                    f"{selected['factory_name']} / {selected['area_name']}"
                )
                if selected["incident_url"]:
                    st.link_button(
                        "查看 OSHA 原文",
                        selected["incident_url"],
                        icon=":material/open_in_new:",
                    )
                st.markdown(
                    f"**对应设备**  {selected['asset_code']} · {selected['equipment_name']}"
                )
                st.markdown(
                    f"**对应危险**  {selected['hazard_name']} · {selected['assessment_code']}"
                )
            with right:
                percentage = selected["relevance_score"] * 100
                st.metric("相关度分数", f"{percentage:.1f} / 100")
                st.markdown(
                    f'<div class="score-line"><div class="score-fill" '
                    f'style="width:{min(100, percentage):.1f}%"></div></div>',
                    unsafe_allow_html=True,
                )
                st.caption("检索相关度，不是事故概率")
                score_rows = [
                    {"证据": "语义", "分数": round(selected["semantic_score"] * 100, 1)},
                    {"证据": "危险词", "分数": round(selected["hazard_score"] * 100, 1)},
                    {"证据": "行业", "分数": round(selected["industry_score"] * 100, 1)},
                    {"证据": "后果", "分数": round(selected["consequence_score"] * 100, 1)},
                ]
                st.dataframe(score_rows, width="stretch", hide_index=True)

            reviewer = st.text_input("决定人", value="demo-reviewer")
            note = st.text_input("备注", placeholder="可选")
            confirm_col, dismiss_col, _ = st.columns([1, 1, 3])
            if confirm_col.button(
                "确认相关",
                icon=":material/check:",
                type="primary",
                width="stretch",
            ):
                record_decision(
                    settings.incident_database,
                    selected_id,
                    "confirmed",
                    reviewer,
                    note,
                )
                st.rerun()
            if dismiss_col.button(
                "忽略",
                icon=":material/close:",
                width="stretch",
            ):
                record_decision(
                    settings.incident_database,
                    selected_id,
                    "dismissed",
                    reviewer,
                    note,
                )
                st.rerun()

    with matches_tab:
        matches = query_rows(
            settings.incident_database,
            """
            SELECT incident_title AS 事故, incident_date AS 日期,
                   area_name AS 区域, asset_code AS 设备编号,
                   equipment_name AS 设备, hazard_name AS 危险,
                   ROUND(relevance_score * 100, 1) AS 相关度,
                   CASE decision_status WHEN 'pending' THEN '待决定'
                        WHEN 'confirmed' THEN '已确认' ELSE '已忽略' END AS 状态,
                   updated_at AS 更新时间
            FROM incident_factory_matches
            ORDER BY updated_at DESC, relevance_score DESC
            LIMIT 500
            """,
        )
        st.dataframe(matches, width="stretch", hide_index=True, height=520)

    with runs_tab:
        runs = query_rows(
            settings.incident_database,
            """
            SELECT run_id, trigger_type, started_at, finished_at, status,
                   collection_result_json, import_result_json,
                   sync_result_json, match_result_json, error
            FROM rag_runs ORDER BY started_at DESC LIMIT 100
            """,
        )
        display_runs = []
        for row in runs:
            sync = _decode_json(row["sync_result_json"])
            match = _decode_json(row["match_result_json"])
            display_runs.append(
                {
                    "运行ID": row["run_id"][:8],
                    "触发": row["trigger_type"],
                    "开始": row["started_at"],
                    "状态": row["status"],
                    "事故向量": sync.get("incident_vectors_upserted", 0),
                    "保存匹配": match.get("matches_saved", 0),
                    "错误": row["error"] or "",
                }
            )
        st.dataframe(display_runs, width="stretch", hide_index=True, height=500)

    with data_tab:
        st.subheader("存储位置")
        st.code(
            f"SQLite: {settings.incident_database}\n"
            f"Factory: {settings.factory_database}\n"
            f"Milvus: {settings.milvus_uri}\n"
            f"Incident collection: {settings.incident_collection}\n"
            f"Factory collection: {settings.factory_collection}"
        )
        st.subheader("队友可用视图")
        st.dataframe(
            [
                {"视图": "事故工厂匹配", "用途": "全部匹配与人工状态"},
                {"视图": "待人工决定", "用途": "只取需要决定的匹配"},
                {"视图": "每日运行", "用途": "查看定时任务与失败原因"},
                {"视图": "事故记录", "用途": "OSHA SIR 事故底库"},
                {"视图": "官方公告", "用途": "OSHA 官方新闻公告"},
            ],
            width="stretch",
            hide_index=True,
        )


def main() -> int:
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, "--server.headless=false"]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    render_dashboard()
