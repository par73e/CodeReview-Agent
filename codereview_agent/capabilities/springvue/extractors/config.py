"""Extract visible datasource, MyBatis, profile, and Nacos configuration facts."""

import re
from typing import List, Tuple

from ....types import SourceFile
from ..evidence import EvidenceEdge, EvidenceGraph, EvidenceLocation, EvidenceNode
from ..source_utils import line_of, stable_id


RELEVANT_MARKERS = ("spring.profiles", "spring.config.import", "nacos", "datasource", "mybatis", "mapper-locations")
SENSITIVE_MARKERS = ("password", "secret", "token", "api-key", "apikey", "access-key")


def extract_config(files: List[SourceFile], graph: EvidenceGraph) -> None:
    for source in files:
        if source.language != "yaml":
            continue
        source_node = EvidenceNode(
            "config_source:" + source.relative_path,
            "config_source",
            source.relative_path,
            EvidenceLocation(source.relative_path, 1, source.line_count),
            {"profile": _profile_from_path(source.relative_path)},
        )
        graph.add_node(source_node)
        for key, value, index in _flatten_yaml(source.content):
            normalized = key.lower()
            if not any(marker in normalized for marker in RELEVANT_MARKERS):
                continue
            line = line_of(source.content, index)
            sensitive = any(marker in normalized for marker in SENSITIVE_MARKERS)
            displayed_value = "[已脱敏]" if sensitive and value else value
            node = EvidenceNode(
                stable_id("config", source.relative_path, line, key),
                "config_key",
                key,
                EvidenceLocation(source.relative_path, line),
                {
                    "key": key,
                    "value": displayed_value,
                    "sensitive": sensitive,
                    "remote_reference": "nacos:" in value.lower() or "nacos" in normalized,
                },
            )
            graph.add_node(node)
            graph.add_edge(EvidenceEdge(node.node_id, source_node.node_id, "provided_by", "配置项定义于该 YAML 文件", "high", node.location, source_node.location))


def _flatten_yaml(content: str) -> List[Tuple[str, str, int]]:
    stack: List[Tuple[int, str]] = []
    results: List[Tuple[str, str, int]] = []
    offset = 0
    for raw_line in content.splitlines(True):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            offset += len(raw_line)
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, value = stripped.split(":", 1)
        key = key.strip().strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        full_key = ".".join([item[1] for item in stack] + [key])
        results.append((full_key, value.strip().strip("'\""), offset))
        if not value.strip():
            stack.append((indent, key))
        offset += len(raw_line)
    return results


def _profile_from_path(path: str) -> str:
    match = re.search(r"application-([\w-]+)\.ya?ml$", path, re.I)
    return match.group(1) if match else ""
