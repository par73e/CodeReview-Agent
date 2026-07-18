import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from codereview_agent.capabilities.registry import build_default_registry
from codereview_agent.capabilities.springvue.chain_builder import build_endpoint_chains, render_chain_path
from codereview_agent.capabilities.springvue.evidence import EvidenceGraph
from codereview_agent.capabilities.springvue.extractors import extract_frontend, extract_mybatis, extract_spring, extract_sql
from codereview_agent.capabilities.springvue.java_index import JavaSemanticIndex
from codereview_agent.report import print_project_summary
from codereview_agent.types import SourceFile


class SpringVueRealProjectCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/virtual/springvue-project")
        self.files = [
            self._source("web/src/api/product.ts", "typescript", """
                export const getProduct = (id: number) => request.get('/api/product/' + id)
            """),
            self._source("src/ProductController.java", "java", """
                @RestController
                class ProductController {
                    private final ProductService productService;
                    ProductController(ProductService productService) { this.productService = productService; }

                    @GetMapping("/api/product/{id}")
                    Product detail(Long id) { return productService.getDetailProduct(id); }

                    @PostMapping("/api/product")
                    void create(Product product) { productService.createProduct(product); }

                    @GetMapping("/api/product/internal/{id}")
                    Product internal(Long id) { return productService.getById(id); }
                }
            """),
            self._source("src/ProductService.java", "java", """
                interface ProductService extends IService<Product> {
                    Product getDetailProduct(Long id);
                    void createProduct(Product product);
                }
            """),
            self._source("src/ProductServiceImpl.java", "java", """
                @Service
                class ProductServiceImpl extends ServiceImpl<ProductMapper, Product> implements ProductService {
                    @Override
                    public Product getDetailProduct(Long id) { return baseMapper.findDetailById(id); }

                    @Override
                    public void createProduct(Product product) { this.save(product); }
                }
            """),
            self._source("src/ProductMapper.java", "java", """
                @Mapper
                interface ProductMapper extends BaseMapper<Product> {
                    @Select("<script>"
                        + "SELECT * FROM product"
                        + " WHERE id = #{id}"
                        + "</script>")
                    Product findDetailById(@Param("id") Long id);
                }
            """),
            self._source("src/Product.java", "java", """
                @TableName("product")
                class Product { Long id; }
            """),
        ]

    def _build_graph(self):
        graph = EvidenceGraph()
        index = JavaSemanticIndex(self.files)
        extract_frontend(self.files, graph)
        extract_mybatis(self.files, graph, index)
        extract_spring(self.files, graph, index)
        extract_sql(self.files, graph)
        return graph

    def test_connects_service_interface_to_implementation_and_explicit_mapper_sql(self):
        graph = self._build_graph()
        chains, _ = build_endpoint_chains(graph)
        chain = next(item for item in chains if item.endpoint == "GET /api/product/{}")
        path = render_chain_path(graph, chain)

        self.assertEqual("complete", chain.status)
        self.assertIn("ProductServiceImpl.getDetailProduct", path)
        self.assertIn("ProductMapper.findDetailById", path)
        self.assertIn("product", path)
        self.assertTrue(any(edge.kind == "implements_method" for edge in chain.edges))

    def test_models_inherited_mybatis_plus_crud_without_inventing_sql(self):
        graph = self._build_graph()
        chains, _ = build_endpoint_chains(graph)
        create_chain = next(item for item in chains if item.endpoint == "POST /api/product")
        internal_chain = next(item for item in chains if item.endpoint == "GET /api/product/internal/{}")

        framework_nodes = [graph.nodes[node_id] for node_id in create_chain.node_ids + internal_chain.node_ids if graph.nodes[node_id].kind == "framework_persistence"]
        self.assertTrue(any(node.metadata.get("operation") == "insert" for node in framework_nodes))
        self.assertTrue(any(node.metadata.get("operation") == "select" for node in framework_nodes))
        self.assertTrue(all("statement" not in node.metadata for node in framework_nodes))
        self.assertEqual("complete", create_chain.status)
        self.assertEqual("complete", internal_chain.status)

    def test_matches_concatenated_frontend_url_to_path_variable(self):
        graph = self._build_graph()
        _, summary = build_endpoint_chains(graph)
        call = next(node for node in graph.nodes.values() if node.kind == "api_call")

        self.assertEqual("/api/product/{}", call.metadata["normalized_url"])
        self.assertEqual(0, summary.unmatched_frontend_count)

    def test_low_controller_resolution_automatically_uses_layered_review(self):
        files = [
            self._source("src/BrokenController.java", "java", """
                @RestController
                class BrokenController {
                    private MissingService missingService;
                    @GetMapping("/a") Object a() { return missingService.a(); }
                    @GetMapping("/b") Object b() { return missingService.b(); }
                    @GetMapping("/c") Object c() { return missingService.c(); }
                }
            """),
            self._source("src/MissingService.java", "java", """
                interface MissingService { Object a(); Object b(); Object c(); }
            """),
        ]

        run = build_default_registry().analyze(self.root, files)
        summary = run.project.analysis_summary["SpringVue"]

        self.assertEqual("layered_fallback", summary["review_strategy"])
        self.assertTrue(summary["fallback_reasons"])
        self.assertFalse(any(task.metadata.get("kind") == "endpoint_chain" for task in run.tasks))

    def test_feign_call_is_remote_terminal_not_fallback_implementation(self):
        files = [
            self._source("src/ProductClient.java", "java", """
                @FeignClient(name = "product-service", fallback = ProductClientFallback.class)
                interface ProductClient { Product getProduct(Long id); }
            """),
            self._source("src/ProductClientFallback.java", "java", """
                class ProductClientFallback implements ProductClient {
                    public Product getProduct(Long id) { return null; }
                }
            """),
            self._source("src/OrderService.java", "java", """
                @Service
                class OrderService {
                    private ProductClient productClient;
                    Product create(Long id) { return productClient.getProduct(id); }
                }
            """),
        ]
        graph = EvidenceGraph()
        index = JavaSemanticIndex(files)

        extract_spring(files, graph, index)

        remote_nodes = [node for node in graph.nodes.values() if node.kind == "remote_call"]
        fallback_edges = [edge for edge in graph.edges if graph.nodes[edge.target].name == "ProductClientFallback.getProduct"]
        self.assertEqual(["ProductClient.getProduct"], [node.name for node in remote_nodes])
        self.assertEqual([], fallback_edges)

    def test_multiple_interface_implementations_are_not_randomly_selected(self):
        files = [
            self._source("src/PaymentController.java", "java", """
                @RestController
                class PaymentController {
                    private PaymentService paymentService;
                    @PostMapping("/pay") Object pay() { return paymentService.pay(); }
                }
            """),
            self._source("src/PaymentService.java", "java", "interface PaymentService { Object pay(); }"),
            self._source("src/CardPaymentService.java", "java", "class CardPaymentService implements PaymentService { public Object pay() { return null; } }"),
            self._source("src/CashPaymentService.java", "java", "class CashPaymentService implements PaymentService { public Object pay() { return null; } }"),
        ]
        graph = EvidenceGraph()
        index = JavaSemanticIndex(files)
        extract_spring(files, graph, index)

        chains, _ = build_endpoint_chains(graph)
        targets = {
            graph.nodes[edge.target].name
            for edge in chains[0].edges
            if edge.kind == "implements_method"
        }

        self.assertEqual({"CardPaymentService.pay", "CashPaymentService.pay"}, targets)
        self.assertEqual("needs_confirmation", chains[0].status)

    def test_terminal_summary_does_not_list_each_chain_gap(self):
        run = build_default_registry().analyze(self.root, self.files)
        output = io.StringIO()
        with redirect_stdout(output):
            print_project_summary(run.project)

        rendered = output.getvalue()
        self.assertIn("审查策略：", rendered)
        self.assertNotIn("断链与待确认项：", rendered)

    def _source(self, relative_path, language, content):
        cleaned = "\n".join(line.strip() for line in content.strip().splitlines()) + "\n"
        return SourceFile(self.root / relative_path, relative_path, language, cleaned, len(cleaned.splitlines()))


if __name__ == "__main__":
    unittest.main()
