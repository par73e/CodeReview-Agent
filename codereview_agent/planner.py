"""Risk-first task planning and minimal context construction."""

from pathlib import Path
from typing import Dict, Iterable, List, Set

from .types import ProjectMap, ReviewTask, SourceFile


CHECKLISTS: Dict[str, List[str]] = {
    "接口安全与权限": [
        "检查鉴权、IDOR 越权、参数校验、批量赋值和异常泄露。",
        "安全结论必须说明外部输入、处理过程和危险落点。",
        "只有代码证据足以证明时才判为安全漏洞。",
    ],
    "业务正确性与事务": [
        "检查多次写库的事务和回滚、先查后改并发、状态流转及空值/异常处理。",
        "识别循环查库和职责过重，但不要把风格偏好当成 Bug。",
    ],
    "MyBatis 与 MySQL": [
        "检查 ${}、字符串拼接、动态 SQL、更新/删除 WHERE、分页、N+1 和全表扫描风险。",
        "没有 DDL 或执行计划时，索引结论只能是建议或需人工确认。",
    ],
    "Nacos 与应用配置": [
        "检查明文密钥、Profile 混用、数据源和 Nacos 命名空间/分组的可见误配风险。",
        "不得声称验证运行中 Nacos 集群或线上环境。",
    ],
    "Vue 与前后端契约": [
        "检查 v-html、Token/401、路由守卫、异步错误处理、表单校验及 API 契约。",
        "只有后端也缺少保护时，前端权限隐藏才可能构成严重安全问题。",
    ],
    "结构与性能": [
        "检查 Controller 直连 Mapper、重复职责、超长方法、循环查库和可验证的性能优化点。",
        "优化建议必须说明预期收益和适用前提。",
    ],
}


def build_review_plan(project: ProjectMap) -> List[ReviewTask]:
    tasks: List[ReviewTask] = []
    roles = project.roles
    controllers = roles.get("controller", [])
    services = roles.get("service", [])
    mapper_paths = roles.get("mapper", []) + roles.get("mapper_xml", []) + roles.get("sql_file", [])
    config_paths = roles.get("configuration", [])
    frontend = roles.get("vue_component", []) + roles.get("frontend_script", [])

    if controllers:
        tasks.append(_task("security", "接口安全与权限", 10, controllers, _related(project, controllers), "外部 HTTP 入口和权限边界优先审查"))
    if services:
        tasks.append(_task("business", "业务正确性与事务", 20, services, _related(project, services), "业务写操作、事务和状态流转"))
    if mapper_paths:
        tasks.append(_task("database", "MyBatis 与 MySQL", 30, mapper_paths, _related(project, mapper_paths), "数据库输入边界、SQL 与查询行为"))
    if config_paths:
        tasks.append(_task("config", "Nacos 与应用配置", 40, config_paths, [], "配置、Profile、Nacos 和敏感信息"))
    if frontend:
        tasks.append(_task("frontend", "Vue 与前后端契约", 50, frontend, _related(project, frontend), "前端安全边界和接口契约"))

    all_primary = controllers + services
    if all_primary:
        tasks.append(_task("architecture", "结构与性能", 60, all_primary, mapper_paths[:8], "跨层关系、重复职责和性能线索"))
    return tasks


def estimate_tokens(project: ProjectMap, tasks: List[ReviewTask], output_per_task: int = 1300) -> Dict[str, int]:
    source_by_path = {item.relative_path: item for item in project.files}
    input_chars = 0
    for task in tasks:
        seen: Set[str] = set()
        for path in task.target_paths + task.related_paths:
            if path in seen or path not in source_by_path:
                continue
            seen.add(path)
            input_chars += min(len(source_by_path[path].content), 18000)
    # Conservative rough estimate suitable for a confirmation prompt, not billing.
    input_tokens = max(1, input_chars // 3)
    return {"input": input_tokens, "output_max": len(tasks) * output_per_task, "total_max": input_tokens + len(tasks) * output_per_task}


def build_context(project: ProjectMap, task: ReviewTask) -> str:
    source_by_path = {item.relative_path: item for item in project.files}
    sections: List[str] = ["项目技术栈：" + "、".join(project.technologies)]
    sections.append("任务范围：" + task.domain)
    sections.append("任务理由：" + task.rationale)
    sections.append("审查清单：\n- " + "\n- ".join(task.checklist))
    relevant = []
    seen: Set[str] = set()
    for path in task.target_paths + task.related_paths:
        if path in seen or path not in source_by_path:
            continue
        seen.add(path)
        relevant.append(source_by_path[path])
    for source in relevant[:14]:
        sections.append(_render_source(source, is_target=source.relative_path in task.target_paths))
    relation_lines = ["{0} --{1}--> {2}".format(item.source, item.kind, item.target) for item in project.relations if item.source in seen or item.target in seen]
    if relation_lines:
        sections.append("关联事实：\n" + "\n".join(relation_lines[:40]))
    return "\n\n".join(sections)


def _render_source(source: SourceFile, is_target: bool) -> str:
    lines = source.content.splitlines()
    limit = 420 if is_target else 140
    if len(lines) > limit:
        lines = lines[:limit]
        suffix = "\n...（该文件其余内容未提供）"
    else:
        suffix = ""
    numbered = "\n".join("{0:4d}: {1}".format(index, line) for index, line in enumerate(lines, start=1))
    label = "目标文件" if is_target else "关联文件"
    return "{0}: {1}\n```{2}\n{3}\n```{4}".format(label, source.relative_path, source.language, numbered, suffix)


def _related(project: ProjectMap, paths: Iterable[str]) -> List[str]:
    selected = set(paths)
    related: List[str] = []
    for relation in project.relations:
        if relation.source in selected and relation.target not in selected:
            related.append(relation.target)
        elif relation.target in selected and relation.source not in selected:
            related.append(relation.source)
    return list(dict.fromkeys(related))[:12]


def _task(task_id: str, domain: str, priority: int, targets: List[str], related: List[str], rationale: str) -> ReviewTask:
    return ReviewTask(task_id, domain, priority, targets, related, CHECKLISTS[domain], rationale)
