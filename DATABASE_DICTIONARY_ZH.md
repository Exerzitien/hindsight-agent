# Hindsight Agent 数据库说明书

本文档按数据库说明当前项目里保存了什么数据，以及每张表和关键字段分别代表什么。

## 先认识几个词

| 名称 | 含义 |
|---|---|
| 数据库 | 一个 `.db` 文件，里面可以包含多张表和视图 |
| 表（table） | 真正保存数据的二维表，由行和列组成 |
| 视图（view） | 已经写好的查询窗口，本身通常不重复保存数据 |
| 主键（PK） | 一行数据的唯一编号 |
| 外键（FK） | 指向另一张表的编号，用来建立表之间的关系 |
| `TEXT` | 文本，例如企业名称、事故描述 |
| `INTEGER` | 整数，例如人数、是否启用（`1` 是是，`0` 是否） |
| `REAL` | 小数，例如经纬度和平面图坐标 |

## 数据库总览

| 数据库 | 文件位置 | 数据性质 | 主要用途 | 当前规模 |
|---|---|---|---|---:|
| `factory.db` | `data/factory/factory.db` | 标准概念对齐的虚拟工厂，英文内容 | 保存资产台账、作业风险、控制验证和整改闭环 | 20 台设备、97 条风险评估 |
| `factory_zh.db` | `data/factory/factory_zh.db` | 标准概念对齐的虚拟工厂，中文内容 | `factory.db` 的中文版，提供答辩用中文视图 | 20 台设备、97 条风险评估 |
| `incidents.db` | `data/incidents/incidents.db` | OSHA 官方公开数据 | 保存外部真实事故和 OSHA 官方公告 | 105,996 条事故、5 条公告 |

> 当前项目还没有 Milvus 数据库。这三个文件都是 SQLite。以后接入 Milvus 时，通常把事故描述等文本转成向量放入 Milvus，SQLite 继续保存完整、可核对的原始记录。

---

## 一、`factory.db`：英文虚拟工厂数据库

这是一个虚拟的综合金属制造工厂，用来代表 Agent 需要检查的“我的工厂”。数据不是某一家真实企业的内部资料。

### 1. 主要业务表

| 表名 | 行数 | 表里有什么 | 关键字段 |
|---|---:|---|---|
| `database_metadata` | 9 | 数据库版本、种子哈希、标准配置哈希和数据分类 | `key`：配置名；`value`：配置值 |
| `factories` | 1 | 工厂的基本档案 | `id`、`name`、`industry`、`naics`、`description`、`fictional` |
| `areas` | 6 | 工厂内部区域，例如冲压区、焊接区、仓储区 | `factory_id`、`code`、`name`、`description`、平面图坐标 |
| `equipment` | 20 | 设备资产台账及其所在区域 | 资产编号、制造商、型号、序列号、投用日期、关键度、责任岗位、检查日期 |
| `tasks` | 22 | 工人在设备上执行的工作任务 | `name`、`description` |
| `hazards` | 21 | 设备可能涉及的危险 | `name`、`category`、`description` |
| `worker_roles` | 12 | 设备暴露岗位、资产责任岗位、评估和审批岗位 | `name`、`description` |
| `energy_sources` | 7 | 设备的危险能源类型 | `name`、`description` |
| `materials` | 10 | 设备加工或接触的材料 | `name`、`description` |
| `safety_controls` | 15 | 已配置的安全控制措施及验证要求 | 控制层级、性能要求、验证方法、验证周期 |

### 2. 设备关系表

一台设备可以对应多个任务和危险，一个危险也可以同时出现在多台设备上，因此要用中间关系表保存这种“多对多”关系。

| 表名 | 行数 | 左侧字段 | 右侧字段 | 表示什么 |
|---|---:|---|---|---|
| `equipment_tasks` | 59 | `equipment_id` | `task_id` | 某台设备会执行哪些任务 |
| `equipment_hazards` | 97 | `equipment_id` | `hazard_id` | 某台设备存在哪些危险 |
| `equipment_worker_roles` | 48 | `equipment_id` | `worker_role_id` | 哪些岗位会接触某台设备 |
| `equipment_energy_sources` | 55 | `equipment_id` | `energy_source_id` | 某台设备有哪些危险能源 |
| `equipment_materials` | 48 | `equipment_id` | `material_id` | 某台设备会接触哪些材料 |
| `equipment_safety_controls` | 66 | `equipment_id` | `safety_control_id` | 某台设备已经配置哪些安全措施 |

### 3. v2 标准治理与风险表

| 表名 | 行数 | 表里有什么 | 现实企业用途 |
|---|---:|---|---|
| `standards` | 5 | ISO/IEC 标准编号、版本、适用范围和官方链接 | 追溯数据模型依据 |
| `schema_standard_mappings` | 8 | 标准概念与数据库对象之间的映射 | 答辩或审计时解释字段来源 |
| `control_hierarchy_levels` | 5 | 消除、替代、工程、管理和 PPE 五级控制层级 | 按控制有效性确定优先顺序 |
| `risk_matrix_bands` | 4 | 低、中、高、重大四个示例风险区间 | 将 5×5 分值转成处理等级 |
| `risk_assessments` | 97 | 设备、任务、危险情境、初始风险和剩余风险 | 企业风险登记册主体 |
| `risk_assessment_controls` | 182 | 每条风险实际采用了哪些控制措施 | 证明风险降低与控制之间的关系 |
| `control_verifications` | 66 | 控制的测试日期、结果、发现、证据和下次到期日 | 证明控制持续有效 |
| `corrective_actions` | 14 | 验证异常或高剩余风险的整改任务 | 建立责任人和期限闭环 |

### 4. 核心字段说明

| 表 | 字段 | 类型 | 含义 |
|---|---|---|---|
| `factories` | `id` | `INTEGER PK` | 工厂唯一编号 |
| `factories` | `name` | `TEXT` | 工厂名称 |
| `factories` | `industry` | `TEXT` | 所属行业 |
| `factories` | `naics` | `TEXT` | 北美行业分类代码 |
| `factories` | `profile_type` | `TEXT` | 工厂档案类型 |
| `factories` | `source_note` | `TEXT` | 数据来源说明 |
| `factories` | `fictional` | `INTEGER` | 是否为虚拟工厂，`1` 表示是 |
| `factories` | `site_code` | `TEXT` | 工厂站点代码 |
| `factories` | `iec_62264_level` | `TEXT` | IEC 62264 制造层级，本表固定为 `site` |
| `areas` | `factory_id` | `INTEGER FK` | 这个区域属于哪座工厂 |
| `areas` | `code` | `TEXT` | 区域短编号 |
| `areas` | `map_x`、`map_y` | `REAL` | 区域在虚拟平面图上的位置 |
| `areas` | `map_width`、`map_height` | `REAL` | 区域在虚拟平面图上的大小 |
| `equipment` | `area_id` | `INTEGER FK` | 设备所在区域 |
| `equipment` | `asset_code` | `TEXT` | 设备资产编号 |
| `equipment` | `equipment_type` | `TEXT` | 设备类别 |
| `equipment` | `manufacturer`、`model_number`、`serial_number` | `TEXT` | 制造商、型号和唯一序列号 |
| `equipment` | `commissioned_on`、`lifecycle_status` | `TEXT` | 投用日期和在用/备用/退役状态 |
| `equipment` | `criticality_class` | `TEXT` | 设备关键度，示例使用 A/B/C |
| `equipment` | `owner_role_id` | `TEXT FK` | 设备资产责任岗位 |
| `equipment` | `last_inspected_on`、`next_inspection_due` | `TEXT` | 上次和下次设备检查日期 |
| `equipment` | `active` | `INTEGER` | 设备是否在用，`1` 表示在用 |
| `hazards` | `category` | `TEXT` | 危险分类，例如机械、电气、化学 |
| `safety_controls` | `control_type` | `TEXT` | 控制措施类型，例如工程控制、管理控制、PPE |
| `safety_controls` | `hierarchy_code` | `TEXT FK` | 稳定的五级控制层级代码 |
| `safety_controls` | `performance_requirement` | `TEXT` | 控制措施必须达到的性能要求 |
| `risk_assessments` | `initial_*`、`residual_*` | 数值和文本 | 控制前后的可能性、严重度、风险分和等级 |
| `risk_assessments` | `risk_accepted` | `INTEGER` | 剩余风险是否可接受 |
| `control_verifications` | `result`、`evidence_reference` | `TEXT` | 验证结果和证据编号 |
| `corrective_actions` | `owner_role_id`、`due_on`、`status` | 外键和文本 | 整改责任岗位、期限和执行状态 |

### 5. 表之间的关系

```text
factories（工厂）
  └─ areas（区域）
      └─ equipment（设备）
          ├─ equipment_tasks ─────────── tasks（任务）
          ├─ equipment_hazards ───────── hazards（危险）
          ├─ equipment_worker_roles ──── worker_roles（岗位）
          ├─ equipment_energy_sources ── energy_sources（能源）
          ├─ equipment_materials ─────── materials（材料）
          └─ equipment_safety_controls ─ safety_controls（安全措施）
          ├─ risk_assessments（风险评估）
          │   └─ risk_assessment_controls（风险采用的控制）
          ├─ control_verifications（控制验证）
          └─ corrective_actions（整改行动）

standards（国际标准）
  └─ schema_standard_mappings（标准与字段概念映射）
```

---

## 二、`factory_zh.db`：中文虚拟工厂数据库

这个数据库和 `factory.db` 描述的是同一座虚拟工厂，底层表结构和数据量相同，但名称、描述等业务内容已转成中文，并增加了适合直接阅读的中文视图。

### 1. 底层数据表

| 表名 | 行数 | 中文含义 | 说明 |
|---|---:|---|---|
| `database_metadata` | 9 | 数据库元数据 | 版本、哈希、语言和标准核验日期 |
| `factories` | 1 | 工厂 | 保存中文工厂档案 |
| `areas` | 6 | 区域 | 保存中文区域名称和说明 |
| `equipment` | 20 | 设备 | 保存中文设备名称、类别和说明 |
| `tasks` | 22 | 任务 | 保存中文作业任务 |
| `hazards` | 21 | 危险 | 保存中文危险名称和说明 |
| `worker_roles` | 12 | 岗位 | 包含操作岗位、EHS 安全经理和生产主管 |
| `energy_sources` | 7 | 能源来源 | 保存中文危险能源类型 |
| `materials` | 10 | 材料 | 保存中文材料名称 |
| `safety_controls` | 15 | 安全控制 | 保存中文安全措施 |
| `equipment_tasks` | 59 | 设备与任务关系 | 设备会执行什么任务 |
| `equipment_hazards` | 97 | 设备与危险关系 | 设备可能有什么危险 |
| `equipment_worker_roles` | 48 | 设备与岗位关系 | 哪些岗位会接触设备 |
| `equipment_energy_sources` | 55 | 设备与能源关系 | 设备涉及什么危险能源 |
| `equipment_materials` | 48 | 设备与材料关系 | 设备会接触什么材料 |
| `equipment_safety_controls` | 66 | 设备与安全措施关系 | 设备已有何种保护措施 |
| `standards` | 5 | 国际标准 | 标准编号、官方链接和本项目对齐范围 |
| `schema_standard_mappings` | 8 | 标准映射 | 标准概念对应哪些表和字段 |
| `control_hierarchy_levels` | 5 | 控制层级 | 消除、替代、工程、管理和 PPE |
| `risk_matrix_bands` | 4 | 风险区间 | 示例 5×5 风险矩阵等级 |
| `risk_assessments` | 97 | 风险评估 | 初始风险、控制后剩余风险和复审日期 |
| `risk_assessment_controls` | 182 | 风险与控制关系 | 每条风险用了哪些控制措施 |
| `control_verifications` | 66 | 控制验证 | 验证方法、结果、证据和到期日 |
| `corrective_actions` | 14 | 整改行动 | 异常和未接受风险的责任闭环 |

底层字段名仍然使用英文，含义与上一节 `factory.db` 完全一致。程序开发时可以共用同一套代码。

### 2. 中文视图

| 视图名 | 行数 | 包含的列 | 适合做什么 |
|---|---:|---|---|
| `工厂` | 1 | 工厂ID、工厂名称、行业、NAICS、档案类型、来源说明 | 快速查看工厂基本信息 |
| `区域` | 6 | 区域代码、区域名称、区域说明、平面图X、平面图Y、宽度、高度 | 查看工厂区域和平面图位置 |
| `设备` | 20 | 设备编号、设备名称、设备类型、区域代码、所在区域、设备说明 | 按区域查看所有设备 |
| `设备完整档案` | 20 | 设备编号、设备名称、所在区域、相关任务、相关危险、相关岗位、能源来源、安全控制 | 一行看懂一台设备的完整风险档案 |
| `标准依据` | 5 | 标准编号、名称、版本、发布机构、对齐范围、官方来源 | 答辩时展示数据模型依据 |
| `资产台账` | 20 | 资产身份、制造商、型号、序列号、关键度、责任岗位和检查日期 | 展示现实企业设备台账结构 |
| `风险登记册` | 97 | 危险情境、初始风险、剩余风险、评估和复审信息 | 展示风险评估主流程 |
| `控制措施验证` | 66 | 性能要求、验证方法、结果、发现、证据和下次日期 | 展示控制是否持续有效 |
| `整改行动` | 14 | 整改内容、优先级、责任岗位、期限和状态 | 展示问题如何形成闭环 |

初次查看可从 `资产台账` 和 `设备完整档案` 开始；答辩建议依次展示 `标准依据`、`风险登记册`、`控制措施验证` 和 `整改行动`，这样能讲清从标准、识别、评价、控制到改进的完整闭环。

---

## 三、`incidents.db`：OSHA 真实事故数据库

这个数据库保存从 OSHA 官方公开来源收集的数据。事故原文和分类名称主要保留英文，以便追溯证据；同时提供中文列名视图，方便查看。

### 1. 表和视图总览

| 类型 | 名称 | 行数 | 里面有什么 |
|---|---|---:|---|
| 表 | `database_metadata` | 1 | 数据库结构版本 |
| 表 | `source_snapshots` | 2 | 每次导入所对应的原始来源、哈希和本地快照目录 |
| 表 | `osha_sir_incidents` | 105,996 | OSHA Severe Injury Reports 严重工伤记录 |
| 表 | `osha_news_releases` | 5 | OSHA 官方新闻和执法公告 |
| 视图 | `事故记录` | 105,996 | 使用中文列名展示事故核心字段 |
| 视图 | `官方公告` | 5 | 使用中文列名展示公告核心字段 |

### 2. `source_snapshots`：来源快照表

这张表回答“这条数据从哪里来、什么时候收集、对应哪个原始文件”。它是证据追溯的入口。

| 字段 | 类型 | 含义 |
|---|---|---|
| `source_key` | `TEXT PK` | 来源快照唯一编号 |
| `source_type` | `TEXT` | 来源类型，例如 OSHA SIR 或 RSS |
| `snapshot_id` | `TEXT` | 本地快照编号 |
| `source_url` | `TEXT` | 官方来源网址 |
| `collected_at` | `TEXT` | 收集时间 |
| `sha256` | `TEXT` | 原始文件的数字指纹，用于判断文件是否变化 |
| `snapshot_directory` | `TEXT` | 原始快照在本地的目录 |
| `imported_at` | `TEXT` | 导入 SQLite 的时间 |

### 3. `osha_sir_incidents`：严重工伤事故表

这是事故数据库的主体，每一行代表 OSHA SIR 数据中的一条严重工伤记录。

| 字段分组 | 字段 | 类型 | 含义 |
|---|---|---|---|
| 唯一标识 | `incident_key` | `TEXT PK` | 本数据库事故唯一键，由 `ID:UPA` 组合，避免 OSHA 原始 ID 重复 |
| 唯一标识 | `incident_id` | `TEXT` | OSHA 数据中的事故 ID |
| 唯一标识 | `upa` | `TEXT` | OSHA 的 UPA 编号，用于进一步区分事故 |
| 时间 | `event_date` | `TEXT` | 事故发生日期，使用 ISO 日期格式 |
| 企业 | `employer` | `TEXT` | 发生事故的企业名称 |
| 地址 | `address1`、`address2` | `TEXT` | 事故地点街道地址 |
| 地址 | `city`、`state`、`zip_code` | `TEXT` | 城市、州、邮编 |
| 地理位置 | `latitude`、`longitude` | `REAL` | 事故地点经纬度 |
| 行业 | `primary_naics` | `TEXT` | 企业的主要 NAICS 行业代码 |
| 后果 | `hospitalized` | `INTEGER` | 住院人数 |
| 后果 | `amputation` | `INTEGER` | 截肢人数 |
| 后果 | `loss_of_eye` | `INTEGER` | 失眼人数 |
| 调查 | `inspection` | `TEXT` | OSHA 调查编号，若有则表示可能已开展调查 |
| 事故内容 | `narrative` | `TEXT` | 事故经过的文字描述，是后续语义匹配的核心文本 |
| 伤害性质 | `nature_code`、`nature_title` | `TEXT` | 伤害性质代码和名称，例如骨折、割伤 |
| 受伤部位 | `body_part_code`、`body_part_title` | `TEXT` | 身体受伤部位代码和名称 |
| 事故类型 | `event_code`、`event_title` | `TEXT` | 事故事件类型代码和名称，例如被物体击中、卷入设备 |
| 伤害来源 | `source_code`、`source_title` | `TEXT` | 直接导致伤害的主要物体或来源 |
| 次要来源 | `secondary_source_code`、`secondary_source_title` | `TEXT` | 与事故相关的第二来源 |
| 管辖 | `federal_state` | `INTEGER` | 联邦或州计划相关标记，按 OSHA 原始值保存 |
| 数据校验 | `content_hash` | `TEXT` | 本行内容哈希，用于判断记录是否发生变化 |
| 数据来源 | `source_snapshot_key` | `TEXT FK` | 指向 `source_snapshots`，说明来自哪个快照 |
| 导入信息 | `imported_at` | `TEXT` | 记录导入数据库的时间 |

### 4. `osha_news_releases`：OSHA 官方公告表

每一行是一篇 OSHA 官方新闻、处罚或执法公告。它适合补充 SIR 批量数据截止日期之后的新事故线索。

| 字段 | 类型 | 含义 |
|---|---|---|
| `entry_id` | `TEXT PK` | 公告在本数据库中的唯一编号 |
| `source_id` | `TEXT UNIQUE` | 来源系统中的唯一编号 |
| `title` | `TEXT` | 公告标题 |
| `url` | `TEXT` | OSHA 官方公告链接 |
| `published_at` | `TEXT` | 公告发布时间 |
| `summary` | `TEXT` | 公告摘要 |
| `content_hash` | `TEXT` | 内容数字指纹，用于检测更新 |
| `first_collected_at` | `TEXT` | 第一次收集到该公告的时间 |
| `last_collected_at` | `TEXT` | 最近一次确认该公告的时间 |
| `source_snapshot_key` | `TEXT FK` | 指向对应来源快照 |
| `imported_at` | `TEXT` | 导入数据库的时间 |

### 5. 中文视图

| 视图名 | 包含的列 | 用途 |
|---|---|---|
| `事故记录` | 事故ID、事故日期、企业、记录键、UPA、城市、州、NAICS、住院人数、截肢人数、失眼人数、事故描述、伤害性质、受伤部位、事故类型、伤害来源、OSHA调查编号、来源快照 | 用中文列名浏览和筛选严重工伤记录 |
| `官方公告` | 公告ID、标题、发布日期、链接、摘要、来源快照 | 用中文列名浏览 OSHA 官方公告 |

### 6. 表之间的关系

```text
source_snapshots（来源快照）
  ├─ osha_sir_incidents（严重工伤事故）
  └─ osha_news_releases（官方公告）
```

---

## 四、三个数据库在 Agent 中怎么配合

| 步骤 | 读取哪个数据库 | 做什么 |
|---|---|---|
| 1. 收集外部事故 | `incidents.db` | 保存真实事故、事故描述、行业、伤害类型和来源证据 |
| 2. 读取我的工厂 | `factory_zh.db` | 查找工厂有哪些设备、任务、危险、岗位和安全措施 |
| 3. 风险匹配 | 两个数据库一起读 | 比较外部事故与本厂设备、任务和危险是否相似 |
| 4. 输出行动建议 | 两个数据库一起读 | 说明相关设备、可能受影响岗位、现有控制和需要检查的地方 |

最核心的逻辑可以理解为：

```text
外部真实事故（incidents.db）
          ↓
识别事故中的设备、任务、危险和伤害模式
          ↓
查询我的虚拟工厂（factory_zh.db）
          ↓
找到相似设备/任务/危险，并生成检查建议
```
