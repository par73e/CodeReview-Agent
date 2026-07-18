"""Extract frontend HTTP calls as evidence nodes."""

import re
from typing import List

from ....types import SourceFile
from ..evidence import EvidenceEdge, EvidenceGraph, EvidenceLocation, EvidenceNode
from ..source_utils import find_matching, line_of, mask_comments_and_strings, normalize_url, split_arguments, stable_id


HTTP_CALL_PATTERN = re.compile(r"(?:axios|request|http)\s*\.\s*(get|post|put|delete|patch)\s*\(", re.I)
FETCH_PATTERN = re.compile(r"\bfetch\s*\(", re.I)
OBJECT_REQUEST_PATTERN = re.compile(r"\brequest\s*\(\s*\{", re.I)


def extract_frontend(files: List[SourceFile], graph: EvidenceGraph) -> None:
    for source in files:
        if source.language not in {"vue", "javascript", "typescript"}:
            continue
        seen = set()
        masked = mask_comments_and_strings(source.content)
        for match in HTTP_CALL_PATTERN.finditer(masked):
            arguments = _call_arguments(source.content, masked, match.end() - 1)
            if arguments:
                url, dynamic = _parse_url_expression(arguments[0])
                if url:
                    _add_call(source, graph, match.start(), match.group(1).upper(), url, seen, dynamic)
        for match in FETCH_PATTERN.finditer(masked):
            arguments = _call_arguments(source.content, masked, match.end() - 1)
            if arguments:
                url, dynamic = _parse_url_expression(arguments[0])
                if url:
                    _add_call(source, graph, match.start(), "FETCH", url, seen, dynamic)
        for match in OBJECT_REQUEST_PATTERN.finditer(masked):
            opening = masked.find("{", match.start(), match.end())
            closing = find_matching(masked, opening)
            if closing is None:
                continue
            body = source.content[opening + 1:closing]
            url_match = re.search(r"\burl\s*:\s*(.+?)(?=,\s*\w+\s*:|$)", body, re.S)
            method_match = re.search(r"\bmethod\s*:\s*([\"'])(\w+)\1", body, re.I)
            if url_match:
                url, dynamic = _parse_url_expression(url_match.group(1))
                if url:
                    method = method_match.group(2).upper() if method_match else "REQUEST"
                    _add_call(source, graph, match.start(), method, url, seen, dynamic)
    api_functions = {
        str(node.metadata.get("function")): node
        for node in graph.nodes.values()
        if node.kind == "api_call" and node.metadata.get("function")
    }
    for source in files:
        if source.language != "vue":
            continue
        _extract_actions(source, graph, api_functions)


def _add_call(source: SourceFile, graph: EvidenceGraph, index: int, method: str, url: str, seen: set, dynamic: bool = False) -> None:
    line = line_of(source.content, index)
    identity = (line, method, url)
    if identity in seen:
        return
    seen.add(identity)
    dynamic = dynamic or bool(re.search(r"\$\{", url))
    function_name = _nearest_function_name(source.content, index)
    display = "{0} {1}".format(method, url)
    node = EvidenceNode(
        stable_id("api", source.relative_path, line, display),
        "api_call",
        display,
        EvidenceLocation(source.relative_path, line),
        {
            "method": method,
            "url": url,
            "normalized_url": normalize_url(url),
            "function": function_name,
            "dynamic": dynamic,
            "confidence": "high" if _known_url_shape(url) else "medium",
        },
    )
    graph.add_node(node)


def _call_arguments(content: str, masked: str, opening: int) -> List[str]:
    closing = find_matching(masked, opening, "(", ")")
    if closing is None:
        return []
    return split_arguments(content[opening + 1:closing])


def _parse_url_expression(expression: str):
    value = expression.strip()
    if not value:
        return "", False
    if value.startswith("`") and value.endswith("`"):
        inner = value[1:-1]
        dynamic = bool(re.search(r"\$\{[^}]+\}", inner))
        return re.sub(r"\$\{[^}]+\}", "{}", inner), dynamic
    parts = _split_concatenation(value)
    if len(parts) > 1:
        rendered = []
        dynamic = False
        for part in parts:
            part = part.strip()
            if len(part) >= 2 and part[0] in {"'", '"'} and part[-1] == part[0]:
                rendered.append(_decode_js_string(part[1:-1]))
            else:
                rendered.append("{}")
                dynamic = True
        return "".join(rendered), dynamic
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return _decode_js_string(value[1:-1]), False
    return "", True


def _split_concatenation(value: str) -> List[str]:
    parts = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1
        elif character == "+" and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return [item for item in parts if item.strip()]


def _decode_js_string(value: str) -> str:
    return value.replace(r"\/", "/").replace(r"\'", "'").replace(r'\"', '"').replace(r"\\", "\\")


def _known_url_shape(url: str) -> bool:
    return bool(url.startswith("/") and not re.search(r"\b(?:undefined|null)\b", url))


def _nearest_function_name(content: str, index: int) -> str:
    prefix = content[max(0, index - 600):index]
    matches = list(re.finditer(
        r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(|(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
        prefix,
    ))
    if not matches:
        return ""
    match = matches[-1]
    return match.group(1) or match.group(2) or ""


def _extract_actions(source: SourceFile, graph: EvidenceGraph, api_functions) -> None:
    masked = mask_comments_and_strings(source.content)
    pattern = re.compile(r"(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{")
    for match in pattern.finditer(masked):
        closing = find_matching(masked, match.end() - 1)
        if closing is None:
            continue
        body = source.content[match.end():closing]
        called = [name for name in api_functions if re.search(r"\b{0}\s*\(".format(re.escape(name)), body)]
        if not called:
            continue
        line = line_of(source.content, match.start())
        action = EvidenceNode(
            stable_id("action", source.relative_path, line, match.group(1)),
            "frontend_action",
            source.path.stem + "." + match.group(1),
            EvidenceLocation(source.relative_path, line, line_of(source.content, closing)),
            {"function": match.group(1)},
        )
        graph.add_node(action)
        for function_name in called:
            api_call = api_functions[function_name]
            graph.add_edge(EvidenceEdge(action.node_id, api_call.node_id, "initiates", "Vue 方法调用 API 封装函数 " + function_name, "high", action.location, api_call.location))
