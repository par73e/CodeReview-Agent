"""Filesystem scanning with conservative exclusions."""

from pathlib import Path
from typing import Dict, List

from .types import SourceFile


EXTENSIONS: Dict[str, str] = {
    ".java": "java",
    ".vue": "vue",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".py": "python",
    ".rs": "rust",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".xml": "xml",
}
IGNORED_DIRECTORIES = {
    ".git", ".idea", ".vscode", "node_modules", "target", "dist", "build",
    "coverage", ".gradle", ".mvn", "vendor", "__pycache__",
}
MAX_FILE_BYTES = 512 * 1024


def scan_project(root: Path) -> List[SourceFile]:
    files: List[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        language = EXTENSIONS.get(path.suffix.lower())
        if not language:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(SourceFile(
            path=path,
            relative_path=str(path.relative_to(root)),
            language=language,
            content=content,
            line_count=content.count("\n") + (1 if content else 0),
        ))
    return files


def choose_subdirectory(root: Path) -> Path:
    children = [path for path in sorted(root.iterdir()) if path.is_dir() and path.name not in IGNORED_DIRECTORIES]
    if not children:
        print("当前目录没有可选子目录，将审查当前目录。")
        return root
    print("\n可选子目录：")
    for index, child in enumerate(children, start=1):
        print("{0}. {1}".format(index, child.name))
    print("0. 返回当前目录")
    choice = input("请选择：").strip()
    try:
        selected = int(choice)
        if 1 <= selected <= len(children):
            return children[selected - 1]
    except ValueError:
        pass
    return root
