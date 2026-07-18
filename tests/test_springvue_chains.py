import unittest
from pathlib import Path

from codereview_agent.capabilities.springvue.chain_builder import build_endpoint_chains, render_chain_path
from codereview_agent.capabilities.springvue.evidence import (
    ChainSummary,
    EndpointChain,
    EvidenceEdge,
    EvidenceGraph,
    EvidenceLocation,
    EvidenceNode,
)
from codereview_agent.capabilities.springvue.extractors import extract_config, extract_frontend, extract_mybatis, extract_spring, extract_sql
from codereview_agent.scanner import scan_project


FIXTURE = Path(__file__).parent / "fixtures" / "springvue_dataflow"


class EvidenceModelTests(unittest.TestCase):
    def test_graph_deduplicates_nodes_and_keeps_stronger_edge(self):
        graph = EvidenceGraph()
        controller = EvidenceNode("method:OrderController.create", "java_method", "OrderController.create", EvidenceLocation("OrderController.java", 10))
        service = EvidenceNode("method:OrderService.create", "java_method", "OrderService.create", EvidenceLocation("OrderService.java", 12))
        graph.add_node(controller)
        graph.add_node(controller)
        graph.add_node(service)
        graph.add_edge(EvidenceEdge(controller.node_id, service.node_id, "invokes", "名称匹配", "medium"))
        graph.add_edge(EvidenceEdge(controller.node_id, service.node_id, "invokes", "接收者类型和方法匹配", "high"))

        self.assertEqual(2, len(graph.nodes))
        self.assertEqual(1, len(graph.edges))
        self.assertEqual("high", graph.edges[0].confidence)

    def test_low_confidence_edge_is_not_a_model_fact(self):
        edge = EvidenceEdge("a", "b", "invokes", "仅名称相似", "low")
        self.assertFalse(edge.model_fact)

    def test_chain_and_summary_are_serializable_values(self):
        chain = EndpointChain("springvue.http.001", "POST /orders", ["endpoint", "sql"], [], "partial", ["未找到 Mapper"])
        summary = ChainSummary(endpoint_count=1, partial_count=1)
        self.assertEqual("partial", chain.status)
        self.assertEqual(1, summary.as_dict()["partial_count"])

    def test_builds_complete_vue_to_table_chain(self):
        files = scan_project(FIXTURE)
        graph = EvidenceGraph()
        extract_frontend(files, graph)
        extract_mybatis(files, graph)
        extract_spring(files, graph)
        extract_sql(files, graph)
        extract_config(files, graph)

        chains, summary = build_endpoint_chains(graph)

        self.assertEqual(1, len(chains))
        self.assertEqual("complete", chains[0].status)
        self.assertIn("OrderPage", " ".join(node.name for node in graph.nodes.values()))
        self.assertIn("OrderMapper.insertOrder", render_chain_path(graph, chains[0]))
        self.assertIn("orders", render_chain_path(graph, chains[0]))
        self.assertEqual(1, summary.complete_count)
        self.assertEqual(0, summary.unmatched_frontend_count)

    def test_missing_persistence_layers_produces_visible_partial_chain(self):
        graph = EvidenceGraph()
        endpoint = EvidenceNode("endpoint", "controller_endpoint", "GET /orders", EvidenceLocation("OrderController.java", 8), {"method": "GET", "normalized_url": "/orders"})
        controller = EvidenceNode("controller", "java_method", "OrderController.list", EvidenceLocation("OrderController.java", 8), {"role": "controller", "unresolved_calls": ["orderService.list(...)"]})
        graph.add_node(endpoint)
        graph.add_node(controller)
        graph.add_edge(EvidenceEdge(endpoint.node_id, controller.node_id, "routes_to", "路由绑定", "high"))

        chains, summary = build_endpoint_chains(graph)

        self.assertEqual("partial", chains[0].status)
        self.assertIn("未解析调用：orderService.list(...)", chains[0].gaps)
        self.assertEqual(1, summary.partial_count)
        self.assertEqual(1, summary.unmatched_endpoint_count)

    def test_dynamic_frontend_route_requires_confirmation(self):
        graph = EvidenceGraph()
        api = EvidenceNode("api", "api_call", "POST /orders/${id}", EvidenceLocation("orderApi.ts", 3), {"method": "POST", "normalized_url": "/orders/{}", "dynamic": True})
        endpoint = EvidenceNode("endpoint", "controller_endpoint", "POST /orders/{}", EvidenceLocation("OrderController.java", 8), {"method": "POST", "normalized_url": "/orders/{}"})
        controller = EvidenceNode("controller", "java_method", "OrderController.update", EvidenceLocation("OrderController.java", 8), {"role": "controller"})
        for node in (api, endpoint, controller):
            graph.add_node(node)
        graph.add_edge(EvidenceEdge(endpoint.node_id, controller.node_id, "routes_to", "路由绑定", "high"))

        chains, summary = build_endpoint_chains(graph)

        self.assertEqual("needs_confirmation", chains[0].status)
        self.assertEqual(1, summary.needs_confirmation_count)

    def test_cyclic_java_calls_do_not_loop_forever(self):
        graph = EvidenceGraph()
        endpoint = EvidenceNode("endpoint", "controller_endpoint", "GET /cycle", EvidenceLocation("CycleController.java", 4), {"method": "GET", "normalized_url": "/cycle"})
        first = EvidenceNode("first", "java_method", "CycleController.start", EvidenceLocation("CycleController.java", 4), {"role": "controller"})
        second = EvidenceNode("second", "java_method", "CycleService.next", EvidenceLocation("CycleService.java", 5), {"role": "service"})
        for node in (endpoint, first, second):
            graph.add_node(node)
        graph.add_edge(EvidenceEdge(endpoint.node_id, first.node_id, "routes_to", "路由绑定", "high"))
        graph.add_edge(EvidenceEdge(first.node_id, second.node_id, "invokes", "类型匹配", "high"))
        graph.add_edge(EvidenceEdge(second.node_id, first.node_id, "invokes", "回调", "high"))

        chains, _ = build_endpoint_chains(graph)

        self.assertEqual(3, len(chains[0].node_ids))


if __name__ == "__main__":
    unittest.main()
