"""Extract verifiable project facts before asking a model to reason about them."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .types import ProjectMap, Relation, SourceFile


ROUTE_PATTERN = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\(\s*)?(?:value\s*=\s*)?[\"']?([^\"')\s,}]+)?",
    re.MULTILINE,
)
CLASS_PATTERN = re.compile(r"\b(?:class|interface)\s+(\w+)")
INJECTION_PATTERN = re.compile(r"(?:private|protected)\s+(?:final\s+)?(\w+)\s+(\w+)\s*;")
MAPPER_METHOD_PATTERN = re.compile(r"\b(?:public\s+)?[\w<>?,\s\[\]]+\s+(\w+)\s*\([^;{}]*\)\s*;")
SQL_TAG_PATTERN = re.compile(r"<(select|insert|update|delete)\b[^>]*(?:id=[\"']([^\"']+)[\"'])?[^>]*>(.*?)</\1>", re.I | re.S)
SQL_ANNOTATION_PATTERN = re.compile(r"@(Select|Insert|Update|Delete)\s*\(\s*[\"'](.+?)[\"']\s*\)", re.S)
AXIOS_PATTERN = re.compile(r"(?:axios|request|http)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*[`\"']([^`\"']+)", re.I)
FETCH_PATTERN = re.compile(r"fetch\s*\(\s*[`\"']([^`\"']+)")
SENSITIVE_KEY_PATTERN = re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|access[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"'#]{4,})")


def _line_of(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _role_for_java(source: SourceFile) -> str:
    content = source.content
    if "@RestController" in content or "@Controller" in content:
        return "controller"
    if "@Service" in content:
        return "service"
    if "@Mapper" in content or source.relative_path.endswith("Mapper.java"):
        return "mapper"
    if "@Entity" in content or "@TableName" in content:
        return "entity"
    if source.relative_path.endswith(("DTO.java", "VO.java", "Request.java", "Response.java")):
        return "dto"
    return "java_other"


def _technologies(files: Iterable[SourceFile]) -> List[str]:
    all_content = "\n".join(item.content for item in files)
    tech: List[str] = []
    if "@SpringBootApplication" in all_content or "spring-boot" in all_content.lower():
        tech.append("Spring Boot")
    if "org.apache.ibatis" in all_content or "<mapper" in all_content or "@Mapper" in all_content:
        tech.append("MyBatis")
    if "nacos" in all_content.lower():
        tech.append("Nacos")
    if any(item.language == "vue" for item in files):
        tech.append("Vue")
    if any(item.language == "sql" for item in files) or "mysql" in all_content.lower():
        tech.append("MySQL")
    return tech or ["未识别的项目类型"]


def build_project_map(root: Path, files: List[SourceFile]) -> ProjectMap:
    roles: Dict[str, List[str]] = defaultdict(list)
    routes: List[Dict[str, str]] = []
    api_calls: List[Dict[str, str]] = []
    sql_operations: List[Dict[str, str]] = []
    config_findings: List[Dict[str, str]] = []
    relations: List[Relation] = []
    signals: List[Dict[str, str]] = []
    class_to_path: Dict[str, str] = {}

    for source in files:
        if source.language == "java":
            role = _role_for_java(source)
            roles[role].append(source.relative_path)
            match = CLASS_PATTERN.search(source.content)
            if match:
                class_to_path[match.group(1)] = source.relative_path
        elif source.language == "vue":
            roles["vue_component"].append(source.relative_path)
        elif source.language in {"javascript", "typescript"}:
            roles["frontend_script"].append(source.relative_path)
        elif source.language == "sql":
            roles["sql_file"].append(source.relative_path)
        elif source.language == "xml" and "<mapper" in source.content:
            roles["mapper_xml"].append(source.relative_path)
        elif source.language == "yaml":
            roles["configuration"].append(source.relative_path)

    for source in files:
        content = source.content
        if source.language == "java":
            for match in ROUTE_PATTERN.finditer(content):
                routes.append({
                    "file": source.relative_path,
                    "line": str(_line_of(content, match.start())),
                    "method": match.group(1).upper(),
                    "path": match.group(2) or "",
                })
            owner = next((name for name, path in class_to_path.items() if path == source.relative_path), "")
            for match in INJECTION_PATTERN.finditer(content):
                dependency = match.group(1)
                if dependency in class_to_path:
                    relations.append(Relation(source.relative_path, class_to_path[dependency], "injects", dependency))
                if owner and dependency.endswith("Mapper") and _role_for_java(source) == "controller":
                    signals.append(_signal(source, match.start(), "controller_direct_mapper", "Controller 直接注入 Mapper"))
            if "@Transactional" not in content and re.search(r"\b(insert|update|delete|save|remove)\w*\s*\(", content, re.I):
                if _role_for_java(source) == "service":
                    signals.append(_signal(source, 0, "write_without_transaction", "Service 包含写操作但未发现 @Transactional"))
        if source.language in {"xml", "sql", "java"}:
            index = content.find("${")
            if index >= 0:
                signals.append(_signal(source, index, "mybatis_dollar_placeholder", "发现 MyBatis ${} 字符串替换"))
            for match in SQL_TAG_PATTERN.finditer(content):
                sql_operations.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "operation": match.group(1).lower(), "statement": _compact(match.group(3))})
            for match in SQL_ANNOTATION_PATTERN.finditer(content):
                sql_operations.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "operation": match.group(1).lower(), "statement": _compact(match.group(2))})
            for match in re.finditer(r"(?i)(?:select|update|delete|insert)\s+[^\n;]+", content):
                statement = match.group(0)
                if "select" in statement.lower() or source.language == "sql":
                    sql_operations.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "operation": statement.split()[0].lower(), "statement": _compact(statement)})
            for match in re.finditer(r"(?i)(select|update|delete)\s+[^\n;]*[\"']\s*\+", content):
                signals.append(_signal(source, match.start(), "sql_string_concatenation", "疑似拼接 SQL 字符串"))
            for match in re.finditer(r"(?i)\bselect\s+\*\b", content):
                signals.append(_signal(source, match.start(), "select_star", "发现 SELECT *"))

        if source.language in {"vue", "javascript", "typescript"}:
            for match in AXIOS_PATTERN.finditer(content):
                api_calls.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "method": match.group(1).upper(), "path": match.group(2)})
            for match in FETCH_PATTERN.finditer(content):
                api_calls.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "method": "FETCH", "path": match.group(1)})
            for match in re.finditer(r"v-html\s*=", content):
                signals.append(_signal(source, match.start(), "vue_v_html", "发现 Vue v-html 动态渲染"))

        if source.language == "yaml":
            for match in SENSITIVE_KEY_PATTERN.finditer(content):
                config_findings.append({"file": source.relative_path, "line": str(_line_of(content, match.start())), "key": match.group(1), "value": "[已脱敏]"})
                signals.append(_signal(source, match.start(), "plaintext_secret", "配置中疑似存在明文敏感值"))

    # Cross-stack links are heuristic facts, deliberately labeled as name/path matches.
    for call in api_calls:
        normalized = call["path"].split("?")[0].rstrip("/")
        for route in routes:
            route_path = route["path"].rstrip("/")
            if normalized and route_path and (normalized.endswith(route_path) or route_path.endswith(normalized)):
                relations.append(Relation(call["file"], route["file"], "calls_route", "{0} -> {1}".format(call["path"], route["path"])))

    return ProjectMap(
        root=root,
        files=files,
        technologies=_technologies(files),
        roles=dict(roles),
        routes=routes,
        api_calls=api_calls,
        sql_operations=sql_operations,
        config_findings=config_findings,
        relations=relations,
        signals=signals,
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:500]


def _signal(source: SourceFile, index: int, kind: str, message: str) -> Dict[str, str]:
    return {"file": source.relative_path, "line": str(_line_of(source.content, index)), "kind": kind, "message": message}
