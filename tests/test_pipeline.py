import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codereview_agent.config import AppConfig, prompt_configuration
from codereview_agent.capabilities.base import Capability, CapabilityDetection, CapabilityResult
from codereview_agent.capabilities.generic import GenericCapability
from codereview_agent.capabilities.registry import CapabilityRegistry, build_default_registry
from codereview_agent.llm import DeepSeekClient
from codereview_agent.planner import build_review_plan, estimate_tokens
from codereview_agent.project_map import build_project_map
from codereview_agent.review import _parse_issues, run_review
from codereview_agent.scanner import scan_project
from codereview_agent.llm import ModelReply
from codereview_agent.types import ProjectMap, ReviewTask, SourceFile, Usage


class FakeModel:
    def review(self, system, user, max_tokens):
        if "独立严重等级复核器" in system:
            return ModelReply('{"verdict":"成立","recommended_severity":"严重 Bug","reason":"路由可直接访问且没有鉴权证据。"}', Usage(10, 5, 15))
        return ModelReply('''{"issues":[{"category":"接口安全","severity":"严重 Bug","title":"缺少接口鉴权","file":"src/main/java/demo/UserController.java","line":5,"evidence":"@GetMapping(\\"/users\\") 方法中没有可见鉴权检查","trigger_path":"外部请求 /users -> UserController.users","impact":"未授权用户可能读取用户数据","recommendation":"接入统一鉴权并在服务端验证访问主体","confidence":"高","needs_human_confirmation":false}]}''', Usage(20, 10, 30))


class RetryModel:
    def __init__(self):
        self.calls = 0

    def review(self, system, user, max_tokens):
        self.calls += 1
        if self.calls == 1:
            return ModelReply('{"issues":[{"title":"truncated"', Usage(20, 1300, 1320))
        return ModelReply('''{"issues":[{"category":"结构","severity":"优化建议","title":"Service 方法可拆分","file":"src/main/java/demo/UserService.java","line":3,"evidence":"save 方法承载多个职责","trigger_path":"业务服务调用","impact":"可维护性下降","recommendation":"按职责拆分私有方法","confidence":"中","needs_human_confirmation":false}]}''', Usage(20, 50, 70))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._write("src/main/java/demo/UserController.java", """
            @RestController
            class UserController {
                private UserMapper userMapper;
                @GetMapping(\"/users\")
                public Object users() { return userMapper.list(); }
            }
        """)
        self._write("src/main/java/demo/UserService.java", """
            @Service
            class UserService { public void save(User user) { mapper.insert(user); } }
        """)
        self._write("src/main/java/demo/UserMapper.java", """
            @Mapper
            interface UserMapper { Object list(); }
        """)
        self._write("src/main/resources/mapper/UserMapper.xml", """
            <mapper><select id=\"list\">select * from user where id = ${id}</select></mapper>
        """)
        self._write("src/main/resources/bootstrap.yml", """
            spring:
              cloud:
                nacos:
                  server-addr: localhost:8848
            datasource:
              password: secret-pass
        """)
        self._write("web/src/App.vue", """<template><div v-html=\"content\"></div></template>""")
        self._write("web/src/api.js", """axios.get('/users')""")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_builds_project_map_and_local_review(self):
        files = scan_project(self.root)
        project = build_project_map(self.root, files)
        self.assertIn("MyBatis", project.technologies)
        self.assertIn("Nacos", project.technologies)
        self.assertIn("Vue", project.technologies)
        self.assertTrue(any(item["kind"] == "mybatis_dollar_placeholder" for item in project.signals))
        self.assertTrue(any(item["kind"] == "plaintext_secret" for item in project.signals))
        tasks = build_review_plan(project)
        self.assertTrue(tasks)
        estimate = estimate_tokens(project, tasks)
        self.assertGreater(estimate["total_max"], 0)
        result = run_review(project, tasks, client=None, output=lambda _: None)
        self.assertTrue(result.issues)
        self.assertTrue(result.uncovered)

    def test_model_critical_finding_is_deduplicated_and_verified(self):
        files = scan_project(self.root)
        project = build_project_map(self.root, files)
        tasks = build_review_plan(project)
        result = run_review(project, tasks, client=FakeModel(), output=lambda _: None)
        self.assertEqual(1, len(result.issues))
        self.assertEqual("严重 Bug", result.issues[0].severity)
        self.assertEqual("二次复核成立", result.issues[0].review_status)
        self.assertGreater(result.usage.total_tokens, 0)

    def test_invalid_model_json_retries_with_stricter_request(self):
        files = scan_project(self.root)
        project = build_project_map(self.root, files)
        tasks = build_review_plan(project)
        model = RetryModel()
        result = run_review(project, tasks, client=model, output=lambda _: None)
        self.assertTrue(result.issues)
        self.assertEqual([], result.failed_tasks)
        self.assertGreater(model.calls, len(tasks))

    def test_string_false_does_not_skip_critical_verification(self):
        issues = _parse_issues('''{"issues":[{"category":"安全","severity":"严重 Bug","title":"test","file":"A.java","line":1,"evidence":"e","trigger_path":"p","impact":"i","recommendation":"r","confidence":"高","needs_human_confirmation":"false"}]}''', "test")
        self.assertFalse(issues[0].needs_human_confirmation)

    @patch("codereview_agent.llm._post_json")
    def test_deepseek_pro_uses_json_mode_with_thinking_disabled(self, post_json):
        post_json.return_value = {
            "choices": [{"message": {"content": "{\"issues\":[]}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        client = DeepSeekClient(AppConfig("deepseek", "deepseek-v4-pro", "test-key", "https://api.deepseek.com"))
        client.review("system", "user", 1600)
        payload = post_json.call_args.args[1]
        self.assertEqual("deepseek-v4-pro", payload["model"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertEqual(1600, payload["max_tokens"])

    @patch("codereview_agent.config.save_config")
    @patch("builtins.input", side_effect=["2", "", ""])
    def test_ollama_configuration_uses_qwen_default(self, mocked_input, save_config):
        config = prompt_configuration()
        self.assertEqual("ollama", config.provider)
        self.assertEqual("qwen2.5:3b", config.model)
        self.assertEqual("http://localhost:11434", config.base_url)
        save_config.assert_called_once_with(config)

    def test_spring_capability_claims_known_stack_without_generic_overlap(self):
        files = scan_project(self.root)
        run = build_default_registry().analyze(self.root, files)
        spring = next(item for item in run.selections if item.name == "SpringVue")
        self.assertGreaterEqual(spring.score, 0.5)
        self.assertFalse(any(item.name == "Generic" for item in run.selections))
        self.assertTrue(any(task.domain == "接口安全与权限" for task in run.tasks))

    def test_generic_capability_claims_only_uncovered_code_in_mixed_project(self):
        self._write("tools/check.go", "package tools\nfunc Check() { secret := \"not-for-production\" }\n")
        files = scan_project(self.root)
        run = build_default_registry().analyze(self.root, files)
        spring = next(item for item in run.selections if item.name == "SpringVue")
        generic = next(item for item in run.selections if item.name == "Generic")
        self.assertIn("tools/check.go", generic.claimed_paths)
        self.assertFalse(set(spring.claimed_paths).intersection(generic.claimed_paths))
        self.assertTrue(any(task.task_id.startswith("generic.") for task in run.tasks))

    def test_registry_combines_multiple_specialized_modules_before_generic(self):
        class LanguageCapability(Capability):
            def __init__(self, name, language):
                self.name = name
                self.language = language
                self.received_paths = []

            def detect(self, files):
                count = sum(item.language == self.language for item in files)
                return CapabilityDetection(self.name, 1.0 if count else 0.0, "发现 {0} 个 {1} 文件".format(count, self.language))

            def claim_files(self, files):
                return [item for item in files if item.language == self.language]

            def analyze(self, root, files):
                self.received_paths = [item.relative_path for item in files]
                project = ProjectMap(root, files, [self.name], {}, [], [], [], [], [], [])
                task = ReviewTask(self.name + ".review", self.name, 10, self.received_paths, [], [], "测试模块任务")
                return CapabilityResult(self.name, project, [task], self.received_paths, "测试模块")

        java_module = LanguageCapability("Java专属", "java")
        go_module = LanguageCapability("Go专属", "go")
        sources = [
            SourceFile(self.root / "src/App.java", "src/App.java", "java", "class App {}", 1),
            SourceFile(self.root / "cmd/main.go", "cmd/main.go", "go", "package main", 1),
        ]

        run = CapabilityRegistry([java_module, go_module, GenericCapability()]).analyze(self.root, sources)

        self.assertEqual(["Java专属", "Go专属"], [item.name for item in run.selections])
        self.assertEqual(["src/App.java"], java_module.received_paths)
        self.assertEqual(["cmd/main.go"], go_module.received_paths)
        self.assertFalse(any(item.name == "Generic" for item in run.selections))

    def test_unknown_project_uses_generic_capability(self):
        source = SourceFile(self.root / "main.go", "main.go", "go", "package main\nfunc main() {}\n", 2)
        run = build_default_registry().analyze(self.root, [source])
        self.assertEqual(["Generic"], [item.name for item in run.selections])
        self.assertTrue(run.tasks)

    def test_failed_specialized_capability_falls_back_to_generic(self):
        class BrokenCapability(Capability):
            name = "Broken"

            def detect(self, files):
                return CapabilityDetection(self.name, 1.0, "测试失败模块")

            def claim_files(self, files):
                return files

            def analyze(self, root, files):
                raise RuntimeError("预期失败")

        source = SourceFile(self.root / "main.go", "main.go", "go", "package main\nfunc main() {}\n", 2)
        run = CapabilityRegistry([BrokenCapability(), GenericCapability()]).analyze(self.root, [source])
        self.assertTrue(run.failures)
        self.assertEqual("Generic", run.selections[-1].name)


if __name__ == "__main__":
    unittest.main()
