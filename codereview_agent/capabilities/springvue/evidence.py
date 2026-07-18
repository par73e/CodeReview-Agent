"""Evidence graph value objects for SpringVue endpoint data flows."""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


CONFIDENCE_LEVELS = {"high", "medium", "low"}
CHAIN_STATUSES = {"complete", "partial", "needs_confirmation"}


@dataclass(frozen=True)
class EvidenceLocation:
    file: str
    line: int
    end_line: int = 0


@dataclass
class EvidenceNode:
    node_id: str
    kind: str
    name: str
    location: EvidenceLocation
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class EvidenceEdge:
    source: str
    target: str
    kind: str
    evidence: str
    confidence: str
    source_location: Optional[EvidenceLocation] = None
    target_location: Optional[EvidenceLocation] = None
    model_fact: bool = True

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError("不支持的证据可信度：" + self.confidence)
        if self.confidence == "low":
            self.model_fact = False


@dataclass
class EvidenceGraph:
    nodes: Dict[str, EvidenceNode] = field(default_factory=dict)
    edges: List[EvidenceEdge] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    metrics: Dict[str, int] = field(default_factory=dict)

    def add_node(self, node: EvidenceNode) -> EvidenceNode:
        existing = self.nodes.get(node.node_id)
        if existing is None:
            self.nodes[node.node_id] = node
            return node
        return existing

    def add_edge(self, edge: EvidenceEdge) -> EvidenceEdge:
        key = (edge.source, edge.target, edge.kind)
        existing = next((item for item in self.edges if (item.source, item.target, item.kind) == key), None)
        if existing is None:
            self.edges.append(edge)
            return edge
        if _confidence_rank(edge.confidence) > _confidence_rank(existing.confidence):
            self.edges.remove(existing)
            self.edges.append(edge)
            return edge
        return existing

    def outgoing(self, node_id: str, kinds: Optional[Iterable[str]] = None, model_facts_only: bool = False) -> List[EvidenceEdge]:
        allowed = set(kinds or [])
        return [
            edge for edge in self.edges
            if edge.source == node_id
            and (not allowed or edge.kind in allowed)
            and (not model_facts_only or edge.model_fact)
        ]

    def incoming(self, node_id: str, kinds: Optional[Iterable[str]] = None, model_facts_only: bool = False) -> List[EvidenceEdge]:
        allowed = set(kinds or [])
        return [
            edge for edge in self.edges
            if edge.target == node_id
            and (not allowed or edge.kind in allowed)
            and (not model_facts_only or edge.model_fact)
        ]


@dataclass
class EndpointChain:
    chain_id: str
    endpoint: str
    node_ids: List[str]
    edges: List[EvidenceEdge]
    status: str
    gaps: List[str] = field(default_factory=list)
    config_node_ids: List[str] = field(default_factory=list)
    risk_score: int = 0
    risk_reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in CHAIN_STATUSES:
            raise ValueError("不支持的链路状态：" + self.status)


@dataclass
class ChainSummary:
    endpoint_count: int = 0
    frontend_call_count: int = 0
    complete_count: int = 0
    partial_count: int = 0
    needs_confirmation_count: int = 0
    unmatched_endpoint_count: int = 0
    unmatched_frontend_count: int = 0
    controller_business_call_count: int = 0
    resolved_controller_business_call_count: int = 0
    service_persistence_call_count: int = 0
    resolved_service_persistence_call_count: int = 0

    def as_dict(self) -> Dict[str, object]:
        return {
            "endpoint_count": self.endpoint_count,
            "frontend_call_count": self.frontend_call_count,
            "complete_count": self.complete_count,
            "partial_count": self.partial_count,
            "needs_confirmation_count": self.needs_confirmation_count,
            "unmatched_endpoint_count": self.unmatched_endpoint_count,
            "unmatched_frontend_count": self.unmatched_frontend_count,
            "controller_business_call_count": self.controller_business_call_count,
            "resolved_controller_business_call_count": self.resolved_controller_business_call_count,
            "service_persistence_call_count": self.service_persistence_call_count,
            "resolved_service_persistence_call_count": self.resolved_service_persistence_call_count,
            "frontend_match_rate": _ratio(self.frontend_call_count - self.unmatched_frontend_count, self.frontend_call_count),
            "controller_business_resolution_rate": _ratio(self.resolved_controller_business_call_count, self.controller_business_call_count),
            "service_persistence_resolution_rate": _ratio(self.resolved_service_persistence_call_count, self.service_persistence_call_count),
        }


def _confidence_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(value, -1)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 1.0
