"""Extract Spring routes, Java methods, DTOs, and typed invocation evidence."""

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ....types import SourceFile
from ..evidence import EvidenceEdge, EvidenceGraph, EvidenceLocation, EvidenceNode
from ..java_index import JavaSemanticIndex
from ..source_utils import (
    find_matching,
    join_url,
    line_of,
    mask_comments_and_strings,
    normalize_type,
    parse_java_parameter,
    split_arguments,
    stable_id,
)


CLASS_PATTERN = re.compile(r"\b(?:class|interface)\s+(\w+)")
PACKAGE_PATTERN = re.compile(r"\bpackage\s+([\w.]+)\s*;")
FIELD_PATTERN = re.compile(r"\b(?:private|protected|public)\s+(?:static\s+)?(?:final\s+)?([\w.<>?,]+)\s+(\w+)\s*;")
METHOD_PATTERN = re.compile(
    r"(?P<annotations>(?:\s*@\w+(?:\s*\([^)]*\))?\s*)*)"
    r"\b(?:(?:public|protected|private)\s+)?(?:(?:static|final|synchronized|abstract)\s+)*"
    r"(?P<return>[\w.<>?,\[\]\s]+?)\s+(?P<name>\w+)\s*\((?P<params>(?:[^()]|\([^()]*\))*)\)\s*\{",
    re.S,
)
CALL_PATTERN = re.compile(r"\b(\w+)\s*\.\s*(\w+)\s*\(")
MAPPING_PATTERN = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\((?P<args>[^)]*)\))?", re.I | re.S)
MYBATIS_PLUS_METHODS = {
    "save": "insert", "saveBatch": "insert",
    "getById": "select", "getOne": "select", "list": "select", "page": "select", "count": "select",
    "updateById": "update", "update": "update",
    "removeById": "delete", "remove": "delete", "removeByIds": "delete",
}


def extract_spring(files: List[SourceFile], graph: EvidenceGraph, index: Optional[JavaSemanticIndex] = None) -> None:
    java_files = [source for source in files if source.language == "java"]
    index = index or JavaSemanticIndex(files)
    type_paths: Dict[str, str] = {}
    type_packages: Dict[str, str] = {}
    for source in java_files:
        class_match = CLASS_PATTERN.search(source.content)
        if not class_match:
            continue
        class_name = class_match.group(1)
        type_paths[class_name] = source.relative_path
        package_match = PACKAGE_PATTERN.search(source.content)
        type_packages[class_name] = package_match.group(1) if package_match else ""

    method_lookup: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    method_records: List[Dict[str, object]] = []
    for source in java_files:
        class_match = CLASS_PATTERN.search(source.content)
        if not class_match:
            continue
        class_name = class_match.group(1)
        class_prefix = source.content[max(0, class_match.start() - 1200):class_match.start()]
        class_route = _mapping_path(class_prefix)
        is_controller = "@RestController" in class_prefix or "@Controller" in class_prefix or "@RestController" in source.content[:class_match.start()]
        receiver_types = _receiver_types(source.content, class_name, type_paths, index)
        masked = mask_comments_and_strings(source.content)

        for match in METHOD_PATTERN.finditer(masked):
            opening = match.end() - 1
            closing = find_matching(masked, opening)
            if closing is None:
                graph.failures.append("Spring 方法边界无法确认：{0}:{1}".format(source.relative_path, line_of(source.content, match.start())))
                continue
            method_name = match.group("name")
            if method_name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                continue
            line = line_of(source.content, match.start())
            end_line = line_of(source.content, closing)
            annotations = source.content[match.start("annotations"):match.end("annotations")]
            raw_params = source.content[match.start("params"):match.end("params")]
            node_id = stable_id("method", source.relative_path, line, class_name + "." + method_name)
            node = EvidenceNode(
                node_id,
                "java_method",
                class_name + "." + method_name,
                EvidenceLocation(source.relative_path, line, end_line),
                {
                    "class": class_name,
                    "method": method_name,
                    "return_type": " ".join(match.group("return").split()),
                    "params": raw_params,
                    "annotations": annotations.strip(),
                    "body": source.content[opening + 1:closing],
                    "body_start": opening + 1,
                    "role": _java_role(source, class_name),
                    "receiver_types": dict(receiver_types),
                },
            )
            graph.add_node(node)
            method_lookup[(class_name, method_name)].append(node_id)
            method_records.append({"node": node, "receivers": receiver_types, "source": source})

            if is_controller:
                mapping = _mapping_details(annotations)
                if mapping:
                    http_method, method_route = mapping
                    full_route = join_url(class_route, method_route)
                    endpoint_name = "{0} {1}".format(http_method, full_route)
                    endpoint = EvidenceNode(
                        stable_id("endpoint", source.relative_path, line, endpoint_name),
                        "controller_endpoint",
                        endpoint_name,
                        EvidenceLocation(source.relative_path, line, end_line),
                        {
                            "method": http_method,
                            "path": full_route,
                            "normalized_url": full_route,
                            "controller_method": node_id,
                            "params": raw_params,
                            "annotations": annotations.strip(),
                        },
                    )
                    graph.add_node(endpoint)
                    graph.add_edge(EvidenceEdge(endpoint.node_id, node_id, "routes_to", "Spring 路由注解绑定 Controller 方法", "high", endpoint.location, node.location))
                    _add_dto_edges(graph, endpoint, raw_params, type_paths)

    mapper_lookup = _mapper_nodes_by_class_and_method(graph)
    for record in method_records:
        node = record["node"]
        source = record["source"]
        receivers = record["receivers"]
        body = str(node.metadata.get("body", ""))
        body_start = int(node.metadata.get("body_start", 0))
        masked_body = mask_comments_and_strings(body)
        for call in CALL_PATTERN.finditer(masked_body):
            receiver, method_name = call.group(1), call.group(2)
            target_type = receivers.get(receiver, "")
            if not target_type:
                continue
            arity = _call_arity(body, masked_body, call)
            source_role = str(node.metadata.get("role", ""))
            controller_business = source_role == "controller" and _is_business_type(index, target_type)
            persistence_call = source_role == "service" and (receiver == "baseMapper" or target_type.endswith("Mapper") or method_name in MYBATIS_PLUS_METHODS)
            if controller_business:
                _increment(graph, "controller_business_call_count")
            if persistence_call:
                _increment(graph, "service_persistence_call_count")

            if index.is_feign_client(target_type):
                resolved = _add_remote_call(graph, node, source, body_start, call, target_type, method_name)
                if controller_business:
                    _increment(graph, "resolved_controller_business_call_count")
                if persistence_call:
                    _increment(graph, "resolved_service_persistence_call_count")
                continue

            owner_candidates = [target_type]
            implementation_owners = index.resolve_method_owners(target_type, method_name, arity)
            owner_candidates.extend(owner for owner in implementation_owners if owner not in owner_candidates)
            candidates = []
            for owner in owner_candidates:
                candidates.extend(_matching_nodes(method_lookup.get((owner, method_name), []), graph, arity))
                candidates.extend(_matching_nodes(mapper_lookup.get((owner, method_name), []), graph, arity))
            candidates = list(dict.fromkeys(candidates))
            resolved = False
            confidence = "high" if len(candidates) == 1 else "medium"
            for target_id in candidates:
                target = graph.nodes[target_id]
                call_line = line_of(source.content, body_start + call.start())
                dispatched = str(target.metadata.get("class", "")) != target_type and target.kind == "java_method"
                graph.add_edge(EvidenceEdge(
                    node.node_id,
                    target_id,
                    "implements_method" if dispatched else "invokes",
                    _call_evidence(receiver, method_name, target_type, target, index, arity),
                    confidence,
                    EvidenceLocation(source.relative_path, call_line),
                    target.location,
                ))
                resolved = True

            if not resolved:
                resolved = _add_mybatis_plus_operation(graph, index, node, source, body_start, call, target_type, method_name)
            if resolved:
                if controller_business:
                    _increment(graph, "resolved_controller_business_call_count")
                if persistence_call:
                    _increment(graph, "resolved_service_persistence_call_count")
            elif controller_business or persistence_call:
                unresolved = node.metadata.setdefault("unresolved_calls", [])
                if isinstance(unresolved, list):
                    unresolved.append("{0}.{1}(...)".format(receiver, method_name))


def connect_spring_to_mapper(graph: EvidenceGraph) -> None:
    """Add Mapper calls after MyBatis nodes have been extracted."""
    mapper_lookup = _mapper_nodes_by_class_and_method(graph)
    for node in list(graph.nodes.values()):
        if node.kind != "java_method":
            continue
        body = str(node.metadata.get("body", ""))
        receiver_types = node.metadata.get("receiver_types", {})
        if not isinstance(receiver_types, dict):
            continue
        for call in CALL_PATTERN.finditer(mask_comments_and_strings(body)):
            target_type = str(receiver_types.get(call.group(1), ""))
            candidates = mapper_lookup.get((target_type, call.group(2)), [])
            for target_id in candidates:
                target = graph.nodes[target_id]
                confidence = "high" if len(candidates) == 1 else "medium"
                graph.add_edge(EvidenceEdge(node.node_id, target_id, "invokes", "接收者类型与 Mapper 方法匹配", confidence, node.location, target.location))


def _receiver_types(content: str, class_name: str, known_types: Dict[str, str], index: JavaSemanticIndex) -> Dict[str, str]:
    receivers: Dict[str, str] = index.implicit_receivers(class_name)
    for match in FIELD_PATTERN.finditer(content):
        type_name = normalize_type(match.group(1))
        if type_name in known_types or type_name.endswith(("Service", "Mapper")):
            receivers[match.group(2)] = type_name
    constructor = re.search(r"\b{0}\s*\(([^)]*)\)".format(re.escape(class_name)), content, re.S)
    if constructor:
        for parameter in split_arguments(constructor.group(1)):
            type_name, variable, _ = parse_java_parameter(parameter)
            normalized = normalize_type(type_name)
            if normalized:
                receivers[variable] = normalized
    return receivers


def _call_arity(body: str, masked_body: str, call) -> int:
    opening = call.end() - 1
    closing = find_matching(masked_body, opening, "(", ")")
    if closing is None:
        return -1
    arguments = body[opening + 1:closing]
    return len(split_arguments(arguments)) if arguments.strip() else 0


def _matching_nodes(node_ids: List[str], graph: EvidenceGraph, arity: int) -> List[str]:
    if arity < 0:
        return list(node_ids)
    result = []
    for node_id in node_ids:
        node = graph.nodes[node_id]
        params = node.metadata.get("params", [])
        if isinstance(params, str):
            count = len(split_arguments(params)) if params.strip() else 0
        elif isinstance(params, list):
            count = len(params)
        else:
            count = -1
        if count == arity:
            result.append(node_id)
    return result


def _call_evidence(receiver: str, method_name: str, declared_type: str, target: EvidenceNode, index: JavaSemanticIndex, arity: int) -> str:
    target_class = str(target.metadata.get("class", ""))
    if target.kind == "java_method" and target_class and target_class != declared_type:
        declared = "，接口声明可见" if index.interface_declares(declared_type, method_name, arity) else ""
        return "{0}.{1}(...) 的声明类型为 {2}，唯一可见实现为 {3}{4}".format(receiver, method_name, declared_type, target_class, declared)
    return "{0}.{1}(...)，接收者类型为 {2}".format(receiver, method_name, declared_type)


def _add_mybatis_plus_operation(graph: EvidenceGraph, index: JavaSemanticIndex, source_node: EvidenceNode, source: SourceFile, body_start: int, call, target_type: str, method_name: str) -> bool:
    operation = MYBATIS_PLUS_METHODS.get(method_name)
    binding = index.service_binding(target_type)
    if not operation or binding is None or not binding.entity_type:
        return False
    line = line_of(source.content, body_start + call.start())
    display = "MyBatis-Plus {0} {1}".format(operation.upper(), binding.entity_type)
    operation_node = EvidenceNode(
        stable_id("mybatis-plus", source.relative_path, line, display),
        "framework_persistence",
        display,
        EvidenceLocation(source.relative_path, line),
        {
            "framework": "MyBatis-Plus",
            "operation": operation,
            "entity": binding.entity_type,
            "mapper": binding.mapper_type,
            "inferred": True,
        },
    )
    graph.add_node(operation_node)
    graph.add_edge(EvidenceEdge(
        source_node.node_id,
        operation_node.node_id,
        "framework_persists",
        binding.evidence + "；调用继承方法 " + method_name,
        binding.confidence,
        source_node.location,
        operation_node.location,
    ))
    table_name, table_confidence, table_evidence = index.table_for_entity(binding.entity_type)
    if table_name:
        table = EvidenceNode(
            "table:" + table_name.lower(),
            "database_table",
            table_name,
            operation_node.location,
            {"inferred_from_entity": True},
        )
        graph.add_node(table)
        relation = "reads_table" if operation == "select" else "writes_table"
        graph.add_edge(EvidenceEdge(
            operation_node.node_id,
            table.node_id,
            relation,
            table_evidence,
            table_confidence,
            operation_node.location,
            table.location,
        ))
    return True


def _add_remote_call(graph: EvidenceGraph, source_node: EvidenceNode, source: SourceFile, body_start: int, call, target_type: str, method_name: str) -> bool:
    line = line_of(source.content, body_start + call.start())
    remote = EvidenceNode(
        stable_id("remote", source.relative_path, line, target_type + "." + method_name),
        "remote_call",
        target_type + "." + method_name,
        EvidenceLocation(source.relative_path, line),
        {"client_type": target_type, "method": method_name},
    )
    graph.add_node(remote)
    graph.add_edge(EvidenceEdge(source_node.node_id, remote.node_id, "calls_remote", "接收者类型使用 @FeignClient", "high", source_node.location, remote.location))
    return True


def _is_business_type(index: JavaSemanticIndex, type_name: str) -> bool:
    info = index.type_info(type_name)
    return bool(
        type_name.endswith(("Service", "Mapper", "Client"))
        or (info and (info.implements or "@FeignClient" in info.annotations or any(parent in {"IService", "BaseMapper"} for parent in info.extends)))
    )


def _increment(graph: EvidenceGraph, key: str) -> None:
    graph.metrics[key] = graph.metrics.get(key, 0) + 1


def _java_role(source: SourceFile, class_name: str) -> str:
    if "@RestController" in source.content or "@Controller" in source.content:
        return "controller"
    if "@Service" in source.content:
        return "service"
    if "@Mapper" in source.content or class_name.endswith("Mapper"):
        return "mapper"
    return "java"


def _mapping_path(text: str) -> str:
    matches = list(MAPPING_PATTERN.finditer(text))
    if not matches:
        return ""
    return _path_from_args(matches[-1].group("args") or "")


def _mapping_details(annotations: str) -> Tuple[str, str]:
    match = MAPPING_PATTERN.search(annotations)
    if not match:
        return ()
    prefix = match.group(1).upper()
    method = prefix if prefix != "REQUEST" else _request_method(match.group("args") or "")
    return method, _path_from_args(match.group("args") or "")


def _path_from_args(args: str) -> str:
    match = re.search(r"(?:value|path)\s*=\s*[\"']([^\"']+)", args)
    if not match:
        match = re.search(r"[\"']([^\"']+)[\"']", args)
    return match.group(1) if match else ""


def _request_method(args: str) -> str:
    match = re.search(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", args, re.I)
    return match.group(1).upper() if match else "REQUEST"


def _add_dto_edges(graph: EvidenceGraph, endpoint: EvidenceNode, raw_params: str, type_paths: Dict[str, str]) -> None:
    for raw in split_arguments(raw_params):
        type_name, variable, _ = parse_java_parameter(raw)
        normalized = normalize_type(type_name)
        if not normalized or normalized not in type_paths:
            continue
        if not normalized.endswith(("DTO", "Dto", "Request", "Response", "VO", "Vo")):
            continue
        dto = EvidenceNode(
            "dto:" + normalized,
            "dto_type",
            normalized,
            EvidenceLocation(type_paths[normalized], 1),
            {"variable": variable, "type": normalized},
        )
        graph.add_node(dto)
        graph.add_edge(EvidenceEdge(endpoint.node_id, dto.node_id, "accepts", "Controller 方法参数类型为 " + normalized, "high", endpoint.location, dto.location))


def _mapper_nodes_by_class_and_method(graph: EvidenceGraph) -> Dict[Tuple[str, str], List[str]]:
    lookup: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.kind != "mapper_method":
            continue
        lookup[(str(node.metadata.get("class", "")), str(node.metadata.get("method", "")))].append(node.node_id)
    return lookup
