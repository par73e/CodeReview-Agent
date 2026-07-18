# CodeReview Agent

CodeReview Agent 是一个面向真实项目目录的命令行代码审查工具。它先识别项目技术栈、代码关系和端到端数据通路，再将受约束的审查任务交给大语言模型，最终在终端输出带有代码位置、证据、影响和建议的分级结果。

它只负责审查和规划，不会自动修改被审查项目。你可以选择把 Markdown 报告交给更强的 Coding Agent，作为后续修复工作的上下文。

## 为什么不直接把代码交给大模型

直接让通用大模型审查整个仓库，容易出现上下文混乱、重复问题、脱离技术栈的推测和 Token 浪费。CodeReview Agent 在模型调用前后增加了一套确定性工作流：

1. 扫描项目并识别已注册的能力模块。
2. 建立前端请求、Controller、Service、Mapper、SQL、数据库表和配置之间的静态关系。
3. 按数据通路和风险优先级构建最小审查上下文。
4. 约束模型只能依据已提供的代码和关系提出候选问题。
5. 对候选结论执行证据校验、技术栈专项治理、分级和根因去重。
6. 对严重问题进行独立二次复核。
7. 在终端展示结果，并可选导出 Markdown 报告。

这套“审查边界”是项目的核心：模型负责理解代码，Agent 负责决定看什么、如何证明、怎样分级，以及哪些结论不能进入最终报告。

## 当前能力

- 引导式命令行交互，可审查当前目录或当前目录下的指定子目录。
- 自动识别并组合全部匹配的能力模块，不依赖单一技术栈判断。
- 端点级静态数据通路分析，而不是逐个文件孤立审查。
- 基于代码证据的问题分级：严重 Bug、中等 Bug、轻度 Bug和优化建议。
- 结论治理：拦截证据矛盾、推测性结论和常见技术误报。
- 严重问题二次复核；证据不足的结论单独列为“需人工确认”。
- 审查前显示 Token 预估，审查后显示模型实际 Token 用量。
- 终端输出完整问题，可选生成 `codereview-report.md`。
- 支持 DeepSeek API 和本地 Ollama。
- 只读审查，不修改目标项目中的代码和配置。

## 能力模块

Agent 会遍历所有已注册模块并组合匹配结果。每个专属模块只接管自己声明的文件，未被专属模块覆盖的代码才会进入 Generic 通用审查。

### SpringVue

当前主要能力模块，面向以下技术栈：

- Spring Boot
- MyBatis / MyBatis-Plus
- MySQL / SQL
- Nacos / YAML 配置
- Vue / JavaScript / TypeScript

SpringVue 模块会尝试建立：

```text
前端操作 → API 请求 → Controller → Service → Mapper → SQL → 数据库表
```

并将远程调用、DTO、框架继承 CRUD 和经过脱敏的配置事实作为补充上下文。这里的“完整链路”表示静态代码关系可解析，不代表已经经过运行时链路追踪。

### Generic

Generic 用于审查没有被专属模块覆盖的常见代码文件。它只依据可验证的通用事实检查安全、异常处理、资源生命周期、复杂度和重复逻辑，不会猜测未知框架的运行行为。

后续增加 Go、C++ 等能力时，只需实现并注册新的能力模块，不需要改动模型调用、问题分级和报告主流程。

## 运行环境

- Python 3.9 或更高版本
- DeepSeek API Key，或已经运行的本地 Ollama 服务
- 支持 macOS 和 Windows

项目使用 Python 标准库完成核心流程，直接运行源码时不需要额外安装第三方 Python 包。

## macOS 使用方法

### 1. 创建全局快捷命令

进入 CodeReview Agent 仓库根目录，执行一次：

```bash
./run install
```

按照提示创建 `codereview` 快捷命令并配置终端 PATH。完成后重新打开终端，或执行程序提示的 `source` 命令。

### 2. 配置模型

首次运行会自动进入配置向导，也可以主动执行：

```bash
codereview config
```

### 3. 审查项目

进入任意待审查项目目录：

```bash
cd <待审查项目目录>
codereview
```

如果不创建快捷命令，也可以在目标项目目录直接运行：

```bash
python3 <CodeReview-Agent仓库根目录>/run_agent.py
```

## Windows 使用方法

Windows 统一使用 `python` 命令，不需要执行 macOS 的 `./run install`。

### 1. 检查 Python

在 PowerShell 中执行：

```powershell
python --version
```

应显示 Python 3.9 或更高版本。

### 2. 配置模型

进入 CodeReview Agent 仓库根目录：

```powershell
cd <CodeReview-Agent仓库根目录>
python run_agent.py config
```

### 3. 审查项目

进入待审查项目目录，并指定 Agent 的启动文件：

```powershell
cd <待审查项目目录>
python <CodeReview-Agent仓库根目录>\run_agent.py
```

例如，Agent 位于 `D:\Project\CodeReview-Agent` 时：

```powershell
cd D:\Project\YourProject
python D:\Project\CodeReview-Agent\run_agent.py
```

需要切换模型或修改 API Key 时执行：

```powershell
python D:\Project\CodeReview-Agent\run_agent.py config
```

## 模型配置

首次配置可以选择：

- DeepSeek API：适合使用云端模型进行较深入的审查。
- 本地 Ollama：代码上下文仅发送给本机 Ollama 服务，适合敏感代码。

当前提供的默认模型：

- DeepSeek：`deepseek-v4-flash`
- Ollama：`qwen2.5:3b`

配置会保存在当前操作系统用户目录下：

- macOS：`~/.codereview/config.json`
- Windows：`%USERPROFILE%\.codereview\config.json`

配置文件位于仓库之外，不会因为提交 CodeReview Agent 或被审查项目而上传到 GitHub。程序在 macOS 上会把配置文件权限限制为仅当前用户可读写；仍请妥善保管 API Key，不要把配置内容复制到公开文件中。

## 一次审查会发生什么

启动后按照菜单选择当前目录或子目录。Agent 会依次显示：

1. 识别到的技术栈和文件统计。
2. 静态数据通路覆盖与解析率。
3. 本次匹配的能力模块。
4. 按风险划分的审查计划。
5. 模型调用前的 Token 预估。
6. 每个任务的审查进度。
7. 分级后的最终问题。
8. 是否导出 Markdown 报告。

报告默认写入被审查目录：

```text
codereview-report.md
```

该文件包含项目摘要、静态数据通路覆盖、问题证据、受影响接口、处理建议和下游 Coding Agent 交接说明。

## 问题分级

| 等级 | 判定边界 |
| --- | --- |
| 严重 Bug | 有明确代码证据，可能造成可利用的安全漏洞、敏感数据泄露、数据破坏或核心服务不可用；必须经过独立二次复核 |
| 中等 Bug | 在常见使用场景下可能导致功能异常、错误结果、明显性能下降、数据不一致或安全边界缺失 |
| 轻度 Bug | 边界条件、低概率异常、提示不准确、规范性缺陷或影响较小的潜在风险 |
| 优化建议 | 当前不一定会出错，但可以改善性能、结构、可读性、可维护性或资源使用 |

“需人工确认”不是第五个严重等级，而是证据状态。它表示静态代码不足以证明部署拓扑、远程配置或运行时行为，不能直接交给下游 Agent 修改。

## 隐私边界

### DeepSeek

Agent 会把经过筛选的代码片段、静态关系和脱敏配置事实发送至配置的 DeepSeek API 地址。它不会主动上传整个 Git 仓库，但发送内容仍可能包含业务代码，请根据项目保密要求决定是否使用。

### Ollama

Agent 只调用配置的本地 Ollama 地址，默认是 `http://localhost:11434`。只要没有把地址修改为远程服务，代码上下文不会离开本机。

### 配置与报告

- API Key 保存在仓库外的用户配置目录。
- 常见密码、Token 和密钥值进入模型上下文前会被脱敏。
- `codereview-report.md` 已加入本仓库的 `.gitignore`；但报告生成在其他项目中时，是否上传取决于目标项目自己的 Git 配置。

## 项目结构

```text
codereview_agent/
├── capabilities/       # 能力模块、匹配注册中心与 SpringVue 数据通路分析
├── cli.py              # 引导式命令行入口
├── config.py           # DeepSeek / Ollama 用户配置
├── llm.py              # 模型提供方适配
├── planner.py          # 审查计划、上下文和 Token 预估
├── project_map.py      # 通用项目结构与关系提取
├── review.py           # 模型审查、证据治理、去重和严重问题复核
├── scanner.py          # 文件扫描与目录过滤
├── report.py           # 终端结果和 Markdown 报告
└── types.py            # 核心数据结构

tests/
├── fixtures/           # 最小化跨层测试工程
└── test_*.py           # 扫描、链路、规划、治理和报告测试
```

## 开发与测试

在仓库根目录运行全部测试：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Windows PowerShell：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

当前测试覆盖能力模块匹配、SpringVue 跨层关系提取、端点链路构建、真实项目兼容、模型 JSON 处理、问题治理、严重问题复核和报告输出。

## 已知限制

- 当前分析基于静态代码，无法替代集成测试、渗透测试、数据库执行计划或运行时链路追踪。
- 模型仍可能产生误报或漏报；“需人工确认”项目必须结合真实部署环境判断。
- 本地小参数模型的代码理解和结构化输出能力通常弱于云端模型。
- 动态生成路由、反射调用、运行时代理和不可见的远程 Nacos 配置可能形成静态分析断点。
- 当前主要深度能力集中在 Spring Boot + MyBatis + MySQL + Nacos + Vue。
- Agent 不会修改代码，也不会自动执行报告中的修复建议。

## 后续方向

- 继续完善结论真实性校验和跨接口根因合并。
- 增加 Go、C++ 等专属能力模块。
- 支持针对 Git 变更范围的增量审查。
- 改进本地小模型的上下文压缩与结构化输出稳定性。
