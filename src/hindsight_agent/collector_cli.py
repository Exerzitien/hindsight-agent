"""Command-line entry point for deterministic OSHA collection."""

from __future__ import annotations

import argparse
import json
import sys

from .tools.collect_osha import CollectionError, collect_osha_sir_impl


def main() -> int:
    # 有 argparse 才有 --help。在这之前 `hindsight-collect --help` 会忽略参数、
    # 直接发起一次真实采集，把一屏 403 当作帮助信息打给你看。
    argparse.ArgumentParser(
        description="确定性采集 OSHA 严重伤害报告（SIR）批量数据集。"
                    "下载到 data/raw/ 并写入带 SHA-256 的不可变快照。",
    ).parse_args()
    try:
        result = collect_osha_sir_impl()
    except CollectionError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
