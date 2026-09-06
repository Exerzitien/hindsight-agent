# AgentCore 部署接入

AgentCore 是比赛技术实现加分项，不是硬性要求。本地演示先使用 Ollama + Docker Milvus；
云端运行时把模型切到 Bedrock，并把 `MILVUS_URI` 指向 AgentCore 可访问的 Milvus/Zilliz
Cloud 或 VPC 服务，不能填写本机 `localhost`。

## 官方 CLI 准备

```powershell
npm install -g @aws/agentcore
agentcore --help
aws sts get-caller-identity
```

当前官方 CLI 要求 Node.js 20+、Python 3.10+、AWS CDK、AWS 凭证和 Bedrock 模型访问权。

## 创建 Strands Runtime 外壳

```powershell
agentcore create --name HindsightSafetyAgent --framework Strands --protocol HTTP --model-provider Bedrock --memory none --build CodeZip
```

将生成项目的 Agent 入口改为导入 `hindsight_agent.rag_agent.build_agent`，并把本项目作为其
Python 依赖。设置：

```text
HINDSIGHT_MODEL_PROVIDER=bedrock
BEDROCK_MODEL_ID=<已获访问权限的 Bedrock 模型 ID>
MILVUS_URI=<云端可访问地址>
HINDSIGHT_INCIDENT_DB=<持久化数据库或云端状态存储>
```

```powershell
agentcore dev
agentcore validate
agentcore deploy --dry-run
agentcore deploy
agentcore invoke "Run today's OSHA surveillance workflow"
agentcore traces list
```

云端每日链路：

```text
EventBridge Scheduler -> Lambda -> InvokeAgentRuntime -> Hindsight AgentCore Runtime
```

Lambda 只负责带固定提示词调用 AgentCore，不重复实现采集或 RAG。生产部署还需要把 SQLite
状态迁移到持久服务或持久卷；本仓库的本地 SQLite 是比赛演示和队友交接实现。
