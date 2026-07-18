"""Lightweight Java type index used to prove common Spring and MyBatis-Plus relations."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...types import SourceFile
from .source_utils import find_matching, line_of, mask_comments_and_strings, normalize_type, parse_java_parameter, split_arguments


TYPE_HEADER_PATTERN = re.compile(r"\b(class|interface)\s+(\w+)\s*([^\{]*)\{")
METHOD_SIGNATURE_PATTERN = re.compile(
    r"\b(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*"
    r"(?P<return>[\w.<>?,\[\]\s]+?)\s+(?P<name>\w+)\s*"
    r"\((?P<params>(?:[^()]|\([^()]*\))*)\)\s*(?P<terminal>\{|;)",
    re.S,
)
TABLE_NAME_PATTERN = re.compile(r"@TableName\s*\(\s*[\"']([^\"']+)")
CREATE_TABLE_PATTERN = re.compile(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?([\w.]+)", re.I)


@dataclass
class JavaMethodInfo:
    owner: str
    name: str
    parameter_types: List[str]
    file: str
    line: int
    has_body: bool

    @property
    def arity(self) -> int:
        return len(self.parameter_types)


@dataclass
class JavaTypeInfo:
    name: str
    kind: str
    package: str
    file: str
    line: int
    extends: List[str] = field(default_factory=list)
    implements: List[str] = field(default_factory=list)
    generic_arguments: Dict[str, List[str]] = field(default_factory=dict)
    methods: List[JavaMethodInfo] = field(default_factory=list)
    annotations: str = ""
    table_name: str = ""


@dataclass(frozen=True)
class ServiceBinding:
    mapper_type: str
    entity_type: str
    confidence: str
    evidence: str


class JavaSemanticIndex:
    """Index only the type facts needed by the review graph; it is not a compiler."""

    def __init__(self, files: List[SourceFile]):
        self.types: Dict[str, JavaTypeInfo] = {}
        self._implementations: Dict[str, List[str]] = {}
        self._ddl_tables = set()
        self._build(files)

    def type_info(self, type_name: str) -> Optional[JavaTypeInfo]:
        return self.types.get(normalize_type(type_name))

    def implementations_of(self, interface_name: str) -> List[str]:
        return list(self._implementations.get(normalize_type(interface_name), []))

    def resolve_method_owners(self, declared_type: str, method_name: str, arity: int) -> List[str]:
        normalized = normalize_type(declared_type)
        direct = self.types.get(normalized)
        if direct and direct.kind == "class" and self._has_body_method(direct, method_name, arity):
            return [direct.name]
        candidates = []
        for implementation in self.implementations_of(normalized):
            info = self.types.get(implementation)
            if info and self._has_body_method(info, method_name, arity):
                candidates.append(info.name)
        return list(dict.fromkeys(candidates))

    def interface_declares(self, type_name: str, method_name: str, arity: int) -> bool:
        info = self.type_info(type_name)
        return bool(info and any(method.name == method_name and method.arity == arity for method in info.methods))

    def service_binding(self, type_name: str) -> Optional[ServiceBinding]:
        normalized = normalize_type(type_name)
        direct = self._direct_service_binding(normalized)
        if direct:
            return direct
        bindings = [self._direct_service_binding(name) for name in self.implementations_of(normalized)]
        bindings = [item for item in bindings if item is not None]
        if len(bindings) == 1:
            item = bindings[0]
            return ServiceBinding(item.mapper_type, item.entity_type, item.confidence, "接口只有一个可见 ServiceImpl 实现；" + item.evidence)
        info = self.types.get(normalized)
        if info:
            for parent in info.extends:
                if parent == "IService":
                    args = info.generic_arguments.get(parent, [])
                    if args:
                        return ServiceBinding("", args[0], "medium", "Service 接口继承 IService<{}>".format(args[0]))
        return None

    def implicit_receivers(self, class_name: str) -> Dict[str, str]:
        result = {"this": normalize_type(class_name)}
        binding = self._direct_service_binding(normalize_type(class_name))
        if binding and binding.mapper_type:
            result["baseMapper"] = binding.mapper_type
        return result

    def is_feign_client(self, type_name: str) -> bool:
        info = self.type_info(type_name)
        return bool(info and "@FeignClient" in info.annotations)

    def table_for_entity(self, entity_type: str) -> Tuple[str, str, str]:
        info = self.type_info(entity_type)
        if info and info.table_name:
            return info.table_name, "high", "Entity 使用 @TableName(\"{}\")".format(info.table_name)
        convention = _snake_case(normalize_type(entity_type))
        if convention in self._ddl_tables:
            return convention, "medium", "DDL 中存在与 Entity 命名约定一致的表"
        return convention, "medium", "根据 Entity 类名的 MyBatis-Plus 默认命名约定推导"

    def _build(self, files: List[SourceFile]) -> None:
        for source in files:
            if source.language == "sql":
                self._ddl_tables.update(match.group(1).split(".")[-1].lower() for match in CREATE_TABLE_PATTERN.finditer(source.content))
            if source.language != "java":
                continue
            self._index_java_file(source)
        for info in self.types.values():
            for interface_name in info.implements:
                self._implementations.setdefault(interface_name, []).append(info.name)

    def _index_java_file(self, source: SourceFile) -> None:
        masked = mask_comments_and_strings(source.content)
        header = TYPE_HEADER_PATTERN.search(masked)
        if not header:
            return
        kind, name = header.group(1), header.group(2)
        original_tail = source.content[header.start(3):header.end(3)]
        package_match = re.search(r"\bpackage\s+([\w.]+)\s*;", source.content)
        prefix = source.content[max(0, header.start() - 1600):header.start()]
        extends, implements, generics = _parse_relations(kind, original_tail)
        table_match = TABLE_NAME_PATTERN.search(prefix)
        info = JavaTypeInfo(
            name,
            kind,
            package_match.group(1) if package_match else "",
            source.relative_path,
            line_of(source.content, header.start()),
            extends,
            implements,
            generics,
            [],
            prefix,
            table_match.group(1) if table_match else "",
        )
        opening = header.end() - 1
        closing = find_matching(masked, opening)
        body_end = closing if closing is not None else len(source.content)
        for match in METHOD_SIGNATURE_PATTERN.finditer(masked, opening + 1, body_end):
            method_name = match.group("name")
            if method_name == name or method_name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                continue
            raw_params = source.content[match.start("params"):match.end("params")]
            parameter_types = []
            for parameter in split_arguments(raw_params):
                type_name, _, _ = parse_java_parameter(parameter)
                if type_name:
                    parameter_types.append(normalize_type(type_name))
            info.methods.append(JavaMethodInfo(
                name,
                method_name,
                parameter_types,
                source.relative_path,
                line_of(source.content, match.start()),
                match.group("terminal") == "{",
            ))
        self.types[name] = info

    def _direct_service_binding(self, type_name: str) -> Optional[ServiceBinding]:
        info = self.types.get(type_name)
        if not info:
            return None
        for parent in info.extends:
            if parent == "ServiceImpl":
                args = info.generic_arguments.get(parent, [])
                if len(args) >= 2:
                    return ServiceBinding(args[0], args[1], "high", "类继承 ServiceImpl<{0}, {1}>".format(args[0], args[1]))
        return None

    @staticmethod
    def _has_body_method(info: JavaTypeInfo, method_name: str, arity: int) -> bool:
        return any(method.name == method_name and method.arity == arity and method.has_body for method in info.methods)


def _parse_relations(kind: str, tail: str) -> Tuple[List[str], List[str], Dict[str, List[str]]]:
    extends: List[str] = []
    implements: List[str] = []
    generics: Dict[str, List[str]] = {}
    extends_match = re.search(r"\bextends\s+(.+?)(?=\bimplements\b|$)", tail, re.S)
    if extends_match:
        values = split_arguments(extends_match.group(1)) if kind == "interface" else [extends_match.group(1).strip()]
        for value in values:
            raw, args = _type_reference(value)
            if raw:
                extends.append(raw)
                generics[raw] = args
    implements_match = re.search(r"\bimplements\s+(.+)$", tail, re.S)
    if implements_match:
        for value in split_arguments(implements_match.group(1)):
            raw, args = _type_reference(value)
            if raw:
                implements.append(raw)
                generics[raw] = args
    return extends, implements, generics


def _type_reference(value: str) -> Tuple[str, List[str]]:
    cleaned = " ".join(value.split()).strip()
    match = re.match(r"([\w.]+)\s*(?:<(.*)>)?$", cleaned, re.S)
    if not match:
        return normalize_type(cleaned), []
    raw = normalize_type(match.group(1))
    args = [normalize_type(item) for item in split_arguments(match.group(2) or "")]
    return raw, args


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
