"""Deep review capability for Spring Boot + MyBatis + Nacos + Vue projects."""

from pathlib import Path
from typing import List

from ..planner import build_review_plan
from ..project_map import build_project_map
from ..types import SourceFile
from .base import Capability, CapabilityDetection, CapabilityResult


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
        claimed = self.claim_files(files)
        tasks = build_review_plan(project)
        return CapabilityResult(self.name, project, tasks, [item.relative_path for item in claimed], self.detect(files).reason)
