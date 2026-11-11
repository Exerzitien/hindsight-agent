"""Command-line entry point for deterministic OSHA news collection."""

from __future__ import annotations

import argparse
import json
import sys

from .tools.collect_osha import CollectionError
from .tools.collect_osha_news import collect_osha_news_impl


def main() -> int:
    # 同 collector_cli：没有 argparse 的 CLI，--help 会变成一次真实的网络采集。
    argparse.ArgumentParser(
        description="确定性采集 OSHA 官方新闻 RSS，并抽取正文写入快照。",
    ).parse_args()
    try:
        result = collect_osha_news_impl()
    except CollectionError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
