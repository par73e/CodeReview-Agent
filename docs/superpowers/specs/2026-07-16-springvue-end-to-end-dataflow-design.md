# SpringVue 端到端数据通路审查设计规格

## 1. 背景与目标

当前 SpringVue 能力已经能够识别 Vue/JavaScript 请求、Spring Controller 路由、Java 类之间的注入关系、MyBatis SQL 和 YAML 配置，并按接口安全、业务事务、数据库、配置、前端契约和结构性能生成分层审查任务。

现有实现属于“关系感知的分层审查”：模型可以看到目标文件及其直接关联文件，但 Agent 尚未把一个用户请求稳定地组织为 Vue、Controller、Service、Mapper、SQL、数据库表和运行配置上下文组成的完整通路。

本次升级的目标是新增端点级全链路审查能力：Agent 先证明代码关系，再以 HTTP 接口为单位构造最小审查上下文，由大模型在受约束的数据通路中发现问题。

核心原则：

> Agent 负责证明链路，大模型负责审查链路；大模型不得自行补全不存在或证据不足的关系。

## 2. 首版范围

首版只覆盖由用户操作触发的 HTTP 数据通路：

```text
Vue 页面或 API 封装
  -> HTTP 请求
  -> Spring Controller 接口
  -> Service 业务方法
  -> Mapper 方法
  -> MyBatis XML 或注解 SQL
  -> 数据库表
```

数据源、Profile、Nacos 和 MyBatis 配置作为持久化链路的运行配置上下文附加，不伪装成业务数据流中的下一个节点：

```text
Vue -> Controller -> Service -> Mapper -> SQL -> 数据库表
                                      ^
                    数据源 / Profile / Nacos 配置上下文
```

首版不包含：

- 定时任务、消息队列、事件总线和异步任务入口。
- 启动被审查项目或执行测试。
- 连接运行中的 MySQL、Nacos 或其他外部服务。
- 验证远程 Nacos 实际内容和运行时生效顺序。
- 完整 Java 编译期类型推断、反射调用和运行时生成 SQL。

这些内容无法从仓库静态证明时，必须显示为未覆盖或需人工确认。

## 3. 总体架构

SpringVue 能力内部扩展为三个阶段：

```text
分层事实提取
  -> 证据图谱
  -> 端点级链路构建与审查任务规划
```

主 Agent 的能力注册、模型客户端、JSON 解析、问题分级、严重问题二次复核、Token 统计、终端输出和 Markdown 导出继续使用统一流程。全链路逻辑只属于 SpringVue 能力，不耦合 DeepSeek 或 Ollama。

建议将当前 `capabilities/spring_vue.py` 逐步迁移为独立包：

```text
codereview_agent/capabilities/springvue/
  capability.py
  evidence.py
  chain_builder.py
  planner.py
  extractors/
    frontend.py
    spring.py
    mybatis.py
    sql.py
    config.py
```

各提取器仅输出统一事实，不直接判定 Bug。后续如引入更强的语法解析器，只替换相应提取器，不改变图谱、链路规划和模型审查流程。

## 4. 统一证据模型

### 4.1 节点

证据图谱至少支持以下节点：

- `FrontendAction`：页面事件或前端业务动作。
- `ApiCall`：HTTP 方法、URL、参数和响应使用。
- `ControllerEndpoint`：完整 Spring 路由、方法签名和请求参数。
- `JavaMethod`：Controller、Service 或其他业务方法。
- `DtoType`：请求与响应 DTO 及可见字段。
- `MapperMethod`：Mapper 接口方法、参数和返回类型。
- `SqlStatement`：MyBatis XML、注解 SQL 或 SQL 文件中的语句。
- `DatabaseTable`：SQL 明确读取或写入的表。
- `ConfigKey`：数据源、Profile、MyBatis、Nacos 等配置项。
- `ConfigSource`：本地 YAML、环境变量占位符或 Nacos 引用。

每个节点保存稳定标识、类型、显示名称、文件、行号、必要元数据和原始证据。

### 4.2 关系

图谱至少支持以下关系：

- `initiates`：前端动作发起 API 请求。
- `routes_to`：API 请求匹配 Controller 接口。
- `accepts` / `returns`：接口使用请求或响应 DTO。
- `invokes`：Java 方法调用另一个 Java 或 Mapper 方法。
- `maps_to_sql`：Mapper 方法映射到 XML 或注解 SQL。
- `reads_table` / `writes_table`：SQL 读取或写入数据库表。
- `configured_by`：持久化或框架节点依赖配置项。
- `provided_by`：配置项由本地文件、占位符或 Nacos 引用提供。

每条关系必须保存：

- 源节点与目标节点。
- 关系类型。
- 源文件、目标文件和行号。
- 匹配依据和简短证据。
- 可信度。
- 是否允许作为模型的确定事实。

### 4.3 可信度

- 高可信：明确方法调用、HTTP 方法和规范化路由一致、Mapper `namespace + id` 一致、SQL 明确包含表名。
- 中可信：类型和名称一致，但存在重载、封装或上下文缺失。
- 低可信：仅名称、目录或路径相似。

高可信关系可以作为模型确定事实。中可信关系必须标注匹配依据，并要求模型保守判断。低可信关系不进入确定链路，仅记录为候选关系或未覆盖说明。

## 5. 分层事实提取

### 5.1 前端提取

识别 Vue、JavaScript 和 TypeScript 中可见的：

- 页面事件与 API 封装函数。
- `axios`、`fetch` 及常见请求封装的 HTTP 方法和 URL。
- Query、Path、Body 参数的字段名称。
- 响应对象的字段读取路径。
- `baseURL`、路径前缀和静态模板变量。

动态拼接且无法解析的 URL 作为部分事实保留，不强行匹配 Controller。

### 5.2 Spring 提取

识别：

- 类级与方法级 `RequestMapping` 组合后的完整路由。
- HTTP 方法、Controller 方法签名和参数来源。
- DTO 类型、校验注解、鉴权相关注解和事务注解。
- 字段注入、构造器注入及常见 Lombok 构造器注入线索。
- 方法体中的 `receiver.method(...)` 调用。
- 接收者变量到 Service 或 Mapper 类型的可证明映射。

方法重载或调用目标不唯一时降低关系可信度，不根据方法名直接断言唯一调用目标。

### 5.3 MyBatis 提取

识别：

- Mapper 接口全限定名、方法名、参数、`@Param` 和返回类型。
- Mapper XML 的 `namespace`、SQL 标签 `id`、参数类型和结果映射。
- `@Select`、`@Insert`、`@Update`、`@Delete` 注解 SQL。
- Mapper 方法与 SQL 的 `namespace + id` 映射。
- SQL 占位符与 Mapper 参数的对应关系。

### 5.4 SQL 提取

识别：

- `select`、`insert`、`update`、`delete` 操作类型。
- 明确出现的数据库表和字段。
- `#{}`、`${}` 及动态 SQL 条件。
- WHERE、分页、排序和批量操作等可见结构。

Agent 不执行 SQL，也不在缺少 DDL、数据量和执行计划时断言索引或确定性能故障。

### 5.5 配置提取

识别：

- `application.yml`、`bootstrap.yml` 及 Profile 配置。
- `spring.config.import`、Nacos `dataId`、group 和 namespace 引用。
- 数据源 URL、驱动、用户名、密码占位符。
- MyBatis Mapper 扫描和 XML 路径。
- 环境变量及配置占位符引用。

远程配置不可见时只建立“引用远程配置”的事实，不推断远程值。

首版保持 Python 3.9+ 和 Windows/macOS 的低安装门槛，不强制增加第三方语法解析依赖。提取器接口应允许后续用 Tree-sitter 等解析器替换内部实现。

## 6. 端点级链路构建

每个 `ControllerEndpoint` 作为链路锚点。链路构建器执行：

1. 组合类级和方法级路由。
2. 按 HTTP 方法与规范化 URL 匹配前端请求。
3. 从 Controller 方法沿高可信 `invokes` 关系追踪 Service 和 Mapper。
4. 按 Mapper `namespace + id` 或注解映射到 SQL。
5. 从 SQL 提取读取或写入的数据库表。
6. 为持久化节点附加当前可见的数据源、Profile、MyBatis 和 Nacos 配置上下文。
7. 记录断链位置、候选关系和无法静态证明的部分。

遍历必须限制深度并检测循环，避免递归调用造成无限链路。共享 Service 或 Mapper 可以出现在多条端点链路中，但事实节点只保存一份。

链路状态：

- `complete`：从 HTTP 请求或 Controller 到 SQL/表的核心关系均有高可信证据。
- `partial`：存在可信关系，但一个或多个层级断开。
- `needs_confirmation`：主要关系依赖中可信匹配。

未匹配到前端调用的后端接口仍可形成从 Controller 开始的链路。未匹配到后端接口的前端请求单独记录为未关联请求。

## 7. 链路审查边界

每条链从以下六个维度交给大模型审查。

### 7.1 前后端接口契约

- HTTP 方法和 URL。
- Path、Query、Body 参数。
- 前端字段、DTO 字段、可空性、默认值和类型。
- 响应结构、分页字段、状态码和错误处理。

### 7.2 接口安全

- 身份认证、授权和对象所有权校验。
- 参数校验和敏感字段批量赋值。
- 外部输入到 SQL、HTML 或危险操作的传播路径。
- 内部异常和敏感信息泄露。

严重安全问题必须同时提供外部输入、传播过程、危险落点和实际影响。统一网关、过滤器或拦截器不可见时，缺少方法级鉴权只能标为需人工确认。

### 7.3 业务正确性与事务

- 多次写库的事务和回滚边界。
- 状态流转、空值、异常和返回值。
- 重复提交、先查后改和可证明的并发风险。
- Controller 绕过 Service 等结构问题。

事务结论必须结合实际写入链路，不能仅根据缺少 `@Transactional` 报告 Bug。

### 7.4 Mapper 与 SQL

- Mapper 参数和 XML 占位符。
- `namespace + id`、返回类型与结果映射。
- SQL 注入、无条件更新或删除、动态 SQL 空条件。
- 分页、循环查询、N+1 和可证明的查询风险。

### 7.5 配置上下文

- Profile、数据源、MyBatis 和 Nacos 引用的一致性。
- 占位符拼写、可见覆盖冲突和明文凭据。
- Mapper 扫描路径是否覆盖实际文件。

运行时值不可见时不得声称配置已生效或必然错误。

### 7.6 结构与性能

- 重复查询、循环访问数据库和无关字段加载。
- Controller、Service 职责过重。
- 前后端重复业务规则造成的不一致。
- 有明确收益和适用前提的分页、批量、缓存或结构优化。

## 8. 模型上下文与 Token 控制

模型仍是主要的问题发现者；静态层只负责组织事实、限制边界和提供证据。

每条链只提供：

- 相关前端请求函数。
- Controller 方法和必要类级注解。
- 相关 DTO 字段。
- 被调用的 Service 方法。
- Mapper 方法和对应 SQL。
- 必要配置片段。
- Agent 已证明的关系、可信度和断链信息。

方法级切片代替重复发送完整文件。高风险写入、权限和敏感数据链路优先逐条审查；结构相似的低风险只读链路可在不混淆证据的前提下合并。共享代码片段在同一任务中去重，执行前继续显示 Token 估算。

模型不得把低可信候选关系当成事实。输出问题必须绑定链路编号、接口、位置、证据、触发路径、影响、建议和链路可信度。

## 9. 问题治理与严重复核

问题分级继续使用严重 Bug、中等 Bug、轻度 Bug 和优化建议四档，“需人工确认”作为结论状态。

去重指纹扩展为链路、类别、文件和行号的组合；跨链路共享根因应合并，并保留受影响接口列表。

严重 Bug 继续执行独立二次复核。复核上下文从单文件附近代码升级为：

- 原始结论。
- 相关完整或部分链路。
- 问题文件和必要方法片段。
- 关系证据、可信度和断链说明。
- 严重等级判定标准。

复核无法证明时降为需人工确认。

## 10. 降级与容错

执行优先级：

```text
完整链路审查
  -> 部分链路审查
  -> 现有分层审查
```

- 单个提取器失败只影响对应层，必须记录失败原因。
- 单条链构建失败不影响其他链。
- 低可信关系不强行拼接。
- 无法形成可信链路时回退到当前接口、业务、数据库、配置等分层任务。
- 模型无效 JSON 沿用严格重试一次的机制。
- 严重复核失败时将结论降为需人工确认。
- 动态 URL、反射、运行时 SQL 和远程配置列入未覆盖范围。

任何降级都不能静默发生，终端和报告必须展示覆盖状态。

## 11. 终端与 Markdown 输出

扫描摘要增加：

```text
数据通路分析：
HTTP 接口：32
前端调用：28
完整链路：16
部分链路：11
未关联接口：5
未关联前端请求：3
```

审查计划按链路风险展示：

```text
高风险写入链路：6 条
权限与敏感数据链路：4 条
查询与分页链路：8 条
部分链路补充审查：9 条
配置上下文审查：1 项
```

每个问题展示接口、链路状态和简化数据通路。部分链路必须显示断点和未覆盖层级。

Markdown 报告增加数据通路覆盖摘要、按链路组织的问题，以及未关联接口、断链位置和运行时未验证项。报告不输出大量内部图谱实现细节。

## 12. 测试策略

建立一个最小完整的 SpringVue 测试项目，包含 Vue 页面、API 封装、Controller、DTO、Service、Mapper、Mapper XML、SQL 表、application YAML 和 Nacos 配置副本。

测试分层：

1. 事实提取测试：验证各层节点和原始证据。
2. 关系图测试：验证路由、方法调用、Mapper-SQL 和表关系。
3. 链路构建测试：验证完整、部分、待确认和循环调用链路。
4. 链路切片测试：验证模型只收到当前链所需代码。
5. 结果治理测试：验证链路绑定、去重、严重复核和降级。
6. 回归测试：验证现有分层审查、Generic 模块、DeepSeek/Ollama JSON 流程和 Markdown 输出。

关键场景：

- 完整新增或更新数据链路。
- 前后端字段和响应结构不一致。
- Mapper 参数与 XML 占位符不一致。
- 多次写库缺少有效事务。
- 动态 URL、方法重载或调用目标不唯一。
- Mapper XML 缺失或 `namespace + id` 不匹配。
- 一个 Controller 调用多个 Service。
- 多个接口共享同一 Service 或 Mapper。
- Nacos 配置副本存在但远程值不可见。
- 提取器失败后回退到分层审查。

## 13. 验收标准

- 测试项目至少形成一条完整的 Vue 到 SQL/数据库表通路。
- 每条确定关系都能追溯到文件、行号和匹配证据。
- 低可信关系不作为确定模型事实。
- 断链和单层失败不阻止其余审查。
- 模型上下文以链路为单位，不重复发送整个项目。
- 严重问题复核使用完整相关链路。
- 终端和 Markdown 显示链路状态、断点和未覆盖范围。
- 无可信链路时能够回退到原有分层审查。
- Generic 与其他能力模块不受 SpringVue 内部升级影响。
- macOS 与 Windows 的 Python 3.9+ 启动方式保持兼容。

## 14. 实施边界

本规格只定义 SpringVue HTTP 数据通路审查。首版实施不同时扩展 Go、C++ 等能力模块，也不引入自动修复、运行时探测或外部服务连接。后续能力模块可以复用统一证据图谱思想，但必须拥有各自的提取器和审查边界。
