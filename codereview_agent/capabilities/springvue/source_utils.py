"""Conservative source-text helpers that preserve locations."""

import re
from typing import List, Optional, Tuple


def line_of(content: str, index: int) -> int:
    return content.count("\n", 0, max(0, index)) + 1


def mask_comments_and_strings(content: str) -> str:
    """Replace comments and string contents with spaces while preserving shape."""
    chars = list(content)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if current == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if current in {'"', "'", "`"}:
                quote = current
                chars[index] = " "
                index += 1
                state = "string"
                continue
        elif state == "line_comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        elif state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 2
                state = "code"
                continue
            if current != "\n":
                chars[index] = " "
            index += 1
            continue
        elif state == "string":
            if current == "\\":
                chars[index] = " "
                if index + 1 < len(chars) and chars[index + 1] != "\n":
                    chars[index + 1] = " "
                index += 2
                continue
            if current == quote:
                chars[index] = " "
                state = "code"
            elif current != "\n":
                chars[index] = " "
            index += 1
            continue
        index += 1
    return "".join(chars)


def find_matching(content: str, start: int, opening: str = "{", closing: str = "}") -> Optional[int]:
    if start < 0 or start >= len(content) or content[start] != opening:
        return None
    depth = 0
    for index in range(start, len(content)):
        if content[index] == opening:
            depth += 1
        elif content[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def split_arguments(value: str) -> List[str]:
    items: List[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character in "(<[{":
            depth += 1
        elif character in ")>]}" and depth:
            depth -= 1
        elif character == "," and depth == 0:
            items.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def parse_java_parameter(parameter: str) -> Tuple[str, str, str]:
    annotation_match = re.search(r"@Param\s*\(\s*[\"']([^\"']+)", parameter)
    param_name = annotation_match.group(1) if annotation_match else ""
    cleaned = re.sub(r"@\w+(?:\s*\([^)]*\))?", " ", parameter)
    cleaned = re.sub(r"\bfinal\b", " ", cleaned)
    parts = cleaned.split()
    if len(parts) < 2:
        return "", "", param_name
    type_name = parts[-2]
    variable = parts[-1].replace("...", "").strip()
    return type_name, variable, param_name or variable


def normalize_type(type_name: str) -> str:
    value = re.sub(r"<.*>", "", type_name).replace("[]", "").replace("?", "")
    return value.rsplit(".", 1)[-1].strip()


def normalize_url(url: str) -> str:
    value = url.strip().split("?", 1)[0]
    value = re.sub(r"^https?://[^/]+", "", value)
    value = re.sub(r"\$\{[^}]+\}", "{}", value)
    value = re.sub(r"\{[^}]+\}", "{}", value)
    value = re.sub(r"/:[^/]+", "/{}", value)
    value = re.sub(r"/+", "/", value)
    if value and not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/"


def join_url(prefix: str, suffix: str) -> str:
    if not prefix:
        return normalize_url(suffix)
    if not suffix:
        return normalize_url(prefix)
    return normalize_url(prefix.rstrip("/") + "/" + suffix.lstrip("/"))


def stable_id(kind: str, file: str, line: int, name: str) -> str:
    safe_name = re.sub(r"\s+", "", name)
    return "{0}:{1}:{2}:{3}".format(kind, file, line, safe_name)
