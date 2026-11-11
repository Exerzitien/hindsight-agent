# 29 个功能点落地对照

对应 Notion《Hindsight Agent 功能工作流 V1.0》。**P0 共 22 个，全部落地。**
最后核对 2026-09-08 · 236 项测试通过。

一条命令跑完整链路：

```bash
hindsight-workflow --limit 500          # 筛选 → 判定 → 路由 → 归档 → 队列 → 提醒 → 晨报
hindsight-workflow --limit 500 --draft  # 另外对红/黄级调用模型起草（需模型服务）
```

## 阶段 0 · 一次性设置

| 编号 | 功能 | 实现 | 兜底 |
|---|---|---|---|
| S1 | 工厂档案录入 | `factory_db.py`，20 设备 / 97 风险评估 / 66 控制验证，对齐 5 项 ISO/IEC | 缺失字段显式为 unknown，判定时降级为 UNKNOWN 而非 False |
| S2 | 档案向量索引 | `rag_documents.build_factory_risk_documents` + `milvus_store` | 内容哈希驱动重索引 |
| S3 | 规则包 v1 初始化 | `rule_pack.initialize`，保守默认（静音空、预算 5 条/周） | 版本化，见 R4 |
| S4 | 分发渠道配置 | `distribution.register_channels`，15 个渠道按班次/岗位/语言，覆盖登记册暴露的全部 7 个岗位 × 英西双语 | demo 用模拟收件箱 `data/outbox/mock_inbox.jsonl`；岗位无渠道时返回 `APPROVED_BUT_NO_CHANNEL`，不退回广播 |

## 阶段 1 · 每日感知与调查

| 编号 | 功能 | 实现 | 实测 |
|---|---|---|---|
| W1 | 定时唤醒 | `scripts/register_daily_task.ps1` / EventBridge | — |
| W2 | 拉取事故流 | `tools/collect_osha.py` + `collect_osha_news.py`，SHA-256 快照 | F1：抓取失败保留 last-good cursor，不静默假装成功 |
| W3 | 去重与 Case Memory | `workflow_db` 状态机 + `open_case` 幂等 | 重跑 500 条，新增案例 **0** |
| W4 | 结构化抽取 + 置信度 | `screening.extract_with_confidence`，字段完整度 + OIICS 弱标签一致性 | F2：低于 0.55 进 `quarantine`，每日抽检 10 条 |
| W5a | **硬过滤** | `screening.hard_filter`，NAICS 邻近 + 本厂危险词表 + 静音项 | **105,996 条 → 剔除 87.2%，3.1 秒，零模型调用** |
| W5b | 向量粗排 | `milvus_store.search_factory` top-k；无向量库时回落到确定性候选检索 | 可注入 `candidate_finder` |
| W5c | 证据调查 | `transfer.assess_paths` 残余路径验证（确定性图遍历，强于 LLM 自述证据） | 五条优先级规则，各有独立测试 |
| V1 | 相关性分级 | `transfer.route_path` 红/黄/绿/无关 | F3：**UNKNOWN 永不进红**，高严重×低置信强制降黄 |
| W6 | 静默归档 | `workflow` 显式 `ARCHIVED` 状态 + `screening.weekly_archive_digest` | 每条归档一行进周报，证明系统在看 |

**警报疲劳的两道闸门**（研究报告 P1 风险，规格未写但必须有）：

- 路径节流：同一条危险路径已在某人面前时，第二起事故作为佐证归档，不重复开案
- 提醒预算：超出规则包 `weekly_alert_budget` 的红降为黄进批量确认，**不丢弃**

实测 500 条事故 → 静默归档 496、进入人工队列 4（4 条不同路径）。

## 阶段 2 · 内容生成

| 编号 | 功能 | 实现 | 兜底 |
|---|---|---|---|
| G1 | 检索接地语料 | `corpus_db` + `tools/collect_grounding` + `grounding`，1,393 条可引段落 | 语料一次下载，之后全程离线 |
| G2 | 证据链简报 | `learning_pack.render_briefing`，仿 Fatal Facts 五节，逐行带出处 | 可点回原始记录 |
| G3 | 班前会 + 测验 | `learning_pack.render_toolbox_talk`，EN/ES 同稿双语 | 双语由同一份主张渲染，防漂移 |
| G4 | 接地自检 | `self_check`，十项确定性核查 | F4：无出处整句删除；重生成 1 次仍失败 → `NEEDS_HUMAN_WRITER`；免责声明由代码注入 |

## 阶段 3 · 人类决策

| 编号 | 功能 | 实现 | 兜底 |
|---|---|---|---|
| A1 | 审批队列 + **hook 强制拦截** | `authorization.DistributionGuard` 挂在 `BeforeToolCallEvent`，未批准的调用被 `cancel_tool` 取消 | 状态机层面也无 `APPROVED` 之外通往 `DISTRIBUTED` 的边 |
| A2 | 审阅与决策 | `workflow.approve_and_distribute` / `reject_case`，支持起草前直接驳回 | F5：`due_reminders` 提醒升级，**永不自动发送** |
| A3 | 决策记录 | `case_transitions` 只追加，自增 `seq` 保证审计链顺序 | 驳回进 `feedback_signals`，成为阶段 5 的燃料 |

拦截实测两种情形：案例未批准 → `POLICY_CASE_NOT_APPROVED`；稿件被编辑后旧 token 失效 → `POLICY_TOKEN_PACK_MISMATCH`。

## 阶段 4 · 分发与追踪

| 编号 | 功能 | 实现 | 兜底 |
|---|---|---|---|
| D1 | 定向分发 | `distribution.deliver`，按危险路径的暴露岗位 + 语言定向 | 幂等键 = 案例+渠道+内容哈希+语言 |
| D2 | 完成与测验追踪 | `completions` 表 + `completion_status` | 状态机驱动，可随时重算 |
| D3 | 提醒与升级 | `due_reminders` + `send_reminder_record`，T+24h / T+72h | 幂等键 = 案例+阶段，不轰炸 |
| D4 | 案例闭环 + 审计 | `close_case` 返回源事故→路径→证据→稿件→决策→触达→完成的完整链 | 评委可逐步核查 |
| D5 | 周一晨报 | `monday_digest` | 无告警的周也发：「本周没有与本厂相关的外部事故」 |

## 阶段 5 · 进化循环

| 编号 | 功能 | 实现 | 约束 |
|---|---|---|---|
| R1 | 汇集反馈信号 | `workflow_db.record_signal`，只追加 | 批准/驳回/未知阻断都进池 |
| R2 | Rule Pack diff 提案 | `rule_pack.propose_diffs`，一次只改一处，每条附证据 | 连续 2 次驳回 → 建议静音该类别 |
| R3 | 人工批准 diff | `rule_pack.decide_diff` | 必须记名，拒绝匿名批准 |
| R4 | 版本化 + 一键回滚 | `rule_pack.rollback` + `version_history` | 旧版本置 retired，**从不删除** |

### 安全内核不可通过 diff 修改

`rule_pack.IMMUTABLE_KEYS` 在代码层拒绝任何触及以下项的提案：

```
require_evidence_for_every_claim        每条主张必须有出处
require_human_approval_before_distribution  未批不触达
unknown_never_routes_red                未知永不冒充确定
cross_site_isolation                    跨厂隔离
numerals_must_be_sourced                数值必须来自来源
```

这是「会学习的系统」与「会漂移的系统」的分界：任何一串被接受的 diff 都不可能
侵蚀这五条。有独立测试锁定。

## 五条铁律的代码位置

| 铁律 | 由什么保证 |
|---|---|
| 未批不触达 | `DistributionGuard` hook + 状态机无旁路边 + `deliver` 二次验签 |
| No Evidence → No Alert | `self_check` 十项核查，无出处整句删除 |
| 无出处不写 | `learning_pack` 主张契约：模型只能产出带 citation 的原子主张 |
| 重跑幂等 | `open_case` / `deliver` / `send_reminder_record` 三处幂等键 |
| 一切留痕 | `case_transitions` 只追加 + 自增 seq |

## 新增模块

| 文件 | 覆盖 |
|---|---|
| `workflow_db.py` | W3 · A3 · 九张状态与治理表 |
| `rule_pack.py` | S3 · R1–R4 |
| `screening.py` | W4 · W5a · W6 |
| `authorization.py` | **A1** |
| `distribution.py` | S4 · D1–D5 |
| `workflow.py` | 总编排 W1→D5 |
| `tests/test_workflow.py` | 34 项 |
| `tests/test_distribution_coverage.py` | 渠道表与登记册岗位 id 的漂移守卫 |
