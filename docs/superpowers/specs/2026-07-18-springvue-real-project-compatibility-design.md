# SpringVue 真实项目链路兼容性修复设计

## 1. 背景

SpringVue 端点链路能力在最小测试夹具上可以建立：

```text
Vue/API -> Controller -> Service -> Mapper -> SQL -> 数据库表
```

但在 CSTS 真实项目中，28 个 Controller 接口全部被标记为部分链路。代码核对证明这些接口实际存在业务实现和持久层调用，主要差异是项目使用了 Service 接口与实现类、MyBatis-Plus 继承能力、多段字符串注解 SQL 和动态前端 URL。

本次修复的目标不是为 CSTS 编写路径特例，而是让 SpringVue 能力适配常见的 Spring Boot + MyBatis-Plus 工程结构，并在静态证据不足时可靠降级。

## 2. 设计原则

- Agent 只建立有源码或明确框架语义支撑的关系，不伪造运行时 SQL。
- 链路覆盖限制不属于 Bug，不进入严重、中等、轻度或优化建议列表。
- 端点链路不可信时自动回退到分层审查，避免浪费模型 Token。
- 不新增 Python 第三方依赖，保持现有 Windows 和 macOS 安装方式。
- 保留 Generic 和其他未来能力模块的独立性。
- 使用真实项目写法构造匿名测试夹具，不把用户项目源码复制进 CRA。

## 3. 轻量级 Java 语义索引

新增独立的 Java 语义索引，统一记录：

- 类、接口、文件和包名。
- `extends`、`implements` 与泛型父类。
- 方法名称、参数类型、参数数量、返回类型和实现位置。
- 字段与构造器参数的变量到类型映射。
- 接口方法与实现方法的对应关系。
- `ServiceImpl<Mapper, Entity>` 和 `BaseMapper<Entity>` 的泛型绑定。

接口方法与实现方法按方法名、参数数量和规范化参数类型匹配。唯一且签名一致时为高可信；存在重载或类型缺失时为中可信；仅名称相似时不作为模型事实。

Controller 中声明为接口类型的调用先连接到接口方法，再通过 `implements` 关系连接到实现方法。链路遍历以实现方法的源码为审查主体。

## 4. MyBatis-Plus 语义

### 4.1 显式 Mapper 调用

对于：

```java
class ProductServiceImpl extends ServiceImpl<ProductMapper, Product> {
    void load() {
        baseMapper.findDetailById(id);
    }
}
```

Agent 从泛型父类证明 `baseMapper` 的实际类型是 `ProductMapper`，再连接 Mapper 方法、注解/XML SQL 和数据库表。

### 4.2 继承 CRUD

对于 `this.save`、`getById`、`updateById`、`removeById`、`page`、`list`、`getOne` 等继承方法，生成明确标记的框架持久化节点：

```text
MyBatis-Plus INSERT Product
MyBatis-Plus SELECT Product
MyBatis-Plus UPDATE Product
```

这些节点不是仓库中的原始 SQL，不输出虚构 SQL 文本。

数据表按以下证据确定：

1. Entity 的 `@TableName`：高可信。
2. 仓库 DDL 中存在与 Entity 命名约定一致的表：中可信。
3. 只有类名转下划线约定：中可信并标为待确认。

### 4.3 多段注解 SQL

解析 `@Select`、`@Insert`、`@Update`、`@Delete` 的完整括号范围，收集其中所有 Java 字符串字面量并按顺序拼接，再提取 `<script>`、`<if>`、`<where>`、`foreach`、占位符和表名。

无法完整解析的注解只记录提取限制，不生成残缺 SQL 事实。

## 5. 前端动态 URL

支持：

```text
`/api/product/${id}`
'/api/product/' + id
request({ url: expression })
```

静态路径段保留，动态参数统一规范化为 `{}`。只有 HTTP 方法一致、静态分段一致且动态段位置兼容时，才能连接 Controller 路由。完全动态或无法确定静态前缀的 URL 不强制匹配。

## 6. 链路完整性语义

端点不再被强制要求必须到达 SQL 表。链路终点可以是：

- 显式 SQL 或数据库表。
- MyBatis-Plus 框架持久化操作。
- Feign/HTTP 远程客户端。
- 明确的本地业务终点。
- Controller 内部完成且没有未解析外部调用的返回路径。

链路状态定义：

- `complete`：端点中的可见业务调用均已解析到明确实现或终点，核心关系为高可信。
- `partial`：存在明确未解析的业务调用或持久层断点。
- `needs_confirmation`：链路主要依赖中可信的接口、表名或动态 URL 推导。

## 7. 覆盖率质量门槛与自动降级

链路构建阶段记录可见的待解析调用与成功解析结果，计算：

- 前端请求匹配率。
- Controller 业务调用解析率。
- Service 持久层调用解析率。
- 完整、部分与待确认链路比例。

当至少存在 3 个明确的待解析业务调用，且 Controller 业务调用解析率低于 50%，或同一层级出现超过 70% 的系统性断链时，SpringVue 能力自动回退到原有分层计划。

降级必须在调用模型之前显示原因。覆盖率门槛只决定审查策略，不决定 Bug 等级。

## 8. 终端与 Markdown

终端只显示覆盖摘要和本次策略：

```text
数据通路覆盖：
- HTTP 接口：28
- 完整链路：20
- 部分链路：5
- 待确认链路：3
- 前端接口匹配率：85%
- Controller 业务调用解析率：92%

审查策略：端点链路审查
```

降级时显示：

```text
数据通路覆盖不足，已自动切换为分层审查。
原因：Controller 业务调用解析率低于可信门槛。
```

终端不逐条铺开断链。Markdown 在“静态分析覆盖限制”中保留端点、断点和可信度，不与模型发现的问题混合。

## 9. 容错

- 单个文件解析失败不阻止其他文件。
- 单个接口断链不影响其他端点。
- 接口实现不唯一时使用中可信关系，不随机选择实现类。
- MyBatis-Plus 泛型无法确定时不生成 `baseMapper` 类型事实。
- 动态 URL 无静态前缀时保留为未关联请求。
- 质量门槛触发时只降级审查计划，不丢失已经提取的项目事实。

## 10. 测试方案

新增匿名测试夹具，覆盖：

- Controller 构造器注入 Service 接口。
- `ServiceImpl implements Service`。
- `ServiceImpl<Mapper, Entity>` 与 `baseMapper`。
- `save/getById/updateById/removeById/page` 等继承 CRUD。
- `@TableName` 与命名约定表名。
- 多段字符串注解 SQL 和动态 MyBatis 脚本。
- 模板字符串、字符串拼接和对象式 request URL。
- 方法重载、多个实现类和无法确定泛型。
- Controller 本地终点和 Feign 客户端终点。
- 覆盖率过低自动降级。
- 终端摘要不把断链显示为 Bug。

回归验证包括全部现有测试、Python 编译检查、`git diff --check`，以及对 CSTS 的只读链路扫描。真实模型调用不进入自动化测试。

## 11. 验收标准

- CSTS 不再出现 28 条 Controller 链路全部停在 Service 之前。
- `ProductController -> ProductServiceImpl` 可被证明。
- 显式 `baseMapper` 调用可以连接到 ProductMapper 与注解 SQL。
- MyBatis-Plus 继承 CRUD 显示为框架持久化操作，不伪造 SQL。
- 动态前端 URL 的静态部分能够与后端路由匹配。
- 链路质量不足时自动分层降级，不生成大量低质量端点任务。
- 覆盖限制与真实 Bug 在终端和 Markdown 中明确分离。
- 不增加安装依赖，不改变 DeepSeek、Ollama 和用户配置。
