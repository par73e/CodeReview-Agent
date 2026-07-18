"""Extract MyBatis Mapper methods and SQL mappings."""

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ....types import SourceFile
from ..evidence import EvidenceEdge, EvidenceGraph, EvidenceLocation, EvidenceNode
from ..java_index import JavaSemanticIndex
from ..source_utils import find_matching, line_of, mask_comments_and_strings, normalize_type, parse_java_parameter, split_arguments, stable_id


CLASS_PATTERN = re.compile(r"\b(?:interface|class)\s+(\w+)")
PACKAGE_PATTERN = re.compile(r"\bpackage\s+([\w.]+)\s*;")
DECLARATION_PATTERN = re.compile(
    r"(?P<annotations>(?:\s*@\w+(?:\s*\([^;]*?\))?\s*)*)"
    r"\b(?P<return>[\w.<>?,\[\]\s]+?)\s+(?P<name>\w+)\s*\((?P<params>(?:[^()]|\([^()]*\))*)\)\s*;",
    re.S,
)
MAPPER_TAG_PATTERN = re.compile(r"<mapper\b[^>]*namespace=[\"']([^\"']+)[\"'][^>]*>", re.I)
SQL_TAG_PATTERN = re.compile(r"<(select|insert|update|delete)\b([^>]*)>(.*?)</\1>", re.I | re.S)
ANNOTATION_SQL_START = re.compile(r"@(Select|Insert|Update|Delete)\s*\(", re.I)
JAVA_STRING_PATTERN = re.compile(r'"((?:\\.|[^"\\])*)"', re.S)


def extract_mybatis(files: List[SourceFile], graph: EvidenceGraph, index: Optional[JavaSemanticIndex] = None) -> None:
    del index
    mapper_methods: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for source in files:
        if source.language != "java":
            continue
        class_match = CLASS_PATTERN.search(source.content)
        if not class_match:
            continue
        class_name = class_match.group(1)
        if "@Mapper" not in source.content and not class_name.endswith("Mapper"):
            continue
        package_match = PACKAGE_PATTERN.search(source.content)
        package_name = package_match.group(1) if package_match else ""
        namespace = package_name + "." + class_name if package_name else class_name
        masked = mask_comments_and_strings(source.content)
        for match in DECLARATION_PATTERN.finditer(masked):
            method_name = match.group("name")
            line = line_of(source.content, match.start())
            params = []
            raw_params = source.content[match.start("params"):match.end("params")]
            for raw in split_arguments(raw_params):
                type_name, variable, binding = parse_java_parameter(raw)
                if variable:
                    params.append({"type": normalize_type(type_name), "variable": variable, "binding": binding})
            node = EvidenceNode(
                stable_id("mapper", source.relative_path, line, class_name + "." + method_name),
                "mapper_method",
                class_name + "." + method_name,
                EvidenceLocation(source.relative_path, line),
                {
                    "class": class_name,
                    "namespace": namespace,
                    "method": method_name,
                    "params": params,
                    "return_type": " ".join(match.group("return").split()),
                },
            )
            graph.add_node(node)
            mapper_methods[(namespace, method_name)].append(node.node_id)
            annotation_sql = _annotation_sql_before(source.content, masked, match.start("return"))
            if annotation_sql:
                _add_annotation_sql(graph, node, source, match, annotation_sql)

    for source in files:
        if source.language != "xml" or "<mapper" not in source.content:
            continue
        namespace_match = MAPPER_TAG_PATTERN.search(source.content)
        if not namespace_match:
            graph.failures.append("MyBatis XML 缺少 namespace：" + source.relative_path)
            continue
        namespace = namespace_match.group(1)
        for match in SQL_TAG_PATTERN.finditer(source.content):
            attributes = match.group(2)
            id_match = re.search(r"\bid=[\"']([^\"']+)[\"']", attributes, re.I)
            if not id_match:
                continue
            statement_id = id_match.group(1)
            operation = match.group(1).lower()
            statement = match.group(3).strip()
            line = line_of(source.content, match.start())
            sql_node = EvidenceNode(
                stable_id("sql", source.relative_path, line, namespace + "." + statement_id),
                "sql_statement",
                namespace + "." + statement_id,
                EvidenceLocation(source.relative_path, line, line_of(source.content, match.end())),
                {
                    "namespace": namespace,
                    "statement_id": statement_id,
                    "operation": operation,
                    "statement": statement,
                    "placeholders": _placeholders(statement),
                    "source_kind": "xml",
                },
            )
            graph.add_node(sql_node)
            candidates = mapper_methods.get((namespace, statement_id), [])
            for mapper_id in candidates:
                mapper = graph.nodes[mapper_id]
                _record_binding_gaps(mapper, sql_node)
                graph.add_edge(EvidenceEdge(
                    mapper_id,
                    sql_node.node_id,
                    "maps_to_sql",
                    "Mapper namespace 与 XML namespace 一致，方法名与 SQL id 一致",
                    "high" if len(candidates) == 1 else "medium",
                    mapper.location,
                    sql_node.location,
                ))


def _add_annotation_sql(graph: EvidenceGraph, mapper: EvidenceNode, source: SourceFile, declaration, annotation_sql) -> None:
    operation, statement = annotation_sql
    line = line_of(source.content, declaration.start())
    sql_node = EvidenceNode(
        stable_id("sql", source.relative_path, line, mapper.name + ".annotation"),
        "sql_statement",
        mapper.name + " 注解 SQL",
        EvidenceLocation(source.relative_path, line),
        {
            "namespace": mapper.metadata.get("namespace", ""),
            "statement_id": mapper.metadata.get("method", ""),
            "operation": operation,
            "statement": statement,
            "placeholders": _placeholders(statement),
            "source_kind": "annotation",
        },
    )
    graph.add_node(sql_node)
    _record_binding_gaps(mapper, sql_node)
    graph.add_edge(EvidenceEdge(mapper.node_id, sql_node.node_id, "maps_to_sql", "Mapper 方法使用 MyBatis SQL 注解", "high", mapper.location, sql_node.location))


def _annotation_sql_before(content: str, masked: str, declaration_start: int):
    lower = masked.rfind(";", 0, declaration_start) + 1
    candidates = list(ANNOTATION_SQL_START.finditer(masked, lower, declaration_start))
    if not candidates:
        return None
    annotation = candidates[-1]
    opening = masked.find("(", annotation.start(), annotation.end() + 1)
    closing = find_matching(masked, opening, "(", ")")
    if closing is None or closing > declaration_start:
        return None
    arguments = content[opening + 1:closing]
    literals = JAVA_STRING_PATTERN.findall(arguments)
    if not literals:
        return None
    statement = "".join(_decode_java_string(value) for value in literals)
    return annotation.group(1).lower(), statement


def _decode_java_string(value: str) -> str:
    replacements = {
        r"\n": "\n", r"\r": "\r", r"\t": "\t",
        r'\"': '"', r"\\'": "'", r"\\\\": "\\",
    }
    for escaped, plain in replacements.items():
        value = value.replace(escaped, plain)
    return value


def _placeholders(statement: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"[#$]\{\s*([\w.]+)", statement)))


def _record_binding_gaps(mapper: EvidenceNode, sql_node: EvidenceNode) -> None:
    available = set()
    for item in mapper.metadata.get("params", []):
        if isinstance(item, dict):
            available.add(str(item.get("binding", "")))
            available.add(str(item.get("variable", "")))
    missing = []
    for placeholder in sql_node.metadata.get("placeholders", []):
        root = str(placeholder).split(".", 1)[0]
        if root and root not in available and len(available) > 1:
            missing.append(str(placeholder))
    sql_node.metadata["unmatched_placeholders"] = missing
