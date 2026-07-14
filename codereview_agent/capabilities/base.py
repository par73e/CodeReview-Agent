"""Shared contracts for stack-specific review capabilities."""

from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..types import ProjectMap, ReviewTask, SourceFile


@dataclass
class CapabilityDetection:
    name: str
    score: float
    reason: str


@dataclass
class CapabilityResult:
    name: str
    project: ProjectMap
    tasks: List[ReviewTask]
    claimed_paths: List[str]
    reason: str


class Capability:
    """A self-contained analyzer and planner for one technology-stack family."""

    name = "capability"
    specialized = True

    def detect(self, files: List[SourceFile]) -> CapabilityDetection:
        raise NotImplementedError

    def claim_files(self, files: List[SourceFile]) -> List[SourceFile]:
        raise NotImplementedError

    def analyze(self, root: Path, files: List[SourceFile]) -> CapabilityResult:
        raise NotImplementedError
