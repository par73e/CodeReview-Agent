# CodeReview Agent

它先建立项目关系图和风险优先级，再将带有技术边界与代码证据要求的任务交给 DeepSeek 或本地 Ollama 审查。工具只审查代码，不会自动修改代码。

## 安装与启动

需要 Python 3.9 或更高版本。

### macOS

在 CodeReview Agent 仓库根目录执行一次：

```bash
cd <CodeReview-Agent 仓库根目录>
./run install
```

按提示确认后，程序会创建 `codereview` 快捷命令，并按需提示将其加入终端路径。完成后重新打开终端，或执行提示的 `source` 命令。

进入待审查项目目录后运行：

```bash
cd <待审查项目目录>
codereview
```

不创建快捷命令时，也可以在待审查项目目录中直接运行：

```bash
python3 <CodeReview-Agent 仓库根目录>/run_agent.py
```

### Windows

先安装 Python 3.9 或更高版本；安装时勾选 **Add Python to PATH**。在 PowerShell 中确认：

```powershell
py --version
```

Windows 无需安装 Python 包，也不使用 `./run install`。进入待审查项目目录后，直接指定 Agent 仓库中的启动文件：

```powershell
cd <待审查项目目录>
py <CodeReview-Agent 仓库根目录>\run_agent.py
```

首次启动会引导选择 DeepSeek 或 Ollama，并保存默认配置。之后每次启动会直接使用该配置；需要修改时执行：

```bash
codereview config
```

Windows 使用直接启动方式时，可在待审查项目目录执行：

```powershell
py <CodeReview-Agent 仓库根目录>\run_agent.py config
```

## 当前能力

- 引导式选择当前目录或子目录审查。
- 能力模块化组合：注册中心会匹配全部已注册的专属模块；当前 SpringVue 模块深度审查 Spring Boot + MyBatis + Nacos + Vue，Generic 模块只保守审查未被任何专属模块覆盖的代码。
- 识别 Java/Spring Boot、MyBatis/SQL、Nacos/YAML、Vue/JavaScript 文件和关键关系。
- 将高风险入口、写操作、权限、数据库、配置与前端边界拆成带上下文的模型审查任务。
- 对严重问题发起独立二次复核。
- 在终端显示详细问题；可选导出 Markdown 详细审查报告与下游 Coding Agent 交接说明。

## 隐私与限制

- DeepSeek 模式会把 Agent 选取的代码上下文发送到 DeepSeek。
- Ollama 模式仅调用本机 `http://localhost:11434`。
- 审查结论需要人工确认，尤其是标为“需人工确认”的项目。

## 能力模块

Agent 会遍历全部已注册模块并组合匹配结果，而不是先判断某一个技术栈。当前内置：

- `SpringVue`：覆盖 Java、Vue、JS/TS、SQL、MyBatis XML、Spring/Nacos YAML，提供专属关系图和审查边界。
- `Generic`：只审查没有被专属模块覆盖的常见代码文件；不对未知框架作无证据断言。

一个项目可同时启用多个专属模块：每个模块仅接管自己声明的文件；只有所有专属模块均未接管的代码才会交给 `Generic`。未来支持 Go、C++ 等技术栈时，只需新增能力模块并注册，无需改动模型调用、结果分级和报告流程。
