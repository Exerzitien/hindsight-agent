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
    return {"provider": "openai", "checked": True, "base_url": base_url,
            "model_id": model_id, "latency_s": round(elapsed, 2),
            "reply_sample": reply[:60]}


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
