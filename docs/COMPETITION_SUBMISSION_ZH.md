# Agents for Humans 提交检查表

## Track

推荐 **Professional Agents**。目标用户是 EHS / 安全管理人员，Agent 自动完成每天重复的
外部事故监控、证据保存、增量向量化和内部风险匹配，只在存在相关工厂风险时要求人工决定。

## 文本描述草稿

Hindsight Safety Agent continuously monitors official OSHA severe-injury data and news releases,
preserves auditable source snapshots, and incrementally indexes new evidence in Milvus. A Strands
Agent matches each new incident against a standards-aligned factory risk register using multilingual
embeddings and explainable hybrid scoring. It stays in the background and surfaces only high-relevance
matches for an EHS professional to confirm or dismiss. Every collection, vector version, score, and
human decision is traceable.

## 5 分钟演示脚本

1. `0:00-0:40` 问题：EHS 人员不可能每天人工浏览所有外部事故并逐条对照厂内设备。
2. `0:40-1:10` 用户与价值：面向制造企业安全管理人员，把重复搜索变成只处理真正决定。
3. `1:10-2:00` 运行 `hindsight-daily-agent`，在 Jaeger 展示 Strands Agent 和 Tool 轨迹。
4. `2:00-2:40` 展示原始快照、SHA-256、SQLite 新增/未变化计数。
5. `2:40-3:30` 展示 Milvus 两个 Collection 和增量向量数。
6. `3:30-4:30` 打开控制台，查看分项证据并确认一条相关匹配。
7. `4:30-5:00` 展示队友读取人工确认记录进入下一模块。

## 检查表

- [x] Strands Agent 与自定义工具
- [x] 端到端工作流，不是聊天外壳
- [x] README、架构图、安装说明和测试
- [x] 可编辑 Excalidraw 工作流与可提交 SVG
- [x] Apache-2.0 License
- [x] Milvus、Ollama、Jaeger 本地部署
- [x] 人工决定边界、审计日志和队友文档
- [ ] PUBLIC GitHub 仓库和 About 区 License
- [ ] 最多 5 分钟演示视频
- [ ] AWS Builder ID 和 Devpost 表单
- [ ] 可选：AgentCore / live demo
- [ ] 可选：标题含 “Agents for Humans” 的 builder.aws.com 文章
