"""Command-line entry point for deterministic OSHA news collection."""

from __future__ import annotations

import json
import sys

from .tools.collect_osha import CollectionError
from .tools.collect_osha_news import collect_osha_news_impl


def main() -> int:
    try:
        result = collect_osha_news_impl()
    except CollectionError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

