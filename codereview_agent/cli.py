"""Guided terminal interface for CodeReview Agent."""

import argparse
from pathlib import Path
from typing import Optional

from .config import AppConfig, load_config, prompt_configuration, redacted_summary
from .capabilities import build_default_registry
from .llm import make_client
from .planner import estimate_tokens
from .report import print_project_summary, print_result, write_markdown
from .review import run_review
from .scanner import choose_subdirectory, scan_project


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeReview Agent - guided LLM-first code review")
    parser.add_argument("command", nargs="?", choices=["config", "install"], help="运行 config 修改模型；运行 install 注册 codereview 快捷命令")
    args = parser.parse_args()
    if args.command == "config":
        prompt_configuration(load_config())
        return
    if args.command == "install":
        _install_shortcut()
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
    capability_run = build_default_registry().analyze(target, files)
    project = capability_run.project
    print_project_summary(project)
    _print_capabilities(capability_run)
    tasks = capability_run.tasks
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
    result.uncovered.extend(capability_run.uncovered + capability_run.failures)
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


def _print_capabilities(capability_run) -> None:
    print("\n已启用能力模块：")
    for selection in capability_run.selections:
        print("- {0}（{1}，覆盖 {2} 个文件；依据：{3}）".format(selection.name, selection.status, len(selection.claimed_paths), selection.reason))


def _install_shortcut() -> None:
    """Create a user-owned shortcut only after explicit confirmation."""
    source = Path(__file__).resolve().parents[1] / "run"
    bin_dir = Path.home() / ".local" / "bin"
    target = bin_dir / "codereview"
    path_line = 'export PATH="$HOME/.local/bin:$PATH"'
    zshrc = Path.home() / ".zshrc"
    print("将创建快捷命令：{0} -> {1}".format(target, source))
    if input("是否继续？[y/N]：").strip().lower() not in {"y", "yes"}:
        print("已取消。")
        return
    bin_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source:
            print("快捷命令已经存在。")
        else:
            print("未修改现有文件：{0}".format(target))
            return
    else:
        target.symlink_to(source)
        print("快捷命令已创建。")
    existing = zshrc.read_text(encoding="utf-8") if zshrc.exists() else ""
    if path_line not in existing:
        if input("是否将 ~/.local/bin 加入 zsh PATH？[y/N]：").strip().lower() in {"y", "yes"}:
            with zshrc.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(path_line + "\n")
            print("已更新 ~/.zshrc。请执行 source ~/.zshrc 或重新打开终端。")
        else:
            print("未修改 PATH；可通过完整路径运行：" + str(target))
    else:
        print("PATH 已包含 ~/.local/bin；现在可直接运行 codereview。")
