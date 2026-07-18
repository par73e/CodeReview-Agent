"""Shared value objects used across the review pipeline."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SourceFile:
    path: Path
    relative_path: str
    language: str
    content: str
    line_count: int


@dataclass
class Relation:
    source: str
    target: str
    kind: str
    evidence: str


@dataclass
class ProjectMap:
    root: Path
    files: List[SourceFile]
    technologies: List[str]
    roles: Dict[str, List[str]]
    routes: List[Dict[str, str]]
    api_calls: List[Dict[str, str]]
    sql_operations: List[Dict[str, str]]
    config_findings: List[Dict[str, str]]
    relations: List[Relation]
    signals: List[Dict[str, str]]
    analysis_summary: Dict[str, object] = field(default_factory=dict)


@dataclass
class ReviewTask:
    task_id: str
    domain: str
    priority: int
    target_paths: List[str]
    related_paths: List[str]
    checklist: List[str]
    rationale: str
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class Issue:
    category: str
    severity: str
    title: str
    file: str
    line: Optional[int]
    evidence: str
    trigger_path: str
    impact: str
    recommendation: str
    confidence: str
    needs_human_confirmation: bool = False
    review_status: str = "initial"
    task_id: str = ""
    chain_id: str = ""
    endpoint: str = ""
    chain_status: str = ""
    chain_path: str = ""
    chain_gaps: List[str] = field(default_factory=list)
    affected_endpoints: List[str] = field(default_factory=list)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ReviewResult:
    project: ProjectMap
    tasks: List[ReviewTask]
    issues: List[Issue] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    failed_tasks: List[str] = field(default_factory=list)
    uncovered: List[str] = field(default_factory=list)
