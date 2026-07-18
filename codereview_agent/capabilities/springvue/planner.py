"""Plan endpoint-chain review tasks and render minimal evidence contexts."""

from typing import Dict, List

from ...types import ProjectMap, ReviewTask, SourceFile
from .chain_builder import render_chain_path
from .evidence import EndpointChain, EvidenceGraph


CHAIN_CHECKLIST = [
    "核对前端 HTTP 方法、URL、请求字段、Controller 参数和响应使用是否一致。",
    "沿已证明链路检查鉴权、对象所有权、输入校验、异常泄露和危险输入落点。",
    "结合实际写库路径检查事务、回滚、状态流转、空值、重复提交和并发风险。",
    "核对 Mapper 参数、MyBatis 映射、SQL 占位符、更新删除条件、分页和返回结构。",
    "配置只依据已提供的本地事实；不得推断远程 Nacos 值或运行时生效状态。",
    "优化建议必须说明链路中的具体位置、预期收益和适用前提。",
]


def build_chain_review_plan(project: ProjectMap, graph: EvidenceGraph, chains: List[EndpointChain]) -> List[ReviewTask]:
    if not chains:
        return []
    tasks: List[ReviewTask] = []
    for chain in sorted(chains, key=lambda item: (-item.risk_score, item.chain_id)):
        paths = _chain_paths(graph, chain)
        metadata = {
            "kind": "endpoint_chain",
            "chain_id": chain.chain_id,
            "endpoint": chain.endpoint,
            "chain_status": chain.status,
            "chain_path": render_chain_path(graph, chain),
            "chain_gaps": list(chain.gaps),
            "risk_score": chain.risk_score,
            "risk_reasons": list(chain.risk_reasons),
            "relations": [_edge_dict(graph, edge) for edge in chain.edges],
            "config_facts": _config_facts(graph, chain),
            "fragments": _fragments(project, graph, chain),
        }
        tasks.append(ReviewTask(
            chain.chain_id,
            "全链路：" + chain.endpoint,
            max(1, 100 - chain.risk_score),
            paths,
            [],
            CHAIN_CHECKLIST,
            "按已证明的 HTTP 数据通路执行端点级审查",
            metadata,
        ))

    config_paths = sorted({node.location.file for node in graph.nodes.values() if node.kind == "config_key"})
    if config_paths:
        tasks.append(ReviewTask(
            "springvue.config-context",
            "项目运行配置上下文",
            80,
            config_paths,
            [],
            [
                "检查 Profile、数据源、MyBatis 与 Nacos 引用的可见一致性。",
                "远程配置不可见时只能记录未验证，不能推断真实值。",
                "敏感配置只根据脱敏证据报告，不得在输出中复述原值。",
            ],
            "配置作为数据访问链路的运行上下文统一审查，避免每条链重复发送完整 YAML。",
            {
                "kind": "springvue_config",
                "config_facts": _all_config_facts(graph),
            },
        ))
    return tasks


def build_chain_context(task: ReviewTask) -> str:
    metadata = task.metadata
    sections = [
        "审查类型：SpringVue HTTP 端点级全链路审查",
        "链路编号：" + str(metadata.get("chain_id", "")),
        "接口：" + str(metadata.get("endpoint", "")),
        "链路状态：" + str(metadata.get("chain_status", "")),
        "已证明通路：" + str(metadata.get("chain_path", "")),
    ]
    gaps = metadata.get("chain_gaps", [])
    if gaps:
        sections.append("断链与未覆盖：\n- " + "\n- ".join(str(item) for item in gaps))
    risk_reasons = metadata.get("risk_reasons", [])
    if risk_reasons:
        sections.append("优先审查依据：" + "、".join(str(item) for item in risk_reasons))
    sections.append("审查清单：\n- " + "\n- ".join(task.checklist))

    relations = metadata.get("relations", [])
    if relations:
        lines = []
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            lines.append("{0} --{1}/{2}--> {3}；证据：{4}".format(
                relation.get("source", ""), relation.get("kind", ""), relation.get("confidence", ""),
                relation.get("target", ""), relation.get("evidence", ""),
            ))
        sections.append("Agent 已证明的关系：\n" + "\n".join(lines))

    config_facts = metadata.get("config_facts", [])
    if config_facts:
        sections.append("可见配置上下文（远程值未验证）：\n- " + "\n- ".join(str(item) for item in config_facts))

    for fragment in metadata.get("fragments", []):
        if not isinstance(fragment, dict):
            continue
        sections.append("{0}: {1}:{2}\n```{3}\n{4}\n```".format(
            fragment.get("label", "代码片段"), fragment.get("file", ""), fragment.get("line", 1),
            fragment.get("language", "text"), fragment.get("content", ""),
        ))
    sections.append("只能依据以上关系和代码判断；中可信关系必须保守处理，断链部分不得自行补全。")
    return "\n\n".join(sections)


def build_config_context(task: ReviewTask) -> str:
    facts = task.metadata.get("config_facts", [])
    rendered = [str(item) for item in facts] if isinstance(facts, list) else []
    return "\n\n".join([
        "审查类型：SpringVue 项目运行配置上下文",
        "审查清单：\n- " + "\n- ".join(task.checklist),
        "Agent 已提取并脱敏的配置事实：\n- " + ("\n- ".join(rendered) if rendered else "无可见配置事实"),
        "只能依据以上脱敏事实判断；不得推断远程 Nacos 值、运行时生效顺序或被脱敏的原始值。",
    ])


def _chain_paths(graph: EvidenceGraph, chain: EndpointChain) -> List[str]:
    result = []
    for node_id in chain.node_ids:
        node = graph.nodes.get(node_id)
        if node and node.location.file not in result:
            result.append(node.location.file)
    return result


def _edge_dict(graph: EvidenceGraph, edge) -> Dict[str, str]:
    source = graph.nodes.get(edge.source)
    target = graph.nodes.get(edge.target)
    return {
        "source": source.name if source else edge.source,
        "target": target.name if target else edge.target,
        "kind": edge.kind,
        "confidence": edge.confidence,
        "evidence": edge.evidence,
    }


def _config_facts(graph: EvidenceGraph, chain: EndpointChain) -> List[str]:
    facts = []
    for node_id in chain.config_node_ids:
        node = graph.nodes.get(node_id)
        if not node:
            continue
        value = str(node.metadata.get("value", ""))
        facts.append("{0}={1}（{2}:{3}）".format(node.name, value, node.location.file, node.location.line))
    return facts[:20]


def _all_config_facts(graph: EvidenceGraph) -> List[str]:
    facts = []
    for node in graph.nodes.values():
        if node.kind != "config_key":
            continue
        facts.append("{0}={1}（{2}:{3}）".format(
            node.name, node.metadata.get("value", ""), node.location.file, node.location.line,
        ))
    return facts[:80]


def _fragments(project: ProjectMap, graph: EvidenceGraph, chain: EndpointChain) -> List[Dict[str, object]]:
    source_by_path: Dict[str, SourceFile] = {source.relative_path: source for source in project.files}
    fragments = []
    seen = set()
    for node_id in chain.node_ids:
        node = graph.nodes.get(node_id)
        if not node or node.kind in {"controller_endpoint", "database_table"}:
            continue
        source = source_by_path.get(node.location.file)
        if not source:
            continue
        start = max(1, node.location.line)
        end = node.location.end_line or start
        if node.kind == "dto_type":
            start, end = 1, min(source.line_count, 160)
        elif start == end:
            start, end = max(1, start - 2), min(source.line_count, end + 6)
        key = (source.relative_path, start, end)
        if key in seen:
            continue
        seen.add(key)
        lines = source.content.splitlines()
        numbered = "\n".join("{0:4d}: {1}".format(index, lines[index - 1]) for index in range(start, min(end, len(lines)) + 1))
        fragments.append({
            "label": _fragment_label(node.kind),
            "file": source.relative_path,
            "line": start,
            "language": source.language,
            "content": numbered,
        })
    return fragments[:18]


def _fragment_label(kind: str) -> str:
    return {
        "frontend_action": "前端操作",
        "api_call": "前端 API",
        "java_method": "Java 方法",
        "dto_type": "DTO",
        "mapper_method": "Mapper 方法",
        "framework_persistence": "框架持久化操作",
        "remote_call": "远程客户端调用",
        "sql_statement": "SQL",
    }.get(kind, "相关代码")
