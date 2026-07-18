import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from codereview_agent.capabilities.registry import build_default_registry
from codereview_agent.llm import ModelReply
from codereview_agent.report import print_project_summary, print_result, write_markdown
from codereview_agent.review import _deduplicate, _issue_context, run_review
from codereview_agent.scanner import scan_project
from codereview_agent.types import Issue, ReviewResult, Usage


FIXTURE = Path(__file__).parent / "fixtures" / "springvue_dataflow"
SERVICE_PATH = "backend/src/main/java/demo/OrderService.java"


class ChainReviewModel:
    def __init__(self):
        self.requests = []

    def review(self, system, user, max_tokens):
        self.requests.append((system, user, max_tokens))
        if "独立严重等级复核器" in system:
            return ModelReply(
                '{"verdict":"成立","recommended_severity":"严重 Bug","reason":"链路证据完整。"}',
                Usage(10, 5, 15),
            )
        return ModelReply(
            '{"issues":[{"category":"业务正确性","severity":"严重 Bug","title":"订单写入缺少业务幂等保护",'
            '"file":"backend/src/main/java/demo/OrderService.java","line":10,'
            '"evidence":"createOrder 直接调用 insertOrder","trigger_path":"POST /api/orders -> createOrder",'
            '"impact":"重复请求可能产生重复订单","recommendation":"增加业务幂等键并建立唯一约束",'
            '"confidence":"高","needs_human_confirmation":false}]}',
            Usage(20, 10, 30),
        )


class SpringVueReportingTests(unittest.TestCase):
    def setUp(self):
        files = scan_project(FIXTURE)
        self.capability_run = build_default_registry().analyze(FIXTURE, files)
        self.project = self.capability_run.project
        self.chain_task = next(task for task in self.capability_run.tasks if task.metadata.get("kind") == "endpoint_chain")

    def test_model_issue_is_bound_to_agent_chain_and_verified_with_chain_context(self):
        model = ChainReviewModel()
        result = run_review(self.project, [self.chain_task], model, output=lambda _: None)

        self.assertEqual(1, len(result.issues))
        issue = result.issues[0]
        self.assertEqual(self.chain_task.task_id, issue.chain_id)
        self.assertEqual("POST /api/orders", issue.endpoint)
        self.assertEqual("complete", issue.chain_status)
        self.assertIn("OrderMapper.insertOrder", issue.chain_path)
        self.assertEqual(["POST /api/orders"], issue.affected_endpoints)
        self.assertEqual("二次复核成立", issue.review_status)
        verification_request = model.requests[-1][1]
        self.assertIn("链路编号：" + self.chain_task.task_id, verification_request)
        self.assertIn("Agent 已证明的关系", verification_request)

    def test_deduplication_merges_affected_endpoints_for_shared_root_cause(self):
        first = self._issue("springvue.http.001", "POST /api/orders")
        second = self._issue("springvue.http.002", "PUT /api/orders/{id}")

        merged = _deduplicate([first, second])

        self.assertEqual(1, len(merged))
        self.assertEqual(["POST /api/orders", "PUT /api/orders/{id}"], merged[0].affected_endpoints)

    def test_terminal_and_markdown_show_dataflow_and_issue_chain(self):
        issue = self._issue(self.chain_task.task_id, "POST /api/orders")
        issue.chain_status = "complete"
        issue.chain_path = str(self.chain_task.metadata.get("chain_path", ""))
        result = ReviewResult(self.project, [self.chain_task], [issue])

        terminal = io.StringIO()
        with redirect_stdout(terminal):
            print_project_summary(self.project)
            print_result(result)
        rendered = terminal.getvalue()
        self.assertIn("数据通路分析", rendered)
        self.assertIn("完整链路 1", rendered)
        self.assertIn("接口：POST /api/orders", rendered)
        self.assertIn("通路：", rendered)

        with tempfile.TemporaryDirectory() as directory:
            report_path = write_markdown(result, Path(directory))
            markdown = report_path.read_text(encoding="utf-8")
        self.assertIn("## 数据通路覆盖摘要", markdown)
        self.assertIn("- 接口：`POST /api/orders`", markdown)
        self.assertIn("- 数据通路：", markdown)

    def test_config_verification_context_does_not_restore_sensitive_yaml_value(self):
        config_task = next(task for task in self.capability_run.tasks if task.metadata.get("kind") == "springvue_config")
        issue = Issue(
            "配置安全", "严重 Bug", "数据源密码配置需复核",
            "backend/src/main/resources/application.yml", 9,
            "配置事实已脱敏", "应用启动加载配置", "可能影响凭据安全", "使用安全配置中心", "中",
            False, "initial", config_task.task_id,
        )

        context = _issue_context(self.project, issue, config_task)

        self.assertIn("spring.datasource.password=[已脱敏]", context)
        self.assertNotIn("${DB_PASSWORD}", context)

    @staticmethod
    def _issue(chain_id, endpoint):
        return Issue(
            "业务正确性", "中等 Bug", "共享写入逻辑缺少校验", SERVICE_PATH, 10,
            "createOrder 直接调用 insertOrder", endpoint, "可能写入无效数据", "增加服务端校验", "高",
            False, "initial", chain_id, chain_id, endpoint, "complete", "", [], [endpoint],
        )


if __name__ == "__main__":
    unittest.main()
