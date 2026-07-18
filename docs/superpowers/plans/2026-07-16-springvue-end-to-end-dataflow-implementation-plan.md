# SpringVue 端到端数据通路审查实施计划

## 1. 实施目标

在不改变能力注册中心和模型提供方接口的前提下，将 SpringVue 能力从“关系感知的分层审查”升级为“端点级证据图谱与链路审查”。

首版完成后，Agent 应能对常见课程设计代码形成：

```text
Vue/API 请求
  -> Controller 接口
  -> Service 方法
  -> Mapper 方法
  -> MyBatis SQL
  -> 数据库表
```

数据源、Profile、Nacos 和 MyBatis 配置作为链路上下文附加。无法证明的关系保留断链或待确认状态，不交给模型作为确定事实。

## 2. 实施约束

- 保持 Python 3.9+ 兼容。
- 首版不强制增加第三方依赖，避免扩大 Windows/macOS 安装成本。
- 不连接 MySQL、Nacos，不启动被审查项目。
- 不改变 DeepSeek 与 Ollama 客户端协议。
- 保留现有分层审查作为降级路径。
- 保留 Generic 和未来其他能力模块的独立性。
- 使用测试驱动的小步实施，但不进行高频 Git 提交；功能完成并由用户确认后再决定是否统一提交。

## 3. 目标文件结构

新增：

```text
codereview_agent/capabilities/springvue/
  __init__.py
  capability.py
  evidence.py
  source_utils.py
  chain_builder.py
  planner.py
  extractors/
    __init__.py
    frontend.py
    spring.py
    mybatis.py
    sql.py
    config.py

tests/
  fixtures/springvue_dataflow/
  test_springvue_extractors.py
  test_springvue_chains.py
  test_springvue_planner.py
  test_springvue_reporting.py
```

保留 `codereview_agent/capabilities/spring_vue.py` 作为兼容入口，内部只重新导出新的 `SpringVueCapability`，避免一次性破坏现有导入。

## 4. 阶段一：建立测试夹具与证据数据模型

### 4.1 测试夹具

创建最小完整项目，包含：

- `OrderPage.vue`：调用订单新增、查询和更新 API。
- `orderApi.ts`：封装 axios 请求。
- `OrderController.java`：类级路由、方法级路由和 DTO。
- `CreateOrderRequest.java`、`OrderResponse.java`。
- `OrderService.java`：查询、状态校验和多次写库。
- `OrderMapper.java`：带 `@Param` 的 Mapper 方法。
- `OrderMapper.xml`：select、insert、update 和动态 SQL。
- `application.yml`、`application-dev.yml`、`bootstrap.yml`。
- 可见的 Nacos 配置副本。

同时准备断链变体：动态 URL、XML id 不匹配、缺失 SQL、方法重载和多个 Service。

### 4.2 证据模型

在 `evidence.py` 中新增：

- `EvidenceLocation`：文件、起止行。
- `EvidenceNode`：节点 ID、类型、名称、位置和元数据。
- `EvidenceEdge`：源、目标、关系、证据、可信度和是否可作为确定事实。
- `EvidenceGraph`：节点与边的增删查、去重和邻接查询。
- `EndpointChain`：链路 ID、接口、节点、边、状态、断点、配置上下文和风险分。
- `ChainSummary`：接口、调用、完整链路、部分链路和未关联数量。

可信度只允许 `high`、`medium`、`low`；链路状态只允许 `complete`、`partial`、`needs_confirmation`。

### 4.3 共享类型兼容扩展

修改 `types.py`：

- `ProjectMap` 增加带默认值的 `analysis_summary`，保存可序列化的能力摘要。
- `ReviewTask` 增加带默认值的 `metadata`，保存链路 ID、状态和上下文片段。
- `Issue` 增加带默认值的 `chain_id`、`endpoint`、`chain_status`。

新增字段必须放在带默认值区域，确保现有构造调用继续工作。

### 4.4 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_springvue_chains -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

验收：数据模型可构造、可去重、默认字段不破坏现有 11 项测试。

## 5. 阶段二：实现分层事实提取器

### 5.1 源码切片工具

在 `source_utils.py` 中实现保守的文本结构工具：

- 注释与字符串掩码，不改变字符位置和行号。
- 花括号配对与方法体范围定位。
- 行号计算和稳定节点 ID。
- 路径、URL 和 Java 类型名称规范化。
- 只返回有明确边界的片段；配对失败返回提取失败而不是猜测。

单独测试字符串中的花括号、注释、嵌套代码块和不完整源码。

### 5.2 前端提取器

实现 `extract_frontend(files)`：

- 提取 axios、fetch 及常见 request 封装调用。
- 保存 HTTP 方法、原始 URL、规范化 URL、Query/Body 字段和响应读取路径。
- 静态字符串与可解析模板路径为高可信。
- 动态表达式保留为中/低可信候选，不强行匹配。

测试：类级前缀、模板参数、Query、Body、动态 URL 和未关联请求。

### 5.3 Spring 提取器

实现 `extract_spring(files)`：

- 组合类级和方法级路由。
- 提取 HTTP 方法、方法范围、参数来源、DTO、校验、鉴权和事务注解。
- 建立字段注入、构造器注入和常见 Lombok 构造器注入的变量到类型映射。
- 从方法体提取 `receiver.method(...)` 调用。
- 根据接收者类型和方法名建立调用候选。
- 目标唯一时为高可信；重载或类型不完整时为中可信。

测试：Controller 到 Service、Service 到 Mapper、多 Service、方法重载、循环调用和 Controller 直接 Mapper。

### 5.4 MyBatis 与 SQL 提取器

实现 `extract_mybatis(files)` 与 `extract_sql(files)`：

- 提取 Mapper 接口、全限定名、方法、参数、`@Param` 和返回类型。
- 提取 XML `namespace`、SQL 标签 id、参数类型和结果映射。
- 使用 `namespace + id` 建立高可信 Mapper-SQL 关系。
- 解析注解 SQL。
- 提取 SQL 操作类型、明确表名、字段、占位符、WHERE 和分页线索。
- 比较 Mapper 参数与 SQL 占位符，输出事实差异供模型审查。

测试：正确映射、namespace 错误、id 缺失、参数不一致、`${}`、无条件更新和动态 SQL。

### 5.5 配置提取器

实现 `extract_config(files)`：

- 提取 YAML 层级键和值的位置。
- 识别 Profile、`spring.config.import`、Nacos dataId/group/namespace。
- 识别 datasource、MyBatis mapper-locations 和环境变量占位符。
- 只记录远程 Nacos 引用，不推断远程值。

测试：本地配置、Profile 覆盖、Nacos 引用、数据源占位符和配置副本。

### 5.6 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_springvue_extractors -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

验收：所有事实均有文件和行号；不完整源码不会产生确定关系；不引入模型调用。

## 6. 阶段三：构建证据图谱与端点链路

### 6.1 图谱装配

在 `capability.py` 中按固定顺序调用提取器，并将节点、关系和失败信息汇总到 `EvidenceGraph`。

提取器异常必须局部捕获，记录：

```text
提取器名称
影响层级
错误摘要
降级结果
```

### 6.2 路由与调用匹配

在 `chain_builder.py` 中实现：

- HTTP 方法和规范化 URL 匹配前端请求与 Controller。
- Controller 方法沿高可信调用边追踪 Service、Mapper。
- Mapper 沿映射边追踪 SQL。
- SQL 沿读写边追踪表。
- 持久化节点附加配置上下文。
- 遍历深度限制、循环检测和节点去重。

中可信关系可以进入 `needs_confirmation` 链路，但必须带证据标签。低可信关系只出现在候选或未覆盖列表。

### 6.3 链路分类与风险分

实现：

- `complete`、`partial`、`needs_confirmation` 分类。
- 写请求、敏感字段、`${}`、无条件更新等确定性风险信号加权。
- 未关联后端接口与未关联前端请求统计。
- 共享 Service/Mapper 节点复用，但每个端点保持独立链路 ID。

风险分只用于排序和 Token 分配，不直接决定 Bug 等级。

### 6.4 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_springvue_chains -v
```

验收：测试夹具形成完整新增链路；动态 URL 和 XML 缺失形成部分链路；循环调用可以终止；低可信关系不进入确定链路。

## 7. 阶段四：链路任务规划与模型上下文

### 7.1 链路任务规划器

在 `springvue/planner.py` 中实现：

- 高风险写入链路逐条生成任务。
- 权限或敏感数据链路逐条生成任务。
- 结构相似的普通只读链路可小批量合并。
- 部分链路生成保守补充任务。
- 配置上下文生成一次项目级任务，避免每条链重复发送完整 YAML。
- 无可信链路时调用现有 `build_review_plan(project)` 降级。

任务 metadata 保存链路摘要、证据边、断点和方法级源码片段。

### 7.2 方法级上下文渲染

扩展 `planner.build_context`：

- 如果任务包含链路 metadata，使用链路专用渲染器。
- 只渲染相关方法、DTO 字段、Mapper 方法、SQL 和必要配置片段。
- 明确区分确定关系、中可信关系和未覆盖项。
- 不允许模型根据未提供文件推断统一鉴权、远程配置或运行状态。
- 普通 ReviewTask 继续使用现有文件级上下文。

### 7.3 Token 估算

扩展 `estimate_tokens` 读取方法级片段实际字符数。共享片段在单任务内去重；终端继续显示执行前估算。

### 7.4 模型输出协议

扩展 JSON 输出字段：

```json
{
  "chain_id": "springvue.http.001",
  "endpoint": "POST /orders",
  "chain_status": "complete"
}
```

解析时以任务 metadata 为可信来源：模型返回的链路字段缺失或不一致时，由 Agent 覆盖为当前任务链路信息。

### 7.5 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_springvue_planner -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

验收：模型上下文只包含目标链路；完整与部分链路提示词边界不同；无链路项目仍生成原有分层任务。

## 8. 阶段五：问题治理、严重复核与输出

### 8.1 问题解析与去重

修改 `review.py`：

- 将任务链路 metadata 写入 Issue。
- 去重指纹加入链路语义，但合并相同文件、行号和根因的跨链问题。
- 合并后保留受影响端点列表。
- 证据验证继续校验文件、行号和代码证据。

### 8.2 严重问题复核

扩展 `_issue_context`：

- 优先使用任务中的链路节点和关系。
- 提供相关方法片段、SQL、配置上下文和断链信息。
- 明确关系可信度。
- 无完整输入传播或危险落点时不得维持严重 Bug。

### 8.3 项目与链路摘要

修改：

- `ProjectMap.analysis_summary` 保存 SpringVue 链路统计。
- `registry._merge_projects` 按能力名称合并摘要，避免覆盖 Generic 或未来模块。
- `report.print_project_summary` 显示 HTTP 接口、前端请求、完整链路、部分链路和未关联数量。
- CLI 审查计划按链路类型显示任务数量。

### 8.4 终端与 Markdown

修改 `report.py`：

- 问题增加接口、链路状态和简化通路。
- 部分链路显示断点和未覆盖层级。
- Markdown 增加数据通路覆盖摘要和按链路问题信息。
- 保持现有下游 Coding Agent 交接说明。

### 8.5 验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_springvue_reporting -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

验收：严重复核收到完整链路；终端和 Markdown 展示链路；旧模型响应仍可解析。

## 9. 阶段六：能力集成与回归验证

### 9.1 SpringVue 能力迁移

- 新 `SpringVueCapability.analyze` 先构建现有 ProjectMap，保持原有信号。
- 再构建证据图谱和端点链路。
- 有可信链路时生成链路任务和必要补充任务。
- 无可信链路或链路构建器失败时使用现有分层计划。
- `spring_vue.py` 保留兼容导出。

### 9.2 注册中心兼容

验证：

- SpringVue 与 Generic 文件所有权不重叠。
- 混合项目仍可同时启用多个能力模块。
- SpringVue 内部失败不会阻止 Generic 审查未接管文件。
- 能力摘要合并不会引入 Spring-first 主流程。

### 9.3 完整验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q codereview_agent tests
git diff --check
```

使用真实 SpringVue 测试仓库进行一次人工冒烟测试：

1. 扫描并确认链路统计合理。
2. 抽查至少三条完整链路的每条关系。
3. 抽查至少两条部分链路没有错误补全。
4. 使用 DeepSeek Flash 执行一次审查。
5. 使用 qwen2.5:3b 执行一次审查。
6. 检查终端、Token 统计和 Markdown 输出。

实际模型测试需要用户本地已配置的模型服务，不写入自动化单元测试。

## 10. README 更新

功能验收后再更新 README：

- 将当前能力描述改为端点级全链路审查。
- 说明完整、部分和待确认链路。
- 说明 Nacos 是配置上下文，不是业务数据流节点。
- 保留 Windows/macOS 启动说明，不增加 Git 或 Ollama 部署教程。
- 不包含开发者个人路径、用户名或 API Key。

README 当前未提交改动必须人工合并，不覆盖用户已有内容。

## 11. 完成定义

满足以下条件才可宣布完成：

- 所有新增和原有自动化测试通过。
- 测试夹具可形成完整 Vue 到 SQL/表链路。
- 每条确定关系可追溯到文件和行号。
- 低可信关系不作为模型事实。
- 断链与提取器失败有可见降级说明。
- 模型上下文按链路切片，Token 估算基于实际片段。
- 严重问题使用链路复核。
- 终端和 Markdown 显示链路覆盖与断点。
- Generic、能力注册和模型配置流程无回归。
- 真实 DeepSeek 与 Ollama 冒烟测试通过，或明确记录尚未执行的外部验证。
