# Hindsight 架构（唯一权威版本）

> **这份文件是准的。** 仓库和团队材料里还有另外三张图，各自的用途见下方
> 「其他几张图分别是什么」一节。有冲突时以本文件为准。
>
> 最后核对：2026-09-06 · 66 项测试全部通过

## 分层

```mermaid
flowchart TB
  subgraph S1["① 采集与入库 · 确定性，无模型"]
    A0["定时唤醒<br/>Task Scheduler / EventBridge"]
    A1["collect_osha / collect_osha_news<br/>不可变快照 + SHA-256"]
    A2["incident_db 增量导入<br/>content_hash 去重"]
  end

  subgraph S2["② 候选检索 · 高召回，不做决策"]
    B1["rag_documents<br/>关系记录 → 可向量化文档"]
    B2["embeddings + milvus_store<br/>768 维，两个 Collection"]
    B3["matching hybrid-v1.0<br/>语义 70 / 危险词 15 / NAICS 10 / 后果 5"]
  end

  subgraph S3["③ 相关性判定 · 确定性，本节是差异化核心"]
    C1["transfer 残余路径验证<br/>NetworkX 式图遍历，不让模型判断控制是否有效"]
    C2{"V1 四级路由"}
  end

  subgraph S4["④ 内容生成 · 证据约束"]
    D1["grounding<br/>1,393 条可引用段落，硬过滤 + BM25"]
    D2["content 结构化主张<br/>模型只产出带出处的原子主张"]
    D3["self_check 十项确定性核查<br/>核查不过整句删除"]
  end

  subgraph S5["⑤ 人工决策与留痕"]
    E1["dashboard 待决定队列"]
    E2["rag_db 审计：匹配 · 决定 · 运行"]
  end

  A0 --> A1 --> A2 --> B1 --> B2 --> B3 --> C1 --> C2
  C2 -->|"绿 / 无关"| F1["静默归档 + 周报一行"]
  C2 -->|"红 / 黄"| D1
  D1 --> D2 --> D3 --> E1 --> E2

  classDef det fill:#E0EAF1,stroke:#25688F,color:#15171A
  classDef llm fill:#F5E9DA,stroke:#A75C0E,color:#15171A
  classDef core fill:#FBEFC4,stroke:#8A6403,color:#15171A
  classDef stop fill:#E2EDE6,stroke:#2C6B4C,color:#15171A
  class A0,A1,A2,B1,B2,D1,D3,E2 det
  class B3,D2 llm
  class C1,C2 core
  class F1,E1 stop
```

蓝＝确定性代码　橙＝用到模型　黄＝差异化核心　绿＝终点。
**全链路只有两处调用模型**：`matching` 的向量生成，和 `content` 的主张起草。
所有判定都是确定性代码。

## 判定契约

### 残余路径验证（`transfer.py`）

一条危险路径是 `risk_assessments` 的一行：设备 × 任务 × 危险 × 暴露岗位。
判定按固定优先级，第一条命中即生效：

| 序 | 条件 | 结论 |
|---|---|---|
| 1 | `risk_accepted=0`、`status=action_required`，或存在未闭环整改 | ACTIONABLE |
| 2 | 该路径未登记任何控制 | UNKNOWN |
| 3 | 至少一条控制在截止日仍 VERIFIED | NO_ACTIONABLE_TRANSFER |
| 4 | 至少一条控制从未验证过 | UNKNOWN |
| 5 | 其余（全部过期 / 有条件 / 未实施） | ACTIONABLE |

规则 1 压过规则 3 是刻意的：一条工厂已声明未接受的风险，不能被一个验证合格的
防护罩挡回去。**UNKNOWN 永远不等于 False。**

### V1 四级路由（`transfer.route_path`）

| 判定 | 严重度 | 级别 | 去向 |
|---|---|---|---|
| ACTIONABLE | ≥ 4 | 红 | 逐条审批 |
| ACTIONABLE | < 4 | 黄 | 批量确认 |
| UNKNOWN | 任何 | 黄 | 请人过目 |
| NO_ACTIONABLE_TRANSFER | 任何 | 绿 | 静默归档 + 周报一行 |
| 无候选路径 | — | 无关 | 静默归档 |

**UNKNOWN 永不进红。** 高严重且无法判定，正是系统不能装作确定的场合，所以
「高严重 × 低置信强制降黄」写成硬规则，而不是一个需要人去解释的置信分数。

当前虚拟工厂 97 条路径，截止日 2026-09-06 的实际分布：**红 14 · 黄 1 · 绿 82**。

### 内容生成（`learning_pack` / `content` / `self_check`）

模型不写段落，只产出带出处的原子主张。英西双语是同一份主张的两种渲染。
G4 十项核查全部是查表或字符串比对，不含模型判断；核查不过的整句删除，
最多重生成一次，仍填不满必需章节则置 `NEEDS_HUMAN_WRITER`。

细节见 [G1_G4_CONTENT_ZH.md](G1_G4_CONTENT_ZH.md)。

## 相关度分数的定位

```text
relevance = semantic*0.70 + hazard*0.15 + industry*0.10 + consequence*0.05
```

版本 `hybrid-v1.0`，分项与命中词存进 `incident_factory_matches.rationale_json`。

**它是候选检索器，不是决策器。** 分数只影响送进第 ③ 层的候选顺序，不决定
是否提醒人。是否提醒由残余路径验证决定。这条分工是刻意的：权重 70/15/10/5
没有金标准支撑，而召回率是可以验证的。

分数表示「外部事故与本厂已登记风险的相关程度」，不是事故概率、合规结论或
自动行动指令。

## 数据边界

| 层 | 内容 | 位置 |
|---|---|---|
| 原始证据 | OSHA ZIP/CSV/XML/RSS 原文与 SHA-256 | `data/raw/`、`corpus_snapshots` |
| 关系事实 | 105,996 条事故；虚拟工厂 97 条风险评估、66 条控制验证 | `incidents.db`、`factory_zh.db` |
| 接地语料 | 1,393 条可引用段落（29 CFR 8 节 + Fatal Facts 24 篇） | `corpus/grounding.db` |
| 检索 | 只保存可重建的向量 | Milvus 两个 Collection |
| 决策 | 匹配、评分版本、路由级别、人工决定、运行审计 | `rag_db` 各表 |

Agent 可以选择并调用工具，但不能绕过确定性数据写入和人工决定边界。

## Strands 工具

`rag_agent.build_agent()` 注册 6 个：

| 工具 | 层 | 说明 |
|---|---|---|
| `run_daily_surveillance` | ①② | 跑完整采集与匹配流水线 |
| `screen_factory_risk_paths` | ③ | 残余路径验证 + 四级路由，返回筛选结果不返回证书 |
| `collect_grounding_corpus` | ④ | 采集接地语料（一次性） |
| `draft_learning_pack` | ④ | 起草简报与双语班前会，返回审计摘要不返回稿件 |
| `get_pending_decisions` | ⑤ | 待人工决定队列 |
| `record_match_decision` | ⑤ | 记录明确的人工决定 |

## 其他几张图分别是什么

| 文件 | 是什么 | 什么时候看 |
|---|---|---|
| `HINDSIGHT_WORKFLOW.excalidraw`<br>`hindsight-workflow.png` | 队友画的 7 步线性流水线 | 只描述第 ①② 层，早于 ③④ 层存在。**已被本文件取代** |
| `architecture.mmd` | 15 节点压缩单链 | 提交用架构图的备选版本 |
| `roadmap-44-nodes.svg` | 44 节点 8 层长期蓝图 | README 的「下一阶段」，展示边界想清楚了 |
| Notion《功能工作流 V1.0》 | 29 个功能点、失败兜底与状态机 | 功能验收清单，编号 W/G/A/D/R 与本文件的层对应见下 |

Notion 编号 → 本文件的层：
`W1–W4`→① · `W5a/W5b`→② · `W5c/V1/W6`→③ · `G1–G4`→④ · `A1–A3`→⑤ ·
`D1–D5`、`R1–R4`→尚未实现，见 `roadmap-44-nodes.svg`。
