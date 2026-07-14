"""LLM-first review execution, evidence checks, deduplication, and critical verification."""

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .llm import ModelClient, ModelError
from .planner import build_context
from .types import Issue, ProjectMap, ReviewResult, ReviewTask, Usage


REVIEW_SYSTEM = """你是 CodeReview Agent 的受约束代码审查引擎。你只审查提供的代码和事实关系，
不能根据缺失信息猜测。每个结论必须有文件、行号或明确代码证据，以及可复现的触发/调用路径。
安全问题必须说明外部输入、处理过程和危险落点。没有足够证据时必须标为 needs_human_confirmation=true。
只输出 JSON 对象，不要使用 Markdown。每个任务最多报告 3 个证据最充分、优先级最高的问题；每个文本字段保持简洁，避免超过 80 个汉字。needs_human_confirmation 必须是 JSON 布尔值 true 或 false，不能是字符串。JSON 必须为：
{"issues":[{"category":"...","severity":"严重 Bug|中等 Bug|轻度 Bug|优化建议","title":"...","file":"...","line":1,"evidence":"...","trigger_path":"...","impact":"...","recommendation":"...","confidence":"高|中|低","needs_human_confirmation":false}]}。
例如：{"issues":[{"category":"参数校验","severity":"中等 Bug","title":"缺少请求参数校验","file":"UserController.java","line":18,"evidence":"接口参数未使用校验注解","trigger_path":"POST /users","impact":"非法参数可能进入业务层","recommendation":"为 DTO 添加校验注解并启用 @Valid","confidence":"高","needs_human_confirmation":false}]}。
不得输出没有代码依据的泛泛建议。"""

VERIFY_SYSTEM = """你是独立严重等级复核器。只根据提供的原始结论、代码证据和调用关系判定严重等级。
不要提出新问题。只输出 JSON：{"verdict":"成立|不成立|证据不足","recommended_severity":"严重 Bug|中等 Bug|轻度 Bug|优化建议|需人工确认","reason":"..."}。
“严重 Bug”必须同时证明外部可利用或必然触发，以及安全、数据破坏或服务不可用的高影响。
仅看到明文配置、重复 YAML 键、Redis KEYS、前端未做路由守卫、或“可能”造成影响时，不能仅凭该代码判为严重 Bug；应降为中等、轻度、优化建议或需人工确认。
内部接口是否真实暴露、配置是否会泄露、后端是否有统一鉴权等缺少代码证据时，必须为“证据不足”。"""


def run_review(project: ProjectMap, tasks: List[ReviewTask], client: Optional[ModelClient], output=print) -> ReviewResult:
    result = ReviewResult(project=project, tasks=tasks)
    if client is None:
        result.issues = _local_findings(project)
        result.uncovered.append("未配置大模型：仅执行辅助本地检查，未进行深度 AI 审查。")
        return result

    for index, task in enumerate(tasks, start=1):
        output("[{0}/{1}] 正在审查：{2}".format(index, len(tasks), task.domain))
        try:
            parsed = _review_task(project, task, client, result.usage, output)
            result.issues.extend(parsed)
            output("  完成：发现 {0} 个候选问题；累计 Token {1}".format(len(parsed), result.usage.total_tokens))
        except ModelError as error:
            message = "{0}：{1}".format(task.domain, error)
            result.failed_tasks.append(message)
            output("  失败：" + message)

    result.issues = _deduplicate(_validate_evidence(project, result.issues))
    _verify_critical(project, result, client, output)
    return result


def _review_task(project: ProjectMap, task: ReviewTask, client: ModelClient, usage: Usage, output=print) -> List[Issue]:
    """Retry once with stricter output limits when a provider truncates invalid JSON."""
    payload = build_context(project, task)
    for attempt in range(2):
        request = payload
        if attempt:
            request += "\n\n上一次输出不是有效 JSON。请重新审查，但最多输出 2 个最重要问题；所有字段必须是单行短句，严格输出完整 JSON。"
            output("  模型 JSON 格式无效，正在以更严格的输出限制重试一次。")
        reply = client.review(REVIEW_SYSTEM, request, 1600)
        usage.add(reply.usage)
        try:
            return _parse_issues(reply.content, task.task_id)
        except ModelError:
            if attempt:
                raise
    return []


def _parse_issues(content: str, task_id: str) -> List[Issue]:
    try:
        data = json.loads(_extract_json(content))
    except json.JSONDecodeError as error:
        raise ModelError("模型未返回有效 JSON：{0}".format(error))
    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raise ModelError("模型 JSON 中缺少 issues 数组。")
    issues: List[Issue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        severity = _normalise_severity(str(raw.get("severity", "需人工确认")))
        line = raw.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        issues.append(Issue(
            category=str(raw.get("category", "未分类")), severity=severity,
            title=str(raw.get("title", "未命名问题")), file=str(raw.get("file", "")), line=line,
            evidence=str(raw.get("evidence", "")), trigger_path=str(raw.get("trigger_path", "")),
            impact=str(raw.get("impact", "")), recommendation=str(raw.get("recommendation", "")),
            confidence=str(raw.get("confidence", "低")),
            needs_human_confirmation=_as_bool(raw.get("needs_human_confirmation", False)), task_id=task_id,
        ))
    return issues


def _verify_critical(project: ProjectMap, result: ReviewResult, client: ModelClient, output=print) -> None:
    critical = [issue for issue in result.issues if issue.severity == "严重 Bug" and not issue.needs_human_confirmation]
    for index, issue in enumerate(critical, start=1):
        output("正在二次复核严重问题 [{0}/{1}]：{2}".format(index, len(critical), issue.title))
        context = _issue_context(project, issue)
        try:
            reply = client.review(VERIFY_SYSTEM, context, 500)
            result.usage.add(reply.usage)
            data = json.loads(_extract_json(reply.content))
            verdict = str(data.get("verdict", "证据不足"))
            recommended = _normalise_severity(str(data.get("recommended_severity", "需人工确认")))
            if verdict == "成立" and recommended != "需人工确认":
                issue.severity = recommended
                issue.needs_human_confirmation = False
                issue.review_status = "二次复核成立" if recommended == "严重 Bug" else "二次复核成立，已校准为 " + recommended
            elif verdict == "不成立":
                issue.review_status = "二次复核不成立"
                issue.needs_human_confirmation = True
                issue.severity = "需人工确认"
            else:
                issue.review_status = "二次复核证据不足"
                issue.needs_human_confirmation = True
                issue.severity = "需人工确认"
        except (ModelError, json.JSONDecodeError) as error:
            issue.review_status = "二次复核失败"
            issue.needs_human_confirmation = True
            issue.severity = "需人工确认"
            output("  复核失败，已降为需人工确认：{0}".format(error))


def _issue_context(project: ProjectMap, issue: Issue) -> str:
    source = next((item for item in project.files if item.relative_path == issue.file), None)
    source_text = "未找到对应文件。"
    if source:
        lines = source.content.splitlines()
        center = max(0, (issue.line or 1) - 1)
        start, end = max(0, center - 50), min(len(lines), center + 80)
        source_text = "\n".join("{0:4d}: {1}".format(i + 1, lines[i]) for i in range(start, end))
    relations = ["{0} --{1}--> {2}".format(rel.source, rel.kind, rel.target) for rel in project.relations if rel.source == issue.file or rel.target == issue.file]
    return "初审结论：{0}\n影响：{1}\n触发路径：{2}\n证据：{3}\n文件：{4}\n代码：\n{5}\n关联关系：\n{6}".format(
        issue.title, issue.impact, issue.trigger_path, issue.evidence, issue.file, source_text, "\n".join(relations[:20]) or "无")


def _validate_evidence(project: ProjectMap, issues: Iterable[Issue]) -> List[Issue]:
    known = {source.relative_path: source for source in project.files}
    valid: List[Issue] = []
    for issue in issues:
        if not issue.file or issue.file not in known or not issue.evidence.strip():
            issue.needs_human_confirmation = True
            issue.severity = "需人工确认"
            issue.review_status = "证据不完整"
        elif issue.line is not None and (issue.line < 1 or issue.line > known[issue.file].line_count):
            issue.needs_human_confirmation = True
            issue.severity = "需人工确认"
            issue.review_status = "行号超出文件范围"
        valid.append(issue)
    return valid


def _deduplicate(issues: Iterable[Issue]) -> List[Issue]:
    selected: Dict[Tuple[str, str, int], Issue] = {}
    rank = {"严重 Bug": 4, "中等 Bug": 3, "轻度 Bug": 2, "优化建议": 1, "需人工确认": 0}
    for issue in issues:
        fingerprint = (issue.category.strip().lower(), issue.file, issue.line or 0)
        old = selected.get(fingerprint)
        if old is None or rank.get(issue.severity, 0) > rank.get(old.severity, 0):
            selected[fingerprint] = issue
    return sorted(selected.values(), key=lambda item: (-rank.get(item.severity, 0), item.file, item.line or 0))


def _local_findings(project: ProjectMap) -> List[Issue]:
    mappings = {
        "sql_string_concatenation": ("SQL 安全", "中等 Bug", "疑似拼接 SQL 字符串", "确认是否使用参数绑定或 MyBatis #{...} 替代拼接。"),
        "mybatis_dollar_placeholder": ("SQL 安全", "中等 Bug", "MyBatis ${} 字符串替换", "仅在经过白名单校验的标识符场景使用 ${}；普通参数改用 #{...}。"),
        "plaintext_secret": ("配置安全", "中等 Bug", "疑似明文敏感配置", "改用环境变量、密钥管理或 Nacos 加密配置，并轮换已暴露密钥。"),
        "vue_v_html": ("前端安全", "轻度 Bug", "Vue v-html 动态渲染", "确认内容已经过可信白名单净化；不可信 HTML 不应直接渲染。"),
        "write_without_transaction": ("事务", "轻度 Bug", "写操作缺少可见事务注解", "确认多次写库是否需要 @Transactional；单次操作不一定需要事务。"),
        "controller_direct_mapper": ("分层结构", "优化建议", "Controller 直接访问 Mapper", "将业务编排放在 Service 层，保持 Controller 的接口职责。"),
        "select_star": ("数据库性能", "优化建议", "使用 SELECT *", "明确所需字段；热点查询结合执行计划评估索引。"),
        "generic_plaintext_secret": ("通用安全", "中等 Bug", "疑似硬编码敏感值", "将敏感值移至安全配置或密钥管理，并轮换已经暴露的凭据。"),
        "generic_command_execution": ("通用安全", "轻度 Bug", "发现系统命令执行调用", "确认命令和参数均来自可信白名单；无法确认时应进行人工安全审查。"),
        "generic_large_file": ("通用结构", "优化建议", "文件规模过大", "按职责拆分文件或模块，降低维护和审查成本。"),
    }
    issues: List[Issue] = []
    for signal in project.signals:
        if signal["kind"] not in mappings:
            continue
        category, severity, title, recommendation = mappings[signal["kind"]]
        issues.append(Issue(category, severity, title, signal["file"], int(signal["line"]), signal["message"], "静态辅助检查发现", "需要结合业务上下文确认影响。", recommendation, "中", True, "辅助检查", "local"))
    return _deduplicate(issues)


def _normalise_severity(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"严重 bug", "严重", "critical", "p0"}:
        return "严重 Bug"
    if normalized in {"中等 bug", "中等", "high", "medium", "p1", "p2"}:
        return "中等 Bug"
    if normalized in {"轻度 bug", "轻度", "low", "warning", "p3"}:
        return "轻度 Bug"
    if normalized in {"优化建议", "优化", "optimization", "info"}:
        return "优化建议"
    return "需人工确认"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是"}
    return bool(value)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return text
    return text[start:end + 1]
