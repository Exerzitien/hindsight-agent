# 队友接入说明

解压交付包后运行 `scripts\setup_local.ps1`。包内已包含当前 SQLite 数据库和原始证据快照；
Milvus 不直接打包二进制数据，而是由脚本在队友机器上从 SQLite 确定性重建，避免路径和
Milvus 版本不一致。

## 最简单的接法

队友不需要理解 Milvus 就能继续下一步。直接读取：

```text
data/incidents/incidents.db
视图：待人工决定
```

```python
import sqlite3

connection = sqlite3.connect("data/incidents/incidents.db")
connection.row_factory = sqlite3.Row
rows = connection.execute(
    'SELECT * FROM "待人工决定" ORDER BY "相关度分数" DESC'
).fetchall()
for row in rows:
    print(dict(row))
```

队友完成后续分析或培训生成时，应保存 `匹配ID`，它是和 Hindsight 对接的稳定主键。

## 英文底表

需要程序化字段时读取 `incident_factory_matches`：

| 字段 | 含义 |
|---|---|
| `match_id` | 稳定匹配主键 |
| `incident_document_id` | Milvus 事故向量主键 |
| `incident_record_id` | SQLite 原始事故/公告主键 |
| `factory_risk_id` | Milvus 工厂风险向量主键 |
| `equipment_id` / `asset_code` | 本厂设备 |
| `hazard_id` | 稳定危险分类 ID |
| `relevance_score` | 0-1 检索相关度 |
| `rationale_json` | 权重、分项和证据词 |
| `decision_status` | `pending` / `confirmed` / `dismissed` |

只处理人工已确认记录：

```sql
SELECT * FROM incident_factory_matches
WHERE decision_status = 'confirmed'
ORDER BY relevance_score DESC;
```

## 直接查询 Milvus

浏览器可打开 `http://localhost:8001` 使用 Attu 查看 Collection、Schema、索引和实体。
该页面已固定连接 Hindsight Milvus；不要使用旧库的 `http://localhost:8000`。

```python
from pymilvus import MilvusClient

client = MilvusClient(uri="http://localhost:19531")
print(client.get_collection_stats("hindsight_v1_incidents"))
print(client.get_collection_stats("hindsight_v1_factory_risks"))
```

Collection 主键与 SQLite `rag_sync_state.document_id` 相同。Milvus 是可重建的检索层，
业务状态和人工决定必须写回 SQLite。

## 记录人工决定

```python
from pathlib import Path
from hindsight_agent.rag_db import record_decision

record_decision(
    Path("data/incidents/incidents.db"),
    match_id="匹配ID",
    decision="confirmed",
    decided_by="teammate-name",
    note="进入培训内容生成",
)
```

## 重新生成向量

修改了文档结构、Embedding 模型或维度时，更换 Collection 前缀：

```powershell
$env:MILVUS_COLLECTION_PREFIX = "hindsight_v2"
hindsight-rag-bootstrap --backfill-limit 0
```
