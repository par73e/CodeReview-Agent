import unittest
from pathlib import Path

from codereview_agent.capabilities.springvue.chain_builder import build_endpoint_chains
from codereview_agent.capabilities.springvue.evidence import EvidenceGraph
from codereview_agent.capabilities.springvue.extractors import extract_config, extract_frontend, extract_mybatis, extract_spring, extract_sql
from codereview_agent.capabilities.springvue.planner import build_chain_context, build_chain_review_plan, build_config_context
from codereview_agent.planner import estimate_tokens
from codereview_agent.project_map import build_project_map
from codereview_agent.scanner import scan_project


FIXTURE = Path(__file__).parent / "fixtures" / "springvue_dataflow"


class SpringVuePlannerTests(unittest.TestCase):
    def setUp(self):
        files = scan_project(FIXTURE)
        self.project = build_project_map(FIXTURE, files)
        self.graph = EvidenceGraph()
        extract_frontend(files, self.graph)
        extract_mybatis(files, self.graph)
        extract_spring(files, self.graph)
        extract_sql(files, self.graph)
        extract_config(files, self.graph)
        self.chains, _ = build_endpoint_chains(self.graph)

    def test_builds_endpoint_task_with_minimal_context(self):
        tasks = build_chain_review_plan(self.project, self.graph, self.chains)
        chain_task = next(task for task in tasks if task.metadata.get("kind") == "endpoint_chain")
        context = build_chain_context(chain_task)
        self.assertIn("POST /api/orders", context)
        self.assertIn("OrderController.create", context)
        self.assertIn("OrderMapper.insertOrder", context)
        self.assertIn("INSERT INTO orders", context)
        self.assertNotIn("该文件其余内容未提供", context)

    def test_token_estimate_uses_chain_context(self):
        tasks = build_chain_review_plan(self.project, self.graph, self.chains)
        estimate = estimate_tokens(self.project, tasks)
        self.assertGreater(estimate["input"], 0)
        self.assertEqual(len(tasks) * 1600, estimate["output_max"])

    def test_config_context_uses_redacted_facts_instead_of_raw_yaml(self):
        tasks = build_chain_review_plan(self.project, self.graph, self.chains)
        config_task = next(task for task in tasks if task.metadata.get("kind") == "springvue_config")

        context = build_config_context(config_task)

        self.assertIn("spring.datasource.password=[已脱敏]", context)
        self.assertNotIn("${DB_PASSWORD}", context)


if __name__ == "__main__":
    unittest.main()
