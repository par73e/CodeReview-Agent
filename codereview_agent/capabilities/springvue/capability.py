"""SpringVue capability backed by endpoint evidence chains."""

from pathlib import Path
from typing import List

from ...planner import build_review_plan
from ...project_map import build_project_map
from ...types import Relation, SourceFile
from ..base import Capability, CapabilityDetection, CapabilityResult
from .chain_builder import build_endpoint_chains, render_chain_path
from .evidence import EvidenceGraph
from .extractors import extract_config, extract_frontend, extract_mybatis, extract_spring, extract_sql
from .java_index import JavaSemanticIndex
from .planner import build_chain_review_plan


class SpringVueCapability(Capability):
    name = "SpringVue"
    _claimed_languages = {"java", "vue", "javascript", "typescript", "sql", "xml", "yaml"}

    def detect(self, files: List[SourceFile]) -> CapabilityDetection:
        content = "\n".join(item.content for item in files)
        score = 0.0
        reasons: List[str] = []
        if "@SpringBootApplication" in content or "spring-boot" in content.lower():
            score += 0.6
            reasons.append("Spring Boot 启动标记")
        elif any("@RestController" in item.content or "@Controller" in item.content for item in files if item.language == "java"):
            score += 0.55
            reasons.append("Spring MVC Controller")
        if "org.apache.ibatis" in content or "<mapper" in content or "@Mapper" in content:
            score += 0.15
            reasons.append("MyBatis")
        if "nacos" in content.lower():
            score += 0.1
            reasons.append("Nacos")
        if any(item.language == "vue" for item in files):
            score += 0.1
            reasons.append("Vue")
        return CapabilityDetection(self.name, min(score, 1.0), "、".join(reasons) or "未发现 SpringVue 特征")

    def claim_files(self, files: List[SourceFile]) -> List[SourceFile]:
        return [item for item in files if item.language in self._claimed_languages]

    def analyze(self, root: Path, files: List[SourceFile]) -> CapabilityResult:
        project = build_project_map(root, files)
        graph = EvidenceGraph()
        try:
            java_index = JavaSemanticIndex(files)
        except Exception as error:
            java_index = None
            graph.failures.append("Java 语义索引构建失败：{0}".format(error))
        extractors = [
            ("extract_frontend", lambda: extract_frontend(files, graph)),
            ("extract_mybatis", lambda: extract_mybatis(files, graph, java_index)),
            ("extract_spring", lambda: extract_spring(files, graph, java_index)),
            ("extract_sql", lambda: extract_sql(files, graph)),
            ("extract_config", lambda: extract_config(files, graph)),
        ]
        for extractor_name, extractor in extractors:
            try:
                extractor()
            except Exception as error:
                graph.failures.append("{0} 提取失败：{1}".format(extractor_name, error))

        chains, summary = build_endpoint_chains(graph)
        fallback_reasons = _fallback_reasons(summary)
        if not chains:
            fallback_reasons.append("未形成可执行的 HTTP 端点链路")
        strategy = "layered_fallback" if fallback_reasons else "endpoint_chain"
        project.analysis_summary[self.name] = {
            "dataflow": summary.as_dict(),
            "review_strategy": strategy,
            "fallback_reasons": fallback_reasons,
            "extractor_failures": list(graph.failures),
            "chains": [
                {
                    "chain_id": chain.chain_id,
                    "endpoint": chain.endpoint,
                    "status": chain.status,
                    "gaps": list(chain.gaps),
                    "risk_score": chain.risk_score,
                    "path": render_chain_path(graph, chain),
                    "frontend_linked": _has_frontend_route(graph, chain),
                }
                for chain in chains
            ],
            "unmatched_frontend_requests": _unmatched_frontend_requests(graph),
        }
        _merge_graph_relations(project, graph)
        tasks = build_chain_review_plan(project, graph, chains) if strategy == "endpoint_chain" else []
        if not any(task.metadata.get("kind") == "endpoint_chain" for task in tasks):
            tasks = build_review_plan(project)
        claimed = self.claim_files(files)
        return CapabilityResult(self.name, project, tasks, [item.relative_path for item in claimed], self.detect(files).reason)


def _merge_graph_relations(project, graph: EvidenceGraph) -> None:
    known = {(relation.source, relation.target, relation.kind, relation.evidence) for relation in project.relations}
    for edge in graph.edges:
        if not edge.model_fact or not edge.source_location or not edge.target_location:
            continue
        if edge.source_location.file == edge.target_location.file:
            continue
        value = (edge.source_location.file, edge.target_location.file, edge.kind, edge.evidence)
        if value in known:
            continue
        project.relations.append(Relation(*value))
        known.add(value)


def _has_frontend_route(graph: EvidenceGraph, chain) -> bool:
    return any(
        edge.kind == "routes_to"
        and edge.source in graph.nodes
        and graph.nodes[edge.source].kind == "api_call"
        for edge in chain.edges
    )


def _unmatched_frontend_requests(graph: EvidenceGraph):
    matched = {
        edge.source
        for edge in graph.edges
        if edge.kind == "routes_to"
        and edge.source in graph.nodes
        and graph.nodes[edge.source].kind == "api_call"
    }
    result = []
    for node in graph.nodes.values():
        if node.kind != "api_call" or node.node_id in matched:
            continue
        result.append({
            "request": node.name,
            "file": node.location.file,
            "line": node.location.line,
        })
    return result


def _fallback_reasons(summary) -> List[str]:
    reasons = []
    if summary.controller_business_call_count >= 3:
        rate = summary.resolved_controller_business_call_count / summary.controller_business_call_count
        if rate < 0.5:
            reasons.append("Controller 业务调用解析率低于 50%")
    if summary.service_persistence_call_count >= 3:
        rate = summary.resolved_service_persistence_call_count / summary.service_persistence_call_count
        if rate < 0.5:
            reasons.append("Service 持久层调用解析率低于 50%")
    if summary.endpoint_count >= 3 and summary.partial_count / summary.endpoint_count > 0.7:
        reasons.append("超过 70% 的 HTTP 端点存在确定性断链")
    return reasons
