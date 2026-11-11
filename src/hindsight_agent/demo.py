"""One command from a clean checkout to a console you can demo.

Why this exists
---------------
Every stage of this project already has a CLI, and the README lists them in
order. That is fine for someone who built it and wrong for everyone else: a
judge, a teammate, or you three days from now. This runs the whole thing,
checks its own preconditions first, and says plainly which one failed.

It is deliberately idempotent. Databases that already exist are left alone;
``--reset`` clears only the workflow tables - cases, transitions, deliveries -
and never the incident corpus or the grounding corpus, because those are
expensive to rebuild and are not what makes a demo stale.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .config import load_settings
from .rag_agent import (
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    ENV_OPENAI_MODEL_ID,
    ENV_PROVIDER,
)

#: 工作流状态表。重跑演示时只清这些，事故底库与接地语料一概不动。
WORKFLOW_TABLES = (
    "case_transitions", "pack_artifacts", "completions", "deliveries",
    "reminders", "feedback_signals", "quarantine", "cases",
)


class PreflightError(RuntimeError):
    """一个人能直接照着修的失败。"""


def _say(step: str, detail: str = "") -> None:
    print(f"  {step}{(' · ' + detail) if detail else ''}", flush=True)


# --------------------------------------------------------------------------
# 前置检查
# --------------------------------------------------------------------------

def check_model(*, timeout: float = 20.0) -> dict[str, Any]:
    """真的调用一次模型，而不是只看环境变量有没有设。

    只检查配置的写法，等于把失败推迟到跑了两分钟筛选之后才爆——那时人已经
    不知道是哪一步坏了。
    """
    provider = os.getenv(ENV_PROVIDER, "ollama").casefold()
    if provider != "openai":
        return {"provider": provider, "checked": False,
                "note": f"provider={provider}，跳过 OpenAI 兼容端点检查"}

    base_url = os.getenv(ENV_OPENAI_BASE_URL, "").strip()
    model_id = os.getenv(ENV_OPENAI_MODEL_ID, "").strip()
    missing = [name for name, value in (
        (ENV_OPENAI_BASE_URL, base_url), (ENV_OPENAI_MODEL_ID, model_id)
    ) if not value]
    if missing:
        raise PreflightError(
            "缺少环境变量：" + "、".join(missing) + "\n"
            f"  例：export {ENV_PROVIDER}=openai\n"
            f"      export {ENV_OPENAI_BASE_URL}=https://your-host/v1\n"
            f"      export {ENV_OPENAI_MODEL_ID}=zai-org/glm-4.7-flash\n"
            f"      export {ENV_OPENAI_API_KEY}=（无鉴权可留空）"
        )

    from openai import OpenAI

    client = OpenAI(base_url=base_url,
                    api_key=os.getenv(ENV_OPENAI_API_KEY, "") or "not-required",
                    timeout=timeout)
    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16, temperature=0.0,
        )
    except Exception as error:
        raise PreflightError(
            f"连不上模型端点 {base_url}（模型 {model_id}）：{error}\n"
            f"  · 若它只监听内网，请先把它暴露成公网地址再设 {ENV_OPENAI_BASE_URL}。\n"
            f"  · 若它需要密钥，请设 {ENV_OPENAI_API_KEY}。"
        ) from error
    elapsed = time.monotonic() - started
    reply = (response.choices[0].message.content or "").strip()

    # 上下文有多大，和「连不连得上」同等重要：一次起草的提示词是四位数
    # token，而本地端点常常默认只装 4096。把它当作前置检查的一部分报出来，
    # 人就能在跑之前知道这台机器上会不会因为窗口太小而写不出东西。
    from .content import discover_context_window, plan_budget

    context_tokens, context_source = discover_context_window(base_url, model_id)
    budget = plan_budget(context_tokens, source=context_source)
    return {"provider": "openai", "checked": True, "base_url": base_url,
            "model_id": model_id, "latency_s": round(elapsed, 2),
            "reply_sample": reply[:60],
            "context_tokens": budget.context_tokens,
            "context_source": budget.source,
            "context_known": context_tokens is not None,
            "prompt_tokens": budget.prompt_tokens,
            "completion_tokens": budget.completion_tokens}


def check_data(settings) -> dict[str, Any]:
    corpus = settings.project_root / "data" / "corpus" / "grounding.db"
    missing = []
    if not Path(settings.incident_database).exists():
        missing.append("事故底库（hindsight-incidents-import）")
    if not Path(settings.factory_database).exists():
        missing.append("虚拟工厂（hindsight-factory-create-zh）")
    if not corpus.exists():
        missing.append("接地语料（hindsight-grounding-collect）")
    if missing:
        raise PreflightError("缺少数据：" + "；".join(missing))

    connection = sqlite3.connect(settings.incident_database)
    incidents = connection.execute(
        "SELECT COUNT(*) FROM osha_sir_incidents"
    ).fetchone()[0]
    connection.close()
    connection = sqlite3.connect(corpus)
    passages = connection.execute("SELECT COUNT(*) FROM corpus_passages").fetchone()[0]
    connection.close()
    if not incidents:
        raise PreflightError("事故底库是空的，先跑 hindsight-incidents-import。")
    if not passages:
        raise PreflightError("接地语料是空的，先跑 hindsight-grounding-collect。")
    return {"incidents": incidents, "corpus_passages": passages,
            "corpus_path": str(corpus)}


def drafting_quality(database_path: Path | str) -> dict[str, Any]:
    """起草出来的东西有多少经得起 G4 核查。

    这是换模型之后第一个该看的数字。自检把没有出处的主张整句删掉，所以一个
    弱模型的典型表现不是"写错了"，而是"写完之后基本被删光"。
    「检查 12 条、删掉 9 条」这一行，比任何主观评价都更快地告诉你这个模型
    够不够用。
    """
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT self_check_json FROM pack_artifacts"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"packs": 0}
    finally:
        connection.close()

    checked = removed = removed_quiz = 0
    reasons: dict[str, int] = {}
    for row in rows:
        report = json.loads(row["self_check_json"] or "{}")
        checked += int(report.get("checked_claims") or 0)
        removed += len(report.get("removed_claim_ids") or [])
        removed_quiz += len(report.get("removed_quiz_ids") or [])
        for finding in report.get("findings") or []:
            code = finding.get("check", "?")
            reasons[code] = reasons.get(code, 0) + 1
    return {
        "packs": len(rows), "checked_claims": checked,
        "removed_claims": removed, "removed_quiz": removed_quiz,
        "survival_rate": round((checked - removed) / checked, 3) if checked else None,
        "top_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5]),
    }


def reset_workflow(database_path: Path | str) -> dict[str, int]:
    """只清工作流状态。事故与语料是资产，不是演示残留。"""
    from .workflow_db import ensure_workflow_schema

    ensure_workflow_schema(database_path)
    cleared: dict[str, int] = {}
    connection = sqlite3.connect(str(database_path))
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        with connection:
            for table in WORKFLOW_TABLES:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - 表名来自本模块常量
                ).fetchone()
                cleared[table] = int(row[0])
                connection.execute(f"DELETE FROM {table}")  # noqa: S608
    finally:
        connection.close()
    return {k: v for k, v in cleared.items() if v}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run_demo(
    *,
    limit: int = 500,
    as_of: str = "2026-09-06",
    draft_limit: int = 3,
    reset: bool = False,
    skip_model: bool = False,
) -> dict[str, Any]:
    from .distribution import register_channels
    from .workflow import run_full_workflow

    settings = load_settings()
    corpus = settings.project_root / "data" / "corpus" / "grounding.db"

    print("\n前置检查")
    # 先报「我在用哪一份代码和哪一个库」。
    # 这个项目在审计里栽过一次：一个装在别处的可编辑安装悄悄接管了导入，
    # 于是测试跑的是一份冻结副本，而所有人都以为在测工作树。同一类混淆
    # 也会发生在数据上——从 clone 目录直接跑，很可能读到另一份数据库，
    # 而输出看上去完全正常。把它打出来，这类问题就只能存活几秒。
    _say("代码", str(Path(__file__).resolve().parent))
    _say("项目根", str(settings.project_root))
    _say("事故库", str(settings.incident_database))
    data_info = check_data(settings)
    _say("数据就绪",
         f"事故 {data_info['incidents']:,} 条 · 语料 {data_info['corpus_passages']:,} 段")

    generator = None
    if skip_model:
        _say("模型", "已跳过（--skip-model）：只跑筛选与路由，不起草")
    else:
        model_info = check_model()
        if model_info["checked"]:
            _say("模型可达",
                 f"{model_info['model_id']} @ {model_info['base_url']} "
                 f"（{model_info['latency_s']}s）")
            _say("上下文",
                 f"{model_info['context_tokens']} token"
                 f"（{model_info['context_source']}）· "
                 f"提示词 {model_info['prompt_tokens']} / "
                 f"回答 {model_info['completion_tokens']}")
            if model_info["context_tokens"] < 8192:
                _say("提示", "窗口偏小，本项目会自动少放语料以装得下。"
                             "用 ≥8192 的上下文重新装载模型，稿件会明显更实。")
        else:
            _say("模型", model_info["note"])
        from .content import build_generator

        generator = build_generator()

    if reset:
        cleared = reset_workflow(settings.incident_database)
        _say("已清空工作流状态",
             "、".join(f"{k} {v}" for k, v in cleared.items()) or "本来就是空的")

    register_channels(settings.incident_database)

    print("\n运行工作流 W1→D5")
    result = run_full_workflow(
        incident_database=settings.incident_database,
        factory_database=settings.factory_database,
        corpus_database=corpus,
        limit=limit, as_of=as_of, generator=generator,
        draft_limit=draft_limit, run_id="demo",
    )

    counts = result["screening"]
    print("\n结果")
    _say("考察", f"{counts['considered']} 条外部事故")
    processed = counts["archived"] + counts["classified"] + counts["quarantined"]
    if counts["considered"] and not processed:
        _say("说明", "这一批全部在之前的运行里处理过了（W3 去重），"
                     "所以本轮没有新动作。要重来一次请加 --reset。")
    _say("确定性归档", f"{counts['archived']} 条（每条都有理由码）")
    _say("抽取隔离", f"{counts['quarantined']} 条")
    _say("进入人工队列", f"{counts['classified']} 条　分级 {result['levels']}")
    _say("已起草待审批", f"{result['drafts']['drafted']} 条")
    if result["drafts"]["needs_human_writer"]:
        _say("需人工补写", f"{result['drafts']['needs_human_writer']} 条"
                          "（自检删完无出处主张后填不满必需段落）")
    if result["drafts"].get("failed"):
        # 起草失败不静默：它和「写得不好」是两回事，通常意味着端点出了问题。
        _say("起草失败", f"{result['drafts']['failed']} 条（已落到需人工补写）")
        for failure in result["drafts"].get("failures", [])[:3]:
            _say("  ↳", f"{failure['case_id']} · {failure['error'][:160]}")

    if generator is not None:
        quality = drafting_quality(settings.incident_database)
        if quality.get("checked_claims"):
            rate = quality["survival_rate"]
            _say("G4 自检",
                 f"检查 {quality['checked_claims']} 条主张，"
                 f"删除 {quality['removed_claims']} 条，"
                 f"存活率 {rate:.0%}" if rate is not None else "")
            if quality["top_reasons"]:
                _say("删除原因",
                     "、".join(f"{k} {v}" for k, v in quality["top_reasons"].items()))
            if rate is not None and rate < 0.5:
                _say("提示", "存活率偏低通常意味着模型在编造引用。"
                             "换一个更强的模型，或检查语料是否覆盖了这条危险路径。")
    print(f"\n  {result['digest']['headline']}")
    print("\n下一步：hindsight-dashboard   打开控制台，在「待你决定」里审批")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一条命令跑通 Hindsight 全流程，并留在可演示状态。"
    )
    parser.add_argument("--limit", type=int, default=500, help="本轮考察的事故条数")
    parser.add_argument("--as-of", default="2026-09-06",
                        help="控制有效期基准日；固定它，结果可复现")
    parser.add_argument("--draft-limit", type=int, default=3,
                        help="最多为几条路由结果起草（每条都要调模型）")
    parser.add_argument("--reset", action="store_true",
                        help="先清空工作流状态再跑（不动事故底库与语料）")
    parser.add_argument("--skip-model", action="store_true",
                        help="不调用模型，只跑筛选与路由")
    parser.add_argument("--out", default=None, help="把完整结果写成 JSON")
    args = parser.parse_args()

    try:
        result = run_demo(
            limit=args.limit, as_of=args.as_of, draft_limit=args.draft_limit,
            reset=args.reset, skip_model=args.skip_model,
        )
    except PreflightError as error:
        # 先把已经打出来的进度冲出去，否则错误（stderr）会插到进度（stdout）前面，
        # 人看到的顺序就是反的。
        sys.stdout.flush()
        print(f"\n前置检查未通过：\n  {error}\n", file=sys.stderr)
        return 2

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  完整结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
