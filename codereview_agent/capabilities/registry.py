"""Select capabilities, allocate files, and merge their independent analyses."""

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

from ..types import ProjectMap, Relation, ReviewTask, SourceFile
from .base import Capability, CapabilityDetection, CapabilityResult
from .generic import GenericCapability, GENERIC_LANGUAGES
from .spring_vue import SpringVueCapability


@dataclass
class CapabilitySelection:
    name: str
    score: float
    reason: str
    claimed_paths: List[str] = field(default_factory=list)
    status: str = "已启用"


@dataclass
class CapabilityRun:
    project: ProjectMap
    tasks: List[ReviewTask]
    selections: List[CapabilitySelection]
    uncovered: List[str]
    failures: List[str]


class CapabilityRegistry:
    def __init__(self, capabilities: Iterable[Capability], threshold: float = 0.5):
        self.capabilities = list(capabilities)
        self.threshold = threshold

    def analyze(self, root: Path, files: List[SourceFile]) -> CapabilityRun:
        specializations = [item for item in self.capabilities if item.specialized]
        fallback = next((item for item in self.capabilities if not item.specialized), GenericCapability())
        detections = [(item, item.detect(files)) for item in specializations]
        selected = sorted(((item, detection) for item, detection in detections if detection.score >= self.threshold), key=lambda pair: pair[1].score, reverse=True)

        claimed = set()
        results: List[CapabilityResult] = []
        selections: List[CapabilitySelection] = []
        failures: List[str] = []
        for capability, detection in selected:
            # A specialization only ever sees files that are still unowned.
            # This makes the registry extensible: Java, Go, C++ and other
            # modules can be selected together without reviewing the same file
            # twice or depending on a Spring-first control flow.
            available = [item for item in files if item.relative_path not in claimed]
            owned_files = capability.claim_files(available)
            if not owned_files:
                selections.append(CapabilitySelection(
                    capability.name,
                    detection.score,
                    detection.reason + "；没有可接管文件（已由优先模块覆盖）",
                    [],
                    "未接管",
                ))
                continue
            try:
                result = capability.analyze(root, owned_files)
                # File ownership is allocated by the registry, not trusted to
                # each implementation's internal analysis result.
                result.claimed_paths = [item.relative_path for item in owned_files]
                claimed.update(result.claimed_paths)
                results.append(result)
                selections.append(CapabilitySelection(capability.name, detection.score, detection.reason, result.claimed_paths))
            except Exception as error:  # Capability failure must not block fallback review.
                failures.append("{0} 模块分析失败：{1}".format(capability.name, error))
                selections.append(CapabilitySelection(capability.name, detection.score, detection.reason, [], "已降级"))

        remaining = [item for item in files if item.relative_path not in claimed and item.language in GENERIC_LANGUAGES]
        if remaining:
            try:
                fallback_result = fallback.analyze(root, remaining)
                results.append(fallback_result)
                selections.append(CapabilitySelection(fallback.name, fallback.detect(remaining).score, fallback_result.reason, fallback_result.claimed_paths))
                claimed.update(fallback_result.claimed_paths)
            except Exception as error:
                failures.append("{0} 模块分析失败：{1}".format(fallback.name, error))

        project = _merge_projects(root, files, results)
        tasks = [task for result in results for task in result.tasks]
        unclaimed = [item.relative_path for item in files if item.language in GENERIC_LANGUAGES and item.relative_path not in claimed]
        return CapabilityRun(project, tasks, selections, ["未被能力模块覆盖：" + path for path in unclaimed], failures)


def build_default_registry() -> CapabilityRegistry:
    return CapabilityRegistry([SpringVueCapability(), GenericCapability()])


def _merge_projects(root: Path, files: List[SourceFile], results: List[CapabilityResult]) -> ProjectMap:
    roles: Dict[str, List[str]] = defaultdict(list)
    technologies: List[str] = []
    routes: List[Dict[str, str]] = []
    api_calls: List[Dict[str, str]] = []
    sql_operations: List[Dict[str, str]] = []
    config_findings: List[Dict[str, str]] = []
    relations: List[Relation] = []
    signals: List[Dict[str, str]] = []
    analysis_summary: Dict[str, object] = {}
    for result in results:
        project = result.project
        for technology in project.technologies:
            if technology not in technologies:
                technologies.append(technology)
        for role, paths in project.roles.items():
            roles[role].extend(path for path in paths if path not in roles[role])
        routes.extend(project.routes)
        api_calls.extend(project.api_calls)
        sql_operations.extend(project.sql_operations)
        config_findings.extend(project.config_findings)
        relations.extend(project.relations)
        signals.extend(project.signals)
        analysis_summary.update(project.analysis_summary)
    return ProjectMap(root, files, technologies or ["未识别的项目类型"], dict(roles), routes, api_calls, sql_operations, config_findings, relations, signals, analysis_summary)
