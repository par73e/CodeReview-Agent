"""Terminal rendering and optional Markdown artifact generation."""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .types import Issue, ProjectMap, ReviewResult


ORDER = ["严重 Bug", "中等 Bug", "轻度 Bug", "优化建议", "需人工确认"]
ICONS = {"严重 Bug": "[严重]", "中等 Bug": "[中等]", "轻度 Bug": "[轻度]", "优化建议": "[优化]", "需人工确认": "[人工确认]"}
CHAIN_STATUS = {"complete": "完整", "partial": "部分", "needs_confirmation": "待确认"}


def print_project_summary(project: ProjectMap) -> None:
    counts: Dict[str, int] = {}
    for source in project.files:
        counts[source.language] = counts.get(source.language, 0) + 1
    print("\n扫描完成：" + str(project.root))
    print("识别技术栈：" + "、".join(project.technologies))
    print("文件统计：" + "，".join("{0} {1}".format(key, value) for key, value in sorted(counts.items())))
    print("关系线索：路由 {0}，前端 API {1}，跨文件关系 {2}".format(len(project.routes), len(project.api_calls), len(project.relations)))
    springvue = _springvue_summary(project)
    if springvue:
        dataflow = springvue.get("dataflow", {})
        if isinstance(dataflow, dict):
            print("数据通路分析：HTTP 接口 {0}，前端请求 {1}，静态完整链路 {2}，部分链路 {3}，待确认链路 {4}".format(
                dataflow.get("endpoint_count", 0), dataflow.get("frontend_call_count", 0),
                dataflow.get("complete_count", 0), dataflow.get("partial_count", 0),
                dataflow.get("needs_confirmation_count", 0),
            ))
            print("调用入口补充：无前端直接调用的后端接口 {0}，未匹配后端路由的前端请求 {1}".format(
                dataflow.get("unmatched_endpoint_count", 0), dataflow.get("unmatched_frontend_count", 0),
            ))
            print("链路解析率：前端接口 {0}，Controller 业务调用 {1}，Service 持久层调用 {2}".format(
                _percentage(dataflow.get("frontend_match_rate", 1.0)),
                _percentage(dataflow.get("controller_business_resolution_rate", 1.0)),
                _percentage(dataflow.get("service_persistence_resolution_rate", 1.0)),
            ))
        strategy = springvue.get("review_strategy", "endpoint_chain")
        print("审查策略：" + ("端点链路审查" if strategy == "endpoint_chain" else "分层降级审查"))
        fallback_reasons = springvue.get("fallback_reasons", [])
        if strategy == "layered_fallback" and isinstance(fallback_reasons, list):
            print("数据通路覆盖不足，已自动切换为分层审查。")
            for reason in fallback_reasons:
                print("- 原因：" + str(reason))
        failures = springvue.get("extractor_failures", [])
        if isinstance(failures, list) and failures:
            print("提取降级：")
            for failure in failures:
                print("- " + str(failure))


def print_result(result: ReviewResult) -> None:
    print("\n" + "=" * 66)
    print("CodeReview Agent 审查结果")
    print("=" * 66)
    if result.usage.total_tokens:
        print("实际模型 Token：输入 {0}，输出 {1}，合计 {2}".format(result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens))
    for severity in ORDER:
        issues = [issue for issue in result.issues if issue.severity == severity]
        if not issues:
            continue
        print("\n{0} {1}（{2}）".format(ICONS[severity], severity, len(issues)))
        for index, issue in enumerate(issues, start=1):
            _print_issue(index, issue)
    if not result.issues:
        print("\n未发现可报告的问题。请注意：这不表示项目不存在缺陷。")
    if result.failed_tasks:
        print("\n未完成的审查任务：")
        for item in result.failed_tasks:
            print("- " + item)
    if result.uncovered:
        print("\n未覆盖范围：")
        for item in result.uncovered:
            print("- " + item)
    print("\n" + "=" * 66)


def _print_issue(index: int, issue: Issue) -> None:
    position = issue.file + (":" + str(issue.line) if issue.line else "")
    print("\n{0}. {1}".format(index, issue.title))
    print("   位置：{0} | 类别：{1} | 置信度：{2} | 状态：{3}".format(position, issue.category, issue.confidence, issue.review_status))
    if issue.endpoint:
        print("   接口：{0} | 链路：{1}（{2}）".format(
            issue.endpoint, issue.chain_id or "未编号", CHAIN_STATUS.get(issue.chain_status, issue.chain_status or "未知"),
        ))
    if issue.chain_path:
        print("   通路：" + issue.chain_path)
    if issue.chain_gaps:
        print("   断点：" + "、".join(issue.chain_gaps))
    if len(issue.affected_endpoints) > 1:
        print("   受影响接口：" + "、".join(issue.affected_endpoints))
    print("   证据：" + issue.evidence)
    print("   路径：" + issue.trigger_path)
    print("   影响：" + issue.impact)
    print("   建议：" + issue.recommendation)


def write_markdown(result: ReviewResult, output_dir: Path) -> Path:
    destination = output_dir / "codereview-report.md"
    lines: List[str] = ["# CodeReview Agent 审查报告", "", "生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", "## 项目摘要", "", "- 路径：`{0}`".format(result.project.root), "- 技术栈：" + "、".join(result.project.technologies), "- 审查任务数：{0}".format(len(result.tasks)), "- 实际 Token：输入 {0}，输出 {1}，合计 {2}".format(result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens)]
    _append_dataflow_markdown(lines, result.project)
    for severity in ORDER:
        issues = [issue for issue in result.issues if issue.severity == severity]
        if not issues:
            continue
        lines.extend(["", "## {0}".format(severity)])
        for index, issue in enumerate(issues, start=1):
            lines.extend(["", "### {0}. {1}".format(index, issue.title), "", "- 类别：{0}".format(issue.category), "- 位置：`{0}{1}`".format(issue.file, ":" + str(issue.line) if issue.line else ""), "- 复核状态：{0}".format(issue.review_status), "- 置信度：{0}".format(issue.confidence)])
            if issue.endpoint:
                lines.append("- 接口：`{0}`".format(issue.endpoint))
                lines.append("- 链路：{0}（{1}）".format(issue.chain_id or "未编号", CHAIN_STATUS.get(issue.chain_status, issue.chain_status or "未知")))
            if issue.chain_path:
                lines.append("- 数据通路：{0}".format(issue.chain_path))
            if issue.chain_gaps:
                lines.append("- 链路断点：{0}".format("、".join(issue.chain_gaps)))
            if len(issue.affected_endpoints) > 1:
                lines.append("- 受影响接口：{0}".format("、".join(issue.affected_endpoints)))
            lines.extend(["- 证据：{0}".format(issue.evidence), "- 触发路径：{0}".format(issue.trigger_path), "- 影响：{0}".format(issue.impact), "- 修复建议：{0}".format(issue.recommendation)])
    if result.failed_tasks or result.uncovered:
        lines.extend(["", "## 未覆盖范围与限制"])
        lines.extend(["- " + item for item in result.failed_tasks + result.uncovered])
    lines.extend(["", "## 下游 Coding Agent 修复交接说明", "", "请按以下原则处理本报告：", "", "1. 先修复严重 Bug，再修复中等 Bug；标为需人工确认的问题必须先验证。", "2. 修改仅限于问题关联文件及必要依赖，避免无关重构。", "3. 每项修复后补充或执行相应的单元、接口或前端验证。", "4. 重新运行 CodeReview Agent，确认原问题不再出现且没有引入新问题。"])
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def _springvue_summary(project: ProjectMap) -> Optional[Dict[str, object]]:
    value = project.analysis_summary.get("SpringVue")
    return value if isinstance(value, dict) else None


def _append_dataflow_markdown(lines: List[str], project: ProjectMap) -> None:
    springvue = _springvue_summary(project)
    if not springvue:
        return
    dataflow = springvue.get("dataflow", {})
    if not isinstance(dataflow, dict):
        return
    lines.extend([
        "", "## 数据通路覆盖摘要", "",
        "- HTTP 接口：{0}".format(dataflow.get("endpoint_count", 0)),
        "- 前端请求：{0}".format(dataflow.get("frontend_call_count", 0)),
        "- 静态完整链路：{0}".format(dataflow.get("complete_count", 0)),
        "- 部分链路：{0}".format(dataflow.get("partial_count", 0)),
        "- 待确认链路：{0}".format(dataflow.get("needs_confirmation_count", 0)),
        "- 无前端直接调用的后端接口：{0}".format(dataflow.get("unmatched_endpoint_count", 0)),
        "- 未匹配后端路由的前端请求：{0}".format(dataflow.get("unmatched_frontend_count", 0)),
    ])
    strategy = springvue.get("review_strategy", "endpoint_chain")
    lines.append("- 审查策略：" + ("端点链路审查" if strategy == "endpoint_chain" else "分层降级审查"))
    fallback_reasons = springvue.get("fallback_reasons", [])
    if isinstance(fallback_reasons, list):
        lines.extend("- 降级原因：" + str(item) for item in fallback_reasons)
    chains = springvue.get("chains", [])
    incomplete = [item for item in chains if isinstance(item, dict) and item.get("status") != "complete"] if isinstance(chains, list) else []
    if incomplete:
        lines.extend(["", "### 静态分析覆盖限制", ""])
        for chain in incomplete:
            if not isinstance(chain, dict):
                continue
            status = CHAIN_STATUS.get(str(chain.get("status", "")), str(chain.get("status", "未知")))
            lines.append("- `{0}`：{1}；状态：{2}；风险优先级：{3}".format(
                chain.get("endpoint", "未知接口"), chain.get("path", "未形成通路"), status, chain.get("risk_score", 0),
            ))
            gaps = chain.get("gaps", [])
            if isinstance(gaps, list) and gaps:
                lines.append("  - 断点：" + "、".join(str(item) for item in gaps))
    unmatched = springvue.get("unmatched_frontend_requests", [])
    if isinstance(unmatched, list) and unmatched:
        lines.extend(["", "### 未关联前端请求", ""])
        for item in unmatched:
            if isinstance(item, dict):
                lines.append("- `{0}`（`{1}:{2}`）".format(item.get("request", "未知请求"), item.get("file", ""), item.get("line", "")))
    failures = springvue.get("extractor_failures", [])
    if isinstance(failures, list) and failures:
        lines.extend(["", "### 提取降级", ""])
        lines.extend("- " + str(item) for item in failures)


def _percentage(value: object) -> str:
    try:
        return "{0:.0f}%".format(float(value) * 100)
    except (TypeError, ValueError):
        return "未知"
