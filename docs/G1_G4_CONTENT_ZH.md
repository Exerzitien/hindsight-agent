# 阶段 2 · 内容生成（G1–G4）实现说明

对应 Notion《功能工作流 V1.0》阶段 2 的四个 P0 功能点，以及第三条铁律
「无出处不写」。

## 一句话

模型不写段落，只产出**带出处的原子主张**；G4 用确定性代码逐条核查，核查不过的
整句删除。英语和西班牙语是同一份主张的两种渲染，不是两次生成。

## 新增文件

| 文件 | 对应 | 职责 |
|---|---|---|
| `src/hindsight_agent/corpus_db.py` | G1 | 接地语料库 schema：快照、文档、可引用段落 |
| `src/hindsight_agent/tools/collect_grounding.py` | G1 | 三个采集适配器（29 CFR / Fatal Facts / FACE） |
| `src/hindsight_agent/grounding.py` | G1 | 检索侧：硬过滤 + BM25，产出证据包 |
| `src/hindsight_agent/learning_pack.py` | G2·G3 | 主张契约与双语渲染 |
| `src/hindsight_agent/content.py` | G2·G3 | 结构化生成与 G1→G4 编排 |
| `src/hindsight_agent/self_check.py` | G4 | 十项确定性核查与删除/重生成 |
| `tests/test_grounding_content.py` | — | 28 个测试 |

未修改队友任何既有文件。

## 命令

```bash
# 一次性：下载并冻结语料（之后全程离线）
hindsight-grounding-collect                      # 29 CFR + Fatal Facts
hindsight-grounding-collect --face-dir ./face    # 另加手工放置的 NIOSH FACE

# 为一条危险路径 + 一条事故生成草稿
hindsight-draft risk-press-01-crush 20251110940:2375211 --out pack.json
```

Strands 工具：`collect_grounding_corpus`、`draft_learning_pack`。

## G1 · 接地语料

三个来源，全部先冻结原始字节和 SHA-256，再解析。

| 来源 | 端点 | 状态 |
|---|---|---|
| 29 CFR | eCFR versioner API，`subtitle=B&chapter=XVII&part=1910&section=…` | 已接入，8 节 1,317 段 |
| OSHA Fatal Facts | `osha.gov/publications/fatal-facts` 列表 + PDF | 已接入，24 篇 76 段（含 3 篇官方西语版） |
| NIOSH FACE | 手工放置目录 | 适配器已写，见下方限制 |

**版本固定**：eCFR 的 issue date 从 `titles.json` 读取，不写死。本次采集固定在
`2026-08-31`。

**引用策略**：CFR 只做**节级引用**（`29 CFR 1910.217`），另存该段印刷体的段落
标记（如 `(a)`）。本模块**不**根据位置拼装 `(a)(3)(ii)` 这类层级路径——`(i)` 在
CFR 里既可能是第 9 个小写字母也可能是罗马数字一，拼错一条法规引用正是本项目要
防的那类错误。段落原文随引用一起存，人工可逐字核对。

**章节角色归一**：Fatal Facts 有三代版式（2012 / 2013–2023 / 2025）两种语言，
标题各不相同。全部归一到 `SUMMARY / DESCRIPTION / CAUSE / PREVENTION`，G2、G3
可以直接要「预防措施那一节」而不用管版式和语言。标题表里每一条都是从已发布 PDF
上读下来的，没有一条是猜的。

**检索**：先用本厂词表（`matching.HAZARD_TERMS` + 设备台账名称）硬过滤，再
BM25 排序。返回结果带命中词、命中危险、命中设备，可解释。实测一条冲床卡料截指
事故的召回首位是 `29 CFR 1910.217`（机械压力机）与 `1910.147`（上锁挂牌）。

## G2 · 证据链简报

仿 Fatal Facts 版式，五节：`WHAT HAPPENED` / `WHY IT APPLIES TO THIS SITE` /
`WHAT THE STANDARD REQUIRES` / `RECOMMENDED ACTION` / `NOT ESTABLISHED`。
每一行都带它所依据主张的出处 id，可点回原始记录。

## G3 · 班前会与测验

从同一份主张渲染英西双语，防止内容漂移。测验题绑定 `claim_id`——依据被删，题目
一起删。

**关于西班牙语**：OSHA 只对少数 Fatal Facts 出西语版，29 CFR 没有官方西语版。
所以西语句子引用的仍然是英文权威来源，整包打上 `translated_from_en` 标记。这不
违反铁律：译句仍然是**有出处的**句子；无出处的句子英西一样删。若某条主张确有
官方西语来源（语料库里已有 2 段西语 `PREVENTION`），G1 会直接给出来。

## G4 · 接地自检

十项核查，全部是查表或字符串比对，不含任何模型判断：

| 检查 | 内容 |
|---|---|
| `CITATION_PRESENT` | 至少有一条出处 |
| `CITATION_RESOLVES` | 每条出处都能解析到真实记录 |
| `CITATION_TYPE` | 出处种类与主张类型匹配（法规主张必须引 CFR；控制建议不能引事故叙述） |
| `QUOTE_VERBATIM` | 引文与来源逐字一致 |
| `NUMERALS_SUPPORTED` | 主张里的每个数值都必须出现在其来源中 |
| `BILINGUAL_PRESENT` | 西语文本存在 |
| `BILINGUAL_NUMERALS` | 英西数值一致 |
| `CONTROL_VOCABULARY` | 控制建议必须命名本厂已批准的控制 |
| `SITE_SCOPE` | 本厂引用必须属于本条危险路径 |
| `QUIZ_BACKED` | 测验题依据仍然存在 |

`NUMERALS_SUPPORTED` 是实战里最关键的一条：一条编造了安全距离、扭矩或检验周期
的安全指令读起来完全正常，而且危险。要求每个数值都在来源中出现，直接消灭这一整
类错误。

`UNKNOWN` 是唯一不需要出处的主张类型——它的内容就是「没有出处」，但必须写清楚缺
什么。

**失败处理**（对应 F4）：不合格主张直接删除 → 允许重生成一次，并把失败原因回传
给模型 → 仍然填不满必需章节则置为 `NEEDS_HUMAN_WRITER`，作为写作任务进队列，绝
不发一份内容单薄的稿子。免责声明由代码注入，不由模型生成。

## 已知限制

1. **NIOSH FACE 无法自动下载。** CDC 的边缘节点对本项目使用的 UA 直接返回
   403（`www.cdc.gov` 与 `stacks.cdc.gov` 均如此，连 `robots.txt` 都取不到）。
   没有绕过，改为手工放置目录 + 同样的快照与引用纪律。
2. **词表缺 “nip point”。** 真实的 29 CFR 1910.212(a)(1) 原文写的是
   “ingoing nip points”，而 `matching.HAZARD_TERMS['haz-caught-in']` 只收了
   “pinch point”，因此这段权威法规文本目前**打不上 `haz-caught-in` 标签**。这
   同时影响事故匹配器和语料标注。建议给该条加上 `"nip point"`、`"nip points"`
   —— 但这会改变匹配器行为，留给词表的负责人决定，本次未改。
3. **语料库不进 git。** `data/` 在 `.gitignore` 里，语料由
   `hindsight-grounding-collect` 复现。README 的安装步骤需要加这一条。
4. **西语覆盖薄。** 只有 3 篇 Fatal Facts 有官方西语版，且都不是金属加工场景。

## 测试

```bash
pytest tests/test_grounding_content.py -q     # 28 项
pytest tests/ -q                              # 全量 47 项
```

其中两项是回归测试，锁死曾经真实出现过的误报：把 “press 01 **in** the stamping
area” 读成测量值 `01in`，把西语 “5 **v**eces” 读成 `5V`。单字母单位已从数值正则
里移除。
