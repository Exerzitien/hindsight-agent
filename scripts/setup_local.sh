#!/usr/bin/env bash
#
# macOS / Linux 一键准备。setup_local.ps1 的对应物。
#
# 与 PowerShell 版的区别，是它默认只准备「一条命令跑通」真正需要的东西：
# Python 环境、虚拟工厂、事故底库、接地语料。Milvus / Ollama / Jaeger 属于
# 向量检索那一路，hindsight-demo 不用它们，所以放在 --with-vectors 后面，
# 而不是让每个人在装 Docker 上先卡半小时。
#
# 每一步都是幂等的：已经建好的库不会被重建，已经采过的语料不会重采。
#
#   ./scripts/setup_local.sh                  # 只准备演示路径需要的
#   ./scripts/setup_local.sh --with-vectors   # 连 Milvus / Ollama 一起
#   ./scripts/setup_local.sh --skip-data      # 已经有数据，只装环境
#
set -euo pipefail

WITH_VECTORS=0
SKIP_DATA=0
SKIP_CORPUS=0
for arg in "$@"; do
    case "$arg" in
        --with-vectors) WITH_VECTORS=1 ;;
        --skip-data)    SKIP_DATA=1 ;;
        --skip-corpus)  SKIP_CORPUS=1 ;;
        -h|--help)
            # 顶部注释块就是这份帮助，从第 3 行读到第一行非注释为止。
            awk 'NR<3 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
            exit 0 ;;
        # ${arg} 要带花括号：紧跟其后的全角括号会被当成变量名的一部分，
        # 在 set -u 下直接变成 "unbound variable"。
        *) echo "未知参数：${arg}（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VENV="$PROJECT_ROOT/.venv"
PY="$VENV/bin/python"

say() { printf '\n=== %s\n' "$1"; }

# --------------------------------------------------------------------------
say "Python 环境"
# --------------------------------------------------------------------------
if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip setuptools
"$PY" -m pip install --quiet -e ".[dev]"
echo "已安装：$("$PY" -c 'import hindsight_agent, sys; print(hindsight_agent.__file__)')"

# --------------------------------------------------------------------------
say "虚拟工厂"
# --------------------------------------------------------------------------
if [ -f data/factory/factory_zh.db ]; then
    echo "已存在 data/factory/factory_zh.db，跳过（要重建加 --replace 自行运行）"
else
    "$VENV/bin/hindsight-factory-create-zh"
fi

# --------------------------------------------------------------------------
if [ "$SKIP_DATA" -eq 0 ]; then
    say "OSHA 事故底库"
    if [ -f data/incidents/incidents.db ] && \
       [ "$("$PY" - <<'EOF'
import sqlite3, sys
try:
    con = sqlite3.connect("data/incidents/incidents.db")
    print(con.execute("SELECT COUNT(*) FROM osha_sir_incidents").fetchone()[0])
except Exception:
    print(0)
EOF
)" != "0" ]; then
        echo "事故底库已有数据，跳过采集与导入"
    else
        echo "采集 OSHA SIR（批量数据集，几百 MB，第一次会比较久）"
        "$VENV/bin/hindsight-collect"
        "$VENV/bin/hindsight-incidents-import"
    fi
fi

# --------------------------------------------------------------------------
if [ "$SKIP_CORPUS" -eq 0 ]; then
    say "接地语料（29 CFR + Fatal Facts）"
    if [ -f data/corpus/grounding.db ] && \
       [ "$("$PY" - <<'EOF'
import sqlite3
try:
    con = sqlite3.connect("data/corpus/grounding.db")
    print(con.execute("SELECT COUNT(*) FROM corpus_passages").fetchone()[0])
except Exception:
    print(0)
EOF
)" != "0" ]; then
        echo "语料已就绪，跳过"
    else
        echo "约 3 分钟。不跑这一步，内容生成会因为没有可引用来源而拒绝出稿。"
        "$VENV/bin/hindsight-grounding-collect"
    fi
fi

# --------------------------------------------------------------------------
if [ "$WITH_VECTORS" -eq 1 ]; then
    say "向量检索一路（Milvus + Ollama）"
    docker compose up -d
    docker compose ps
    ollama pull nomic-embed-text-v2-moe
    ollama pull qwen3:4b-instruct-2507-q4_K_M
    "$PY" -m hindsight_agent.daily_pipeline --backfill-limit 200
fi

# --------------------------------------------------------------------------
say "准备好了"
# --------------------------------------------------------------------------
cat <<'EOF'
下一步：

  1. 配置模型（任何 OpenAI 兼容端点：LM Studio、vLLM、SGLang、Ollama、自建网关）

       cp .env.example .env
       # 然后在 .env 里填：
       #   HINDSIGHT_MODEL_PROVIDER=openai
       #   OPENAI_BASE_URL=http://127.0.0.1:1234/v1
       #   OPENAI_MODEL_ID=<你的模型标识>

  2. 一条命令跑通全流程

       .venv/bin/hindsight-demo --reset

  3. 打开控制台审批

       .venv/bin/hindsight-dashboard

不想接模型也可以先看确定性部分：hindsight-demo --reset --skip-model
EOF
