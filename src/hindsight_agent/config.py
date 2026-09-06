"""Environment-backed configuration shared by the RAG pipeline and UI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
