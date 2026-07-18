"""Build endpoint-centric review chains from verified evidence."""

from collections import deque
from typing import Dict, List, Set, Tuple

from .evidence import ChainSummary, EndpointChain, EvidenceEdge, EvidenceGraph


TRAVERSAL_KINDS = {
    "routes_to", "invokes", "implements_method", "framework_persists", "calls_remote",
    "maps_to_sql", "reads_table", "writes_table", "accepts", "returns",
}


def build_endpoint_chains(graph: EvidenceGraph) -> Tuple[List[EndpointChain], ChainSummary]:
    endpoints = sorted((node for node in graph.nodes.values() if node.kind == "controller_endpoint"), key=lambda node: (node.location.file, node.location.line))
    api_calls = [node for node in graph.nodes.values() if node.kind == "api_call"]
    matched_api_ids: Set[str] = set()
    chains: List[EndpointChain] = []

    for index, endpoint in enumerate(endpoints, start=1):
        frontend_edges = _connect_frontend_calls(graph, api_calls, endpoint)
        matched_api_ids.update(edge.source for edge in frontend_edges)
        node_ids, edges = _walk_from_endpoint(graph, endpoint.node_id)
        for edge in frontend_edges:
            if edge.source not in node_ids:
                node_ids.insert(0, edge.source)
            if edge not in edges:
                edges.insert(0, edge)
            for action_edge in graph.incoming(edge.source, {"initiates"}, model_facts_only=True):
                if action_edge.source not in node_ids:
                    node_ids.insert(0, action_edge.source)
                if action_edge not in edges:
                    edges.insert(0, action_edge)
        config_ids = [node.node_id for node in graph.nodes.values() if node.kind == "config_key"]
        status, gaps = _classify(graph, node_ids, edges)
        risk_score, risk_reasons = _risk(graph, endpoint, node_ids)
        chains.append(EndpointChain(
            "springvue.http.{0:03d}".format(index),
            endpoint.name,
            node_ids,
            edges,
            status,
            gaps,
            config_ids,
            risk_score,
            risk_reasons,
        ))

    summary = ChainSummary(
        endpoint_count=len(endpoints),
        frontend_call_count=len(api_calls),
        complete_count=sum(chain.status == "complete" for chain in chains),
        partial_count=sum(chain.status == "partial" for chain in chains),
        needs_confirmation_count=sum(chain.status == "needs_confirmation" for chain in chains),
        unmatched_endpoint_count=sum(not any(edge.kind == "routes_to" and graph.nodes.get(edge.source, None) and graph.nodes[edge.source].kind == "api_call" for edge in chain.edges) for chain in chains),
        unmatched_frontend_count=len([node for node in api_calls if node.node_id not in matched_api_ids]),
        controller_business_call_count=graph.metrics.get("controller_business_call_count", 0),
        resolved_controller_business_call_count=graph.metrics.get("resolved_controller_business_call_count", 0),
        service_persistence_call_count=graph.metrics.get("service_persistence_call_count", 0),
        resolved_service_persistence_call_count=graph.metrics.get("resolved_service_persistence_call_count", 0),
    )
    return chains, summary


def render_chain_path(graph: EvidenceGraph, chain: EndpointChain) -> str:
    kinds = {"frontend_action", "api_call", "controller_endpoint", "java_method", "mapper_method", "framework_persistence", "remote_call", "sql_statement", "database_table"}
    names = []
    for node_id in chain.node_ids:
        node = graph.nodes.get(node_id)
        if node and node.kind in kinds and node.name not in names:
            names.append(node.name)
    return " -> ".join(names)


def _connect_frontend_calls(graph: EvidenceGraph, api_calls, endpoint) -> List[EvidenceEdge]:
    result = []
    endpoint_method = str(endpoint.metadata.get("method", ""))
    endpoint_url = str(endpoint.metadata.get("normalized_url", ""))
    for call in api_calls:
        call_method = str(call.metadata.get("method", ""))
        call_url = str(call.metadata.get("normalized_url", ""))
        if call_url != endpoint_url:
            continue
        if call_method not in {endpoint_method, "FETCH", "REQUEST"} and endpoint_method != "REQUEST":
            continue
        confidence = "high" if call_method == endpoint_method and call.metadata.get("confidence") == "high" else "medium"
        edge = graph.add_edge(EvidenceEdge(
            call.node_id,
            endpoint.node_id,
            "routes_to",
            "前端 HTTP 方法与规范化 URL 匹配" if confidence == "high" else "前端 URL 匹配但方法或动态路径需确认",
            confidence,
            call.location,
            endpoint.location,
        ))
        result.append(edge)
    return result


def _walk_from_endpoint(graph: EvidenceGraph, endpoint_id: str) -> Tuple[List[str], List[EvidenceEdge]]:
    queue = deque([(endpoint_id, 0)])
    visited: Set[str] = set()
    node_ids: List[str] = []
    edges: List[EvidenceEdge] = []
    while queue:
        node_id, depth = queue.popleft()
        if node_id in visited or depth > 8:
            continue
        visited.add(node_id)
        node_ids.append(node_id)
        for edge in graph.outgoing(node_id, TRAVERSAL_KINDS, model_facts_only=True):
            edges.append(edge)
            if edge.target not in visited:
                queue.append((edge.target, depth + 1))
    return node_ids, edges


def _classify(graph: EvidenceGraph, node_ids: List[str], edges: List[EvidenceEdge]) -> Tuple[str, List[str]]:
    kinds = {graph.nodes[node_id].kind for node_id in node_ids if node_id in graph.nodes}
    gaps = []
    if "java_method" not in kinds:
        gaps.append("未找到 Controller 方法")
    for node_id in node_ids:
        node = graph.nodes.get(node_id)
        if not node or node.kind != "java_method":
            continue
        unresolved = node.metadata.get("unresolved_calls", [])
        if isinstance(unresolved, list):
            gaps.extend("未解析调用：" + str(item) for item in unresolved)
    mapper_nodes = [graph.nodes[node_id] for node_id in node_ids if node_id in graph.nodes and graph.nodes[node_id].kind == "mapper_method"]
    for mapper in mapper_nodes:
        if not any(edge.source == mapper.node_id and edge.kind == "maps_to_sql" for edge in edges):
            gaps.append("Mapper 方法未找到显式 SQL：" + mapper.name)
    sql_nodes = [graph.nodes[node_id] for node_id in node_ids if node_id in graph.nodes and graph.nodes[node_id].kind == "sql_statement"]
    for sql in sql_nodes:
        if not any(edge.source == sql.node_id and edge.kind in {"reads_table", "writes_table"} for edge in edges):
            gaps.append("SQL 未识别操作表：" + sql.name)
    if any(edge.confidence == "medium" for edge in edges):
        return "needs_confirmation", list(dict.fromkeys(gaps))
    if gaps:
        return "partial", list(dict.fromkeys(gaps))
    return "complete", []


def _risk(graph: EvidenceGraph, endpoint, node_ids: List[str]) -> Tuple[int, List[str]]:
    score = 0
    reasons = []
    method = str(endpoint.metadata.get("method", ""))
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        score += 30
        reasons.append("写接口")
    for node_id in node_ids:
        node = graph.nodes.get(node_id)
        if not node or node.kind not in {"sql_statement", "framework_persistence"}:
            continue
        if str(node.metadata.get("operation", "")) in {"insert", "update", "delete"}:
            score += 25
            reasons.append("数据库写操作")
        if node.metadata.get("uses_dollar_placeholder"):
            score += 30
            reasons.append("MyBatis ${} 替换")
        if node.metadata.get("unmatched_placeholders"):
            score += 20
            reasons.append("SQL 参数与 Mapper 参数不一致")
        if str(node.metadata.get("operation", "")) in {"update", "delete"} and not node.metadata.get("has_where"):
            score += 30
            reasons.append("更新或删除未发现 WHERE")
    return min(score, 100), list(dict.fromkeys(reasons))
