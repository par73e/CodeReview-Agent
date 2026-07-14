"""Guided terminal interface for CodeReview Agent."""

import argparse
from pathlib import Path
from typing import Optional

from .config import AppConfig, load_config, prompt_configuration, redacted_summary
from .llm import make_client
from .planner import build_review_plan, estimate_tokens
from .project_map import build_project_map
from .report import print_project_summary, print_result, write_markdown
from .review import run_review
from .scanner import choose_subdirectory, scan_project


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeReview Agent - guided LLM-first code review")
    parser.add_argument("command", nargs="?", choices=["config"], help="运行 config 修改默认模型配置")
    args = parser.parse_args()
    if args.command == "config":
        prompt_configuration(load_config())
        return

    config = load_config()
    if config is None:
        print("首次使用 CodeReview Agent，需要先完成默认模型配置。")
        config = prompt_configuration()
    _session(Path.cwd(), config)


def _session(root: Path, config: AppConfig) -> None:
    while True:
        print("\n" + "=" * 66)
        print("CodeReview Agent")
        print("当前工作目录：{0}".format(root))
        print("默认模型：{0}".format(redacted_summary(config)))
        print("=" * 66)
        print("1. 审查当前目录")
        print("2. 选择当前目录下的子目录")
        print("3. 查看当前模型与审查范围")
        print("4. 查看审查范围与分级说明")
        print("0. 退出")
        choice = input("请选择：").strip()
        if choice == "1":
            _review_target(root, config)
        elif choice == "2":
            _review_target(choose_subdirectory(root), config)
        elif choice == "3":
            print("\n当前默认模型：" + redacted_summary(config))
            print("需要切换模型或修改 API Key，请退出后执行：codereview config")
        elif choice == "4":
            _print_scope()
        elif choice == "0":
            print("再见。")
            return
        else:
            print("输入无效，请按菜单选择。")


def _review_target(target: Path, config: AppConfig) -> None:
    print("\n正在扫描：" + str(target))
    files = scan_project(target)
    if not files:
        print("没有发现可审查的 Java、Vue、JS、SQL、YAML 或 MyBatis XML 文件。")
        return
    project = build_project_map(target, files)
    print_project_summary(project)
    tasks = build_review_plan(project)
    if not tasks:
        print("未能从当前目录构建适用于首版技术栈的审查任务。")
        return
    print("\n审查计划：")
    for task in tasks:
        print("- P{0} {1}：{2} 个目标文件".format(task.priority, task.domain, len(task.target_paths)))

    client = make_client(config)
    if client is not None:
        estimate = estimate_tokens(project, tasks)
        print("\nToken 预估（仅为执行前估算，不等同实际账单）：")
        print("输入约 {0}，最大输出 {1}，合计上限约 {2}".format(estimate["input"], estimate["output_max"], estimate["total_max"]))
        if input("是否继续进行模型审查？[y/N]：").strip().lower() not in {"y", "yes"}:
            print("已取消本次审查。")
            return
    else:
        print("\n当前为辅助本地检查模式，不会调用大模型，也无法完成深度 AI 审查。")

    result = run_review(project, tasks, client)
    print_result(result)
    if input("是否导出完整 Markdown 审查报告？[y/N]：").strip().lower() in {"y", "yes"}:
        path = write_markdown(result, target)
        print("报告已导出：" + str(path))


def _print_scope() -> None:
    print("""
首版技术栈：Spring Boot + MySQL + Nacos + Vue。

严重 Bug：可证明的安全漏洞、数据破坏或核心不可用问题；必须二次复核。
中等 Bug：常见场景可能导致异常、错误结果或明显性能问题。
轻度 Bug：边界条件、规范或低概率风险。
优化建议：不一定出错，但有明确性能、结构或可读性收益。

审查边界：接口安全与权限、业务事务、MyBatis/SQL、Nacos/配置、Vue/契约、结构性能。
模型结论必须提供代码证据；证据不足的结论会标记为需人工确认。
""".strip())
