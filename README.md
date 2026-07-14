# CodeReview Agent

面向 Spring Boot + MySQL + Nacos + Vue 课程设计项目的引导式命令行代码审查 Agent。

它先建立项目关系图和风险优先级，再将带有技术边界与代码证据要求的任务交给 DeepSeek 或本地 Ollama 审查。工具只审查代码，不会自动修改代码。

## 安装与启动

需要 Python 3.9 或更高版本。

```bash
cd /Users/louis/上电/Project/CRA
python3 -m pip install --user .

cd /path/to/your/project
"$(python3 -m site --user-base)/bin/codereview"
```

如果希望以后直接输入 `codereview`，将用户 Python 命令目录加入 zsh 路径后重开终端：

```bash
echo 'export PATH="$(python3 -m site --user-base)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

不想安装也可以在待审查项目目录中直接运行：

```bash
python3 /Users/louis/上电/Project/CRA/run_agent.py
```

首次启动会引导选择 DeepSeek、Ollama 或辅助本地检查，并将默认配置保存到 `~/.codereview/config.json`。之后执行 `codereview` 会直接使用该配置；需要修改时执行：

```bash
codereview config
```

## 当前能力

- 引导式选择当前目录或子目录审查。
- 识别 Java/Spring Boot、MyBatis/SQL、Nacos/YAML、Vue/JavaScript 文件和关键关系。
- 将高风险入口、写操作、权限、数据库、配置与前端边界拆成带上下文的模型审查任务。
- 对严重问题发起独立二次复核。
- 在终端显示详细问题；可选导出 Markdown 详细审查报告与下游 Coding Agent 交接说明。

## 隐私与限制

- DeepSeek 模式会把 Agent 选取的代码上下文发送到 DeepSeek。
- Ollama 模式仅调用本机 `http://localhost:11434`。
- 无模型模式只执行少量确定性辅助检查，不能代替深度 AI 审查。
- 审查结论需要人工确认，尤其是标为“需人工确认”的项目。
