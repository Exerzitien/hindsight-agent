"""Environment-backed configuration shared by the RAG pipeline and UI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


#: 显式指定项目根。装成 wheel 之后这是最直接的一条路。
ENV_HOME = "HINDSIGHT_HOME"


def resolve_project_root() -> Path:
    """项目根——`data/`、语料、`.env` 都相对它定位。

    从源码检出运行时，它就是仓库根：``<repo>/src/hindsight_agent/config.py``
    往上两级。但这条推导在装成 wheel 之后会安静地错掉：同样往上两级会指到
    ``<venv>/lib/python3.11``，一个既没有 ``data/`` 也永远不会有的地方，
    而所有报错都会理直气壮地告诉你「那里缺文件」，并建议你往那里灌数据。

    所以先认布局：认得出源码检出就用仓库根，认不出（说明是装好的包）就用
    当前工作目录——`cd 到你的工作目录再跑` 对一个装好的命令行工具是符合
    直觉的。``HINDSIGHT_HOME`` 永远优先于这两者。
    """
    override = os.getenv(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    if here.parents[1].name == "src":
        return here.parents[2]
    return Path.cwd()


PROJECT_ROOT = resolve_project_root()


def _path_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    incident_database: Path
    factory_database: Path
    milvus_uri: str
    milvus_token: str
    milvus_prefix: str
    ollama_host: str
    embedding_model: str
    embedding_dimension: int
    embedding_batch_size: int
    top_k: int
    relevance_threshold: float
    daily_backfill_limit: int

    @property
    def corpus_database(self) -> Path:
        """接地语料库。四个模块原本各自拼一遍这条路径。"""
        return self.project_root / "data" / "corpus" / "grounding.db"

    @property
    def incident_collection(self) -> str:
        return f"{self.milvus_prefix}_incidents"

    @property
    def factory_collection(self) -> str:
        return f"{self.milvus_prefix}_factory_risks"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    default_factory = PROJECT_ROOT / "data" / "factory" / "factory_zh.db"
    if not default_factory.exists():
        default_factory = PROJECT_ROOT / "data" / "factory" / "factory.db"
    return Settings(
        project_root=PROJECT_ROOT,
        incident_database=_path_env(
            "HINDSIGHT_INCIDENT_DB",
            PROJECT_ROOT / "data" / "incidents" / "incidents.db",
        ),
        factory_database=_path_env("HINDSIGHT_FACTORY_DB", default_factory),
        milvus_uri=os.getenv("MILVUS_URI", "http://localhost:19531"),
        milvus_token=os.getenv("MILVUS_TOKEN", ""),
        milvus_prefix=os.getenv("MILVUS_COLLECTION_PREFIX", "hindsight_v1"),
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
        embedding_model=os.getenv(
            "OLLAMA_EMBED_MODEL",
            "nomic-embed-text-v2-moe",
        ),
        embedding_dimension=int(os.getenv("HINDSIGHT_EMBED_DIM", "768")),
        embedding_batch_size=int(os.getenv("HINDSIGHT_EMBED_BATCH", "32")),
        top_k=int(os.getenv("HINDSIGHT_TOP_K", "8")),
        relevance_threshold=float(os.getenv("HINDSIGHT_RELEVANCE_THRESHOLD", "0.40")),
        daily_backfill_limit=int(os.getenv("HINDSIGHT_DAILY_BACKFILL_LIMIT", "200")),
    )
