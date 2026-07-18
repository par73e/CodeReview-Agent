import unittest
from pathlib import Path

from codereview_agent.capabilities.springvue.evidence import EvidenceGraph
from codereview_agent.capabilities.springvue.extractors import (
    extract_config,
    extract_frontend,
    extract_mybatis,
    extract_spring,
    extract_sql,
)
from codereview_agent.scanner import scan_project


FIXTURE = Path(__file__).parent / "fixtures" / "springvue_dataflow"


class SpringVueExtractorTests(unittest.TestCase):
    def setUp(self):
        self.files = scan_project(FIXTURE)
        self.graph = EvidenceGraph()
        extract_frontend(self.files, self.graph)
        extract_mybatis(self.files, self.graph)
        extract_spring(self.files, self.graph)
        extract_sql(self.files, self.graph)
        extract_config(self.files, self.graph)

    def test_extracts_frontend_and_controller_route(self):
        calls = [node for node in self.graph.nodes.values() if node.kind == "api_call"]
        endpoints = [node for node in self.graph.nodes.values() if node.kind == "controller_endpoint"]
        self.assertEqual("POST", calls[0].metadata["method"])
        self.assertEqual("/api/orders", calls[0].metadata["normalized_url"])
        self.assertEqual("POST /api/orders", endpoints[0].name)

    def test_extracts_typed_java_invocations(self):
        invocations = [edge for edge in self.graph.edges if edge.kind == "invokes"]
        names = {(self.graph.nodes[edge.source].name, self.graph.nodes[edge.target].name) for edge in invocations}
        self.assertIn(("OrderController.create", "OrderService.createOrder"), names)
        self.assertIn(("OrderService.createOrder", "OrderMapper.insertOrder"), names)

    def test_maps_mapper_method_to_sql_and_table(self):
        mapping = [edge for edge in self.graph.edges if edge.kind == "maps_to_sql"]
        table_edges = [edge for edge in self.graph.edges if edge.kind == "writes_table"]
        self.assertEqual(1, len(mapping))
        self.assertEqual("orders", self.graph.nodes[table_edges[0].target].name)

    def test_extracts_config_without_exposing_secret_value(self):
        config_nodes = [node for node in self.graph.nodes.values() if node.kind == "config_key"]
        password = next(node for node in config_nodes if node.name.endswith("datasource.password"))
        self.assertEqual("[已脱敏]", password.metadata["value"])
        self.assertTrue(any(node.metadata.get("remote_reference") for node in config_nodes))


if __name__ == "__main__":
    unittest.main()
