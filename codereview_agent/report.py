"""Terminal rendering and optional Markdown artifact generation."""

from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .types import Issue, ProjectMap, ReviewResult


ORDER = ["严重 Bug", "中等 Bug", "轻度 Bug", "优化建议", "需人工确认"]
ICONS = {"严重 Bug": "[严重]", "中等 Bug": "[中等]", "轻度 Bug": "[轻度]", "优化建议": "[优化]", "需人工确认": "[人工确认]"}


def print_project_summary(project: ProjectMap) -> None:
    counts: Dict[str, int] = {}
    for source in project.files:
        counts[source.language] = counts.get(source.language, 0) + 1
    print("\n扫描完成：" + str(project.root))
    print("识别技术栈：" + "、".join(project.technologies))
    print("文件统计：" + "，".join("{0} {1}".format(key, value) for key, value in sorted(counts.items())))
    print("关系线索：路由 {0}，前端 API {1}，跨文件关系 {2}".format(len(project.routes), len(project.api_calls), len(project.relations)))


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
    print("   证据：" + issue.evidence)
    print("   路径：" + issue.trigger_path)
    print("   影响：" + issue.impact)
    print("   建议：" + issue.recommendation)


def write_markdown(result: ReviewResult, output_dir: Path) -> Path:
    destination = output_dir / "codereview-report.md"
    lines: List[str] = ["# CodeReview Agent 审查报告", "", "生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "", "## 项目摘要", "", "- 路径：`{0}`".format(result.project.root), "- 技术栈：" + "、".join(result.project.technologies), "- 审查任务数：{0}".format(len(result.tasks)), "- 实际 Token：输入 {0}，输出 {1}，合计 {2}".format(result.usage.prompt_tokens, result.usage.completion_tokens, result.usage.total_tokens)]
    for severity in ORDER:
        issues = [issue for issue in result.issues if issue.severity == severity]
        if not issues:
            continue
        lines.extend(["", "## {0}".format(severity)])
        for index, issue in enumerate(issues, start=1):
            lines.extend(["", "### {0}. {1}".format(index, issue.title), "", "- 类别：{0}".format(issue.category), "- 位置：`{0}{1}`".format(issue.file, ":" + str(issue.line) if issue.line else ""), "- 复核状态：{0}".format(issue.review_status), "- 置信度：{0}".format(issue.confidence), "- 证据：{0}".format(issue.evidence), "- 触发路径：{0}".format(issue.trigger_path), "- 影响：{0}".format(issue.impact), "- 修复建议：{0}".format(issue.recommendation)])
    if result.failed_tasks or result.uncovered:
        lines.extend(["", "## 未覆盖范围与限制"])
        lines.extend(["- " + item for item in result.failed_tasks + result.uncovered])
    lines.extend(["", "## 下游 Coding Agent 修复交接说明", "", "请按以下原则处理本报告：", "", "1. 先修复严重 Bug，再修复中等 Bug；标为需人工确认的问题必须先验证。", "2. 修改仅限于问题关联文件及必要依赖，避免无关重构。", "3. 每项修复后补充或执行相应的单元、接口或前端验证。", "4. 重新运行 CodeReview Agent，确认原问题不再出现且没有引入新问题。"])
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination
