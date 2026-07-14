"""Conservative fallback review for code outside specialized capabilities."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from ..types import ProjectMap, ReviewTask, SourceFile
from .base import Capability, CapabilityDetection, CapabilityResult


GENERIC_LANGUAGES = {"c", "cpp", "go", "python", "rust", "java", "javascript", "typescript", "vue"}
SENSITIVE_PATTERN = re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|access[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"'#]{4,})")
DANGEROUS_COMMAND_PATTERN = re.compile(r"\b(system|popen|exec\.Command|Runtime\.getRuntime\(\)\.exec|subprocess\.(run|call|Popen))\s*\(")


class GenericCapability(Capability):
    name = "Generic"
    specialized = False

    def detect(self, files: List[SourceFile]) -> CapabilityDetection:
        code_count = sum(1 for item in files if item.language in GENERIC_LANGUAGES)
        return CapabilityDetection(self.name, 0.1 if code_count else 0.0, "{0} 个未归属代码文件可进行范型审查".format(code_count))

    def claim_files(self, files: List[SourceFile]) -> List[SourceFile]:
        return [item for item in files if item.language in GENERIC_LANGUAGES]

    def analyze(self, root: Path, files: List[SourceFile]) -> CapabilityResult:
        claimed = self.claim_files(files)
        roles: Dict[str, List[str]] = defaultdict(list)
        signals: List[Dict[str, str]] = []
        for source in claimed:
            roles["generic_code"].append(source.relative_path)
            if not _is_test_or_example(source):
                for match in SENSITIVE_PATTERN.finditer(source.content):
                    signals.append(_signal(source, match.start(), "generic_plaintext_secret", "代码中疑似存在硬编码敏感值"))
            for match in DANGEROUS_COMMAND_PATTERN.finditer(source.content):
                signals.append(_signal(source, match.start(), "generic_command_execution", "发现可能执行系统命令的调用"))
            if source.line_count > 300:
                signals.append({"file": source.relative_path, "line": "1", "kind": "generic_large_file", "message": "文件超过 300 行，需要关注职责和可维护性"})

        project = ProjectMap(
            root=root,
            files=claimed,
            technologies=["通用范型审查"],
            roles=dict(roles),
            routes=[], api_calls=[], sql_operations=[], config_findings=[], relations=[], signals=signals,
        )
        tasks: List[ReviewTask] = []
        for index, batch in enumerate(_chunks([item.relative_path for item in claimed], 6), start=1):
            tasks.append(ReviewTask(
                "generic.foundation.{0}".format(index),
                "通用安全、可靠性与结构", 70 + index, batch, [],
                [
                    "仅根据提供代码审查硬编码敏感信息、危险命令拼接、异常处理、资源生命周期、复杂度和重复逻辑。",
                    "不得假设未知框架行为；语言或框架特定判断必须标为需人工确认。",
                    "测试、fixture、example、demo 路径中的演示凭据不作为生产密钥报告，除非提供的调用路径证明会进入运行环境。",
                    "安全结论必须说明输入来源、危险调用和可证实影响。",
                ],
                "能力库未覆盖代码的保守范型审查",
            ))
        return CapabilityResult(self.name, project, tasks, [item.relative_path for item in claimed], self.detect(claimed).reason)


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def _signal(source: SourceFile, index: int, kind: str, message: str) -> Dict[str, str]:
    return {"file": source.relative_path, "line": str(source.content.count("\n", 0, index) + 1), "kind": kind, "message": message}


def _is_test_or_example(source: SourceFile) -> bool:
    parts = [part.lower() for part in Path(source.relative_path).parts]
    markers = {"test", "tests", "fixture", "fixtures", "example", "examples", "demo"}
    return any(part in markers or part.startswith("test_") or part.endswith("_test") for part in parts)
