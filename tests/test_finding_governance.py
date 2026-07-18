import tempfile
import unittest
from pathlib import Path

from codereview_agent.llm import ModelReply
from codereview_agent.review import _deduplicate, _validate_evidence, run_review
from codereview_agent.types import Issue, ProjectMap, ReviewTask, SourceFile, Usage


class RejectingVerifierModel:
    def review(self, system, user, max_tokens):
        if "独立严重等级复核器" in system:
            return ModelReply(
                '{"verdict":"不成立","recommended_severity":"需人工确认","reason":"源码不支持该结论。"}',
                Usage(8, 4, 12),
            )
        return ModelReply(
            '{"issues":[{"category":"接口安全","severity":"严重 Bug","title":"接口缺少鉴权",'
            '"file":"UserController.java","line":2,"evidence":"users 接口没有可见鉴权检查",'
            '"trigger_path":"GET /users -> users","impact":"外部用户可读取敏感用户数据",'
            '"recommendation":"增加服务端鉴权","confidence":"高","needs_human_confirmation":false}]}',
            Usage(20, 10, 30),
        )


class FindingGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_safe_mybatis_placeholder_rejects_injection_claim(self):
        project = self._project({
            "ProductMapper.java": (
                'interface ProductMapper {\n'
                '  @Select("SELECT * FROM product WHERE id = #{id}")\n'
                '  Object find(Long id);\n'
                '}\n'
            ),
        })
        issue = self._issue(
            "SQL 注入", "MyBatis SQL 注解未使用参数占位符", "ProductMapper.java", 2,
            "SQL 直接拼接 id，需确认是否使用 #{}",
        )

        self.assertEqual([], _validate_evidence(project, [issue]))

    def test_concurrent_map_and_scheduled_cleanup_reject_contradictory_claims(self):
        project = self._project({
            "CaptchaUtil.java": (
                "class CaptchaUtil {\n"
                "  private final ConcurrentHashMap<String,String> store = new ConcurrentHashMap<>();\n"
                "  void clean() throws Exception { Thread.sleep(300000); store.clear(); }\n"
                "}\n"
            ),
        })
        issues = [
            self._issue("并发", "验证码使用非线程安全 HashMap", "CaptchaUtil.java", 2, "CAPTCHA_STORE 使用 HashMap"),
            self._issue("性能", "验证码没有过期清理机制", "CaptchaUtil.java", 3, "验证码会永久驻留内存"),
        ]

        self.assertEqual([], _validate_evidence(project, issues))

    def test_gateway_identity_boundary_becomes_one_manual_root_cause(self):
        project = self._project({
            "gateway/SecurityFilter.java": (
                "class SecurityFilter implements GlobalFilter {\n"
                "  void filter() { jwtUtil.validate(token); parseToken(token); header(\"X-User-Role\", role); }\n"
                "}\n"
            ),
            "product/ProductController.java": 'class ProductController { void list(@RequestHeader("X-User-Role") String role) {} }',
            "user/UserController.java": 'class UserController { void list(@RequestHeader("X-User-Role") String role) {} }',
        })
        first = self._issue("鉴权", "角色请求头可被伪造", "product/ProductController.java", 1, "直接信任 X-User-Role")
        first.endpoint = "GET /api/admin/products"
        first.affected_endpoints = [first.endpoint]
        second = self._issue("鉴权", "角色请求头可被伪造", "user/UserController.java", 1, "直接信任 X-User-Role")
        second.endpoint = "GET /api/admin/users"
        second.affected_endpoints = [second.endpoint]

        governed = _deduplicate(_validate_evidence(project, [first, second]))

        self.assertEqual(1, len(governed))
        self.assertEqual("需人工确认", governed[0].severity)
        self.assertEqual(
            ["GET /api/admin/products", "GET /api/admin/users"],
            governed[0].affected_endpoints,
        )

    def test_single_database_write_is_not_a_transaction_bug(self):
        project = self._project({
            "AuthService.java": (
                "class AuthService {\n"
                "  public void register(User user) {\n"
                "    userService.save(user);\n"
                "  }\n"
                "}\n"
            ),
        })
        issue = self._issue(
            "事务", "注册方法缺少事务注解", "AuthService.java", 2,
            "register 未标注 @Transactional，当前仅一个 save 写操作",
        )

        self.assertEqual([], _validate_evidence(project, [issue]))

    def test_global_exception_handler_rejects_unhandled_runtime_claim(self):
        project = self._project({
            "user/src/UserService.java": (
                "class UserService {\n"
                "  void login() { throw new RuntimeException(\"密码错误\"); }\n"
                "}\n"
            ),
            "user/src/GlobalExceptionHandler.java": (
                "@RestControllerAdvice\n"
                "class GlobalExceptionHandler {\n"
                "  @ExceptionHandler(Exception.class) Object handle(Exception e) { return error(e.getMessage()); }\n"
                "}\n"
            ),
        })
        issue = self._issue(
            "异常处理", "登录失败抛出 RuntimeException 导致 500 错误", "user/src/UserService.java", 2,
            "RuntimeException 未被捕获，可能泄露堆栈",
        )

        self.assertEqual([], _validate_evidence(project, [issue]))

    def test_model_marked_uncertain_severe_issue_is_forced_to_manual(self):
        project = self._project({
            "UserController.java": "class UserController { Object users() { return service.list(); } }",
        })
        issue = self._issue(
            "接口安全", "接口是否缺少鉴权", "UserController.java", 1,
            "未看到完整的统一鉴权配置，需确认",
        )
        issue.needs_human_confirmation = True

        governed = _validate_evidence(project, [issue])

        self.assertEqual(1, len(governed))
        self.assertEqual("需人工确认", governed[0].severity)
        self.assertNotEqual("initial", governed[0].review_status)

    def test_unconfirmed_severe_issue_never_reaches_report(self):
        project = self._project({
            "UserController.java": (
                "class UserController {\n"
                "  Object users() { return service.list(); }\n"
                "}\n"
            ),
        })
        task = ReviewTask("security", "接口安全", 1, ["UserController.java"], [], [], "test")

        result = run_review(project, [task], RejectingVerifierModel(), output=lambda _: None)

        self.assertEqual([], result.issues)

    def _project(self, files):
        sources = []
        for relative_path, content in files.items():
            language = "java" if relative_path.endswith(".java") else "text"
            sources.append(SourceFile(
                self.root / relative_path,
                relative_path,
                language,
                content,
                len(content.splitlines()),
            ))
        return ProjectMap(self.root, sources, ["Spring Boot"], {}, [], [], [], [], [], [])

    @staticmethod
    def _issue(category, title, file, line, evidence):
        return Issue(
            category, "严重 Bug", title, file, line, evidence,
            "外部请求进入对应方法", "可能影响系统安全或数据正确性", "根据根因修复", "高",
            False, "initial", "test",
        )


if __name__ == "__main__":
    unittest.main()
