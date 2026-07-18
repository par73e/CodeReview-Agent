"""LLM-first review execution, evidence checks, deduplication, and critical verification."""

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .llm import ModelClient, ModelError
from .planner import build_context
from .types import Issue, ProjectMap, ReviewResult, ReviewTask, Usage


REVIEW_SYSTEM = """你是 CodeReview Agent 的受约束代码审查引擎。你只审查提供的代码和事实关系，
不能根据缺失信息猜测。每个结论必须有文件、准确行号、可在代码中核对的直接证据，以及可复现的触发/调用路径。
安全问题必须同时说明外部输入、处理过程和危险落点。没有足够证据时不得报告为确定问题。
issues 数组允许为空；没有可靠问题时必须返回 {"issues":[]}。禁止为了凑数量输出假设、最佳实践或重复根因。
不得把“若存在”“可能在其他位置”“未展示完整代码”“当前代码安全”等推测写成问题；不得仅凭缺少注解认定鉴权、事务或并发缺陷。
MyBatis #{} 是参数绑定，不能报告为 SQL 注入；单次数据库写入缺少 @Transactional 不构成事务 Bug；身份请求头必须结合网关认证和服务暴露边界判断。
只输出 JSON 对象，不要使用 Markdown。每个任务报告 0 到 3 个证据最充分、优先级最高的问题；同一根因只报告一次。每个文本字段保持简洁，避免超过 80 个汉字。needs_human_confirmation 必须是 JSON 布尔值 true 或 false，不能是字符串。JSON 必须为：
{"issues":[{"category":"...","severity":"严重 Bug|中等 Bug|轻度 Bug|优化建议","title":"...","file":"...","line":1,"evidence":"...","trigger_path":"...","impact":"...","recommendation":"...","confidence":"高|中|低","needs_human_confirmation":false}]}。
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
    result.issues = _deduplicate(result.issues)
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
            issues = _parse_issues(reply.content, task.task_id)
            _bind_task_metadata(issues, task)
            return issues
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


def _bind_task_metadata(issues: Iterable[Issue], task: ReviewTask) -> None:
    """Bind findings to the Agent-proven chain instead of trusting model fields."""
    if task.metadata.get("kind") != "endpoint_chain":
        return
    chain_id = str(task.metadata.get("chain_id", ""))
    endpoint = str(task.metadata.get("endpoint", ""))
    chain_status = str(task.metadata.get("chain_status", ""))
    chain_path = str(task.metadata.get("chain_path", ""))
    raw_gaps = task.metadata.get("chain_gaps", [])
    chain_gaps = [str(item) for item in raw_gaps] if isinstance(raw_gaps, list) else []
    for issue in issues:
        issue.chain_id = chain_id
        issue.endpoint = endpoint
        issue.chain_status = chain_status
        issue.chain_path = chain_path
        issue.chain_gaps = list(chain_gaps)
        issue.affected_endpoints = [endpoint] if endpoint else []


def _verify_critical(project: ProjectMap, result: ReviewResult, client: ModelClient, output=print) -> None:
    critical = [issue for issue in result.issues if issue.severity == "严重 Bug"]
    tasks_by_id = {task.task_id: task for task in result.tasks}
    rejected_ids = set()
    for index, issue in enumerate(critical, start=1):
        output("正在二次复核严重问题 [{0}/{1}]：{2}".format(index, len(critical), issue.title))
        context = _issue_context(project, issue, tasks_by_id.get(issue.task_id))
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
                rejected_ids.add(id(issue))
            else:
                issue.review_status = "二次复核证据不足"
                issue.needs_human_confirmation = True
                issue.severity = "需人工确认"
        except (ModelError, json.JSONDecodeError) as error:
            issue.review_status = "二次复核失败"
            issue.needs_human_confirmation = True
            issue.severity = "需人工确认"
            output("  复核失败，已降为需人工确认：{0}".format(error))
    if rejected_ids:
        result.issues = [issue for issue in result.issues if id(issue) not in rejected_ids]
    for issue in result.issues:
        if issue.severity == "严重 Bug" and issue.review_status != "二次复核成立":
            _mark_manual(issue, "严重等级未通过独立二次复核")


def _issue_context(project: ProjectMap, issue: Issue, task: Optional[ReviewTask] = None) -> str:
    task_kind = task.metadata.get("kind") if task is not None else ""
    if task is not None and task_kind in {"endpoint_chain", "springvue_config"}:
        task_context = build_context(project, task)
        boundary = "链路断点不得自行补全" if task_kind == "endpoint_chain" else "被脱敏值和远程配置不得自行推断"
        return (
            "初审结论：{0}\n影响：{1}\n触发路径：{2}\n证据：{3}\n"
            "以下为 Agent 针对该任务构建的审查上下文。{4}：\n\n{5}"
        ).format(issue.title, issue.impact, issue.trigger_path, issue.evidence, boundary, task_context)
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
            _mark_manual(issue, "证据不完整")
        elif issue.line is not None and (issue.line < 1 or issue.line > known[issue.file].line_count):
            _mark_manual(issue, "行号超出文件范围")
        else:
            decision, reason = _govern_issue(project, issue, known[issue.file])
            if decision == "reject":
                continue
            if decision == "manual" or issue.needs_human_confirmation or issue.severity == "需人工确认":
                _mark_manual(issue, reason or "模型标记为需要人工确认")
            else:
                issue.review_status = "证据校验通过"
        valid.append(issue)
    return valid


def _govern_issue(project: ProjectMap, issue: Issue, source) -> Tuple[str, str]:
    """Apply precision-first, stack-aware policies before a model claim reaches users."""
    combined = " ".join([issue.category, issue.title, issue.evidence, issue.trigger_path, issue.impact, issue.recommendation])
    lowered = combined.lower()

    if _contains_any(combined, (
        "当前代码正确", "当前代码为 #{}", "当前代码使用 #{}", "当前片段安全", "实际安全",
        "无直接证据", "未展示完整", "未显示完整", "根据上下文推断", "若存在 ${}", "如果存在 ${}",
    )):
        return "reject", "候选结论与其自身证据矛盾"

    if _is_sql_injection_claim(lowered) and not _has_sql_injection_sink(source.content):
        return "reject", "对应源码中未发现 SQL 字符串替换或输入拼接落点"

    if ("hashmap" in lowered or "线程安全" in combined) and "ConcurrentHashMap" in source.content:
        return "reject", "源码使用 ConcurrentHashMap，与候选结论矛盾"

    if _contains_any(combined, ("未设置过期", "没有过期", "无过期清理", "永久驻留", "没有清理机制")):
        if _has_expiry_or_cleanup(source.content):
            return "reject", "源码中存在清理或过期机制"

    if _claims_missing_required_header(combined) and _required_request_header_at(source.content, issue.line):
        return "reject", "@RequestHeader 默认要求请求头存在"

    if _is_single_write_transaction_claim(combined, source.content, issue.line):
        return "reject", "单次数据库写入缺少事务注解不构成事务缺陷"

    if _contains_any(combined, ("多个服务共享同一数据库", "共享同一数据库连接")):
        issue.severity = "优化建议"

    if _contains_any(combined, ("直接返回实体", "价格、库存等", "价格、库存")):
        if not _contains_any(source.content.lower(), ("password", "secret", "token", "credential")):
            return "reject", "当前证据未指出实体中存在凭据或高敏感字段"

    if _contains_any(combined, ("返回了不应暴露的字段", "返回未使用字段", "前端仅使用")):
        if not _contains_any(combined.lower(), ("password", "secret", "token", "credential", "身份证", "手机号")):
            issue.severity = "优化建议"

    if _contains_any(combined, ("路径参数未校验", "缺少路径参数校验", "未使用 @Min", "未校验范围")) or (
        "@PathVariable" in combined and "校验" in combined
    ):
        if issue.severity in {"严重 Bug", "中等 Bug"}:
            issue.severity = "轻度 Bug"

    if _is_unhandled_runtime_claim(combined):
        if _has_global_exception_handler(project, issue.file):
            return "reject", "对应服务存在全局异常处理器，候选结论所称的未捕获或堆栈泄露不成立"
        if issue.severity in {"严重 Bug", "中等 Bug"}:
            issue.severity = "轻度 Bug"

    if _is_generic_lost_update_claim(combined) and not _contains_any(combined, ("重复下单", "重复订单", "唯一约束", "唯一性", "幂等")):
        issue.severity = "优化建议"

    if _is_identity_header_claim(combined) and _has_gateway_identity_boundary(project):
        issue.title = "身份请求头的服务边界需要确认"
        return "manual", "已发现 JWT 网关身份注入，但无法仅凭源码确认服务是否可绕过网关访问"

    if _contains_any(combined, ("内部接口未做鉴权", "内部接口没有鉴权", "internal 但无")) and _has_gateway_identity_boundary(project):
        issue.title = "内部接口的访问边界需要确认"
        return "manual", "已发现 JWT 网关保护，但仍需确认内部接口的路由意图和服务端口暴露范围"

    if _contains_any(combined, ("日志记录用户ID和用户名", "日志中明文存储用户ID和用户名")):
        return "reject", "用户标识属于常规审计字段，当前证据未显示凭据或高敏感数据泄露"

    if issue.severity == "严重 Bug":
        if issue.confidence != "高":
            return "manual", "严重等级缺少高置信度直接证据"
        if issue.chain_status in {"partial", "needs_confirmation"}:
            return "manual", "严重等级所在数据通路尚未完整解析"
        if _contains_any(combined, ("需确认", "若未", "若存在", "如果存在", "可能在其他", "风险较低")):
            return "manual", "严重等级仍依赖未证明的前提"

    if _contains_any(issue.evidence, ("需确认", "未展示", "未显示", "根据上下文推断", "无直接证据")):
        return "manual", "证据包含尚未验证的推断"
    return "keep", ""


def _mark_manual(issue: Issue, reason: str) -> None:
    issue.needs_human_confirmation = True
    issue.severity = "需人工确认"
    issue.review_status = "需人工确认：" + reason


def _contains_any(text: str, values: Iterable[str]) -> bool:
    return any(value in text for value in values)


def _is_sql_injection_claim(lowered: str) -> bool:
    return "sql 注入" in lowered or "sql注入" in lowered or "sql injection" in lowered


def _has_sql_injection_sink(content: str) -> bool:
    if "${" in content:
        return True
    return bool(re.search(
        r"(?is)\b(select|update|delete|insert)\b.{0,240}?[\"']\s*\+\s*(?![\"'])[A-Za-z_(]",
        content,
    ))


def _has_expiry_or_cleanup(content: str) -> bool:
    lowered = content.lower()
    explicit_ttl = _contains_any(lowered, ("expireafter", "time-to-live", "ttl", "setex", "expire("))
    scheduled_clear = (".clear(" in content or ".remove(" in content) and _contains_any(
        lowered, ("thread.sleep", "@scheduled", "timer", "scheduleatfixedrate"),
    )
    return explicit_ttl or scheduled_clear


def _claims_missing_required_header(text: str) -> bool:
    return "requestheader" in text.lower() and _contains_any(text, (
        "缺失则", "缺失时", "可能为 null", "未做非空", "required 属性", "@RequestParam(required",
    ))


def _required_request_header_at(content: str, line: Optional[int]) -> bool:
    lines = content.splitlines()
    center = max(0, (line or 1) - 1)
    window = "\n".join(lines[max(0, center - 4):min(len(lines), center + 5)])
    return "@RequestHeader" in window and "required = false" not in window and "required=false" not in window


def _is_single_write_transaction_claim(text: str, content: str, line: Optional[int]) -> bool:
    if not _contains_any(text, ("缺少事务", "未声明事务", "无事务保障", "未使用事务")):
        return False
    if _contains_any(text, ("当前仅一个", "当前仅单", "未来扩展", "若后续", "以后增加")):
        return True
    method = _method_window(content, line)
    writes = re.findall(
        r"\.(?:save|saveBatch|insert|update|updateById|remove|removeById|delete|deleteById)\s*\(",
        method,
    )
    return len(writes) <= 1


def _method_window(content: str, line: Optional[int]) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    center = max(0, min(len(lines) - 1, (line or 1) - 1))
    start = center
    while start > 0 and not re.search(r"\b(public|protected|private)\b.*\([^;]*\)", lines[start]):
        start -= 1
    depth = 0
    opened = False
    end = min(len(lines), start + 120)
    for index in range(start, end):
        depth += lines[index].count("{") - lines[index].count("}")
        opened = opened or "{" in lines[index]
        if opened and depth <= 0:
            end = index + 1
            break
    return "\n".join(lines[start:end])


def _is_unhandled_runtime_claim(text: str) -> bool:
    return "runtimeexception" in text.lower() and _contains_any(text, ("500", "异常处理", "泄露堆栈", "友好错误"))


def _is_generic_lost_update_claim(text: str) -> bool:
    return _contains_any(text, ("并发覆盖", "竞态条件", "乐观锁", "状态覆盖"))


def _is_identity_header_claim(text: str) -> bool:
    lowered = text.lower()
    mentions_header = "x-user-role" in lowered or "x-user-id" in lowered or "身份请求头" in text
    return mentions_header and _contains_any(text, ("伪造", "篡改", "绕过", "可信", "鉴权", "未校验", "任意", "当前登录", "服务边界"))


def _has_gateway_identity_boundary(project: ProjectMap) -> bool:
    gateway_text = "\n".join(
        source.content for source in project.files
        if source.language == "java" and ("gateway" in source.relative_path.lower() or "GlobalFilter" in source.content)
    )
    return (
        "GlobalFilter" in gateway_text
        and ("validate(" in gateway_text or "parseToken(" in gateway_text)
        and ("X-User-Role" in gateway_text or "X-User-Id" in gateway_text)
    )


def _has_global_exception_handler(project: ProjectMap, issue_file: str) -> bool:
    scope = _module_scope(issue_file)
    return any(
        _module_scope(source.relative_path) == scope
        and "@RestControllerAdvice" in source.content
        and "@ExceptionHandler" in source.content
        for source in project.files
        if source.language == "java"
    )


def _deduplicate(issues: Iterable[Issue]) -> List[Issue]:
    selected: Dict[Tuple[str, str, str, int], Issue] = {}
    rank = {"严重 Bug": 4, "中等 Bug": 3, "轻度 Bug": 2, "优化建议": 1, "需人工确认": 0}
    for issue in issues:
        fingerprint = _root_cause_key(issue)
        old = selected.get(fingerprint)
        if old is None:
            selected[fingerprint] = issue
            continue
        endpoints = list(dict.fromkeys(old.affected_endpoints + issue.affected_endpoints))
        if rank.get(issue.severity, 0) > rank.get(old.severity, 0):
            issue.affected_endpoints = endpoints
            selected[fingerprint] = issue
        else:
            old.affected_endpoints = endpoints
    return sorted(selected.values(), key=lambda item: (-rank.get(item.severity, 0), item.file, item.line or 0))


def _root_cause_key(issue: Issue) -> Tuple[str, str, str, int]:
    combined = " ".join([issue.category, issue.title, issue.evidence]).lower()
    if _is_identity_header_claim(combined):
        return ("root", "trusted_identity_header", "", 0)
    if _is_unhandled_runtime_claim(combined):
        return ("root", "unhandled_business_exception", _module_scope(issue.file), 0)
    if _contains_any(combined, ("分页参数未做上限", "大分页", "分页上限")):
        return ("root", "unbounded_page_size", _module_scope(issue.file), 0)
    if _contains_any(combined, ("catch 块未处理", "catch块未处理", "异常处理过于宽泛", "未处理 http 错误")):
        return ("root", "silent_frontend_error", _module_scope(issue.file), 0)
    if _contains_any(combined, ("路径参数未校验", "缺少路径参数校验", "未校验范围", "@pathvariable")):
        return ("root", "path_parameter_validation", _module_scope(issue.file), 0)
    if _contains_any(combined, ("缺少请求参数校验", "dto 未使用 @valid", "dto字段无校验", "dto 字段无校验")):
        return ("root", "request_dto_validation", issue.file, 0)
    if _is_generic_lost_update_claim(combined) and not _contains_any(combined, ("重复下单", "重复订单", "唯一约束", "唯一性", "幂等")):
        return ("root", "state_update_concurrency", _module_scope(issue.file), 0)
    if _contains_any(combined, ("缺少事务", "未声明事务", "未使用事务", "未在同一事务")):
        return ("root", "multi_write_transaction", issue.file, 0)
    return (
        issue.category.strip().lower(),
        issue.title.strip().lower(),
        issue.file,
        issue.line or 0,
    )


def _module_scope(path: str) -> str:
    marker = "/src/"
    return path.split(marker, 1)[0] if marker in path else path.split("/", 1)[0]


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
