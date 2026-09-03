#!/usr/bin/env python3
"""Japanese prose linter used by Claude Code hooks and as a standalone CLI."""

from __future__ import annotations

import argparse
import collections
import functools
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Pattern, Sequence, Union


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
RULES_PATH = BASE_DIR / "rules.jsonl"

DEFAULT_CONFIG = {
    "extensions": {
        ".md": "prose",
        ".txt": "prose",
        ".mdx": "prose",
        ".ts": "code",
        ".tsx": "code",
        ".js": "code",
        ".jsx": "code",
        ".json": "json",
        ".html": "html",
    },
    "exclude_basenames": ["CLAUDE.md", "MEMORY.md", "AGENTS.md", "rules.jsonl"],
    "exclude_path_substrings": [
        "/memory/",
        "/.claude/plans/",
        "/node_modules/",
        "/.git/",
        "/hooks/ja_lint/",
        "/skills/ja-lint/",
    ],
    "default_profile": "business",
    "min_japanese_chars": 30,
    "max_violations_in_warning": 5,
}

REWRITE_INSTRUCTION = (
    "以下の文を丸ごと書き直すこと。NGワードだけを別の語に置換することは禁止"
    "（同じ問題が別の語形で残るため）。書き直し後も自動で再検査される。"
)

STOP_INSTRUCTION = (
    "ja-lint: セッション終了前に以下の critical 違反を書き直すこと。"
    "NGワードだけを別の語に置換せず、該当文を丸ごと書き直すこと。"
)

JAPANESE_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")
BOUNDARY_RE = re.compile(r"[。！？]+|\r?\n")

# config の包括的な自己除外より、受け入れテスト用 fixture の具体パスを優先する。
# この一点だけを対象に戻すことで、通常運用中の ja_lint 自身は検査しない。
FIXTURE_CARVEOUT = "/hooks/ja_lint/tests/fixtures/"


@dataclass(frozen=True)
class Rule:
    id: str
    regex: Pattern[str]
    severity: str
    label: str
    good: tuple[str, ...]
    min_count: int = 1


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int
    line: int
    original: str
    masked: str
    terminator: Optional[str]
    is_fragment: bool


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    rule_id: str
    severity: str
    label: str
    sentence: str
    good: tuple[str, ...]
    end_line: Optional[int] = None

    def __post_init__(self) -> None:
        if self.end_line is None:
            object.__setattr__(self, "end_line", self.line)

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "label": self.label,
            "sentence": self.sentence,
            "good": list(self.good),
        }


def load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            config.update(raw)
    except (OSError, ValueError, TypeError):
        pass
    return _normalize_config(config)


def _normalize_config(config: dict[str, object]) -> dict[str, object]:
    """Normalize legacy extension lists without mutating the caller's mapping."""
    normalized = dict(config)
    extensions = normalized.get("extensions", {})
    if isinstance(extensions, list):
        normalized["extensions"] = {
            str(item).lower(): "prose"
            for item in extensions
            if isinstance(item, str) and item.startswith(".")
        }
    elif isinstance(extensions, dict):
        normalized["extensions"] = {
            str(extension).lower(): str(mode)
            for extension, mode in extensions.items()
            if isinstance(extension, str)
            and extension.startswith(".")
            and mode in {"prose", "code", "json", "html"}
        }
    else:
        normalized["extensions"] = {}
    return normalized


def _absolute_path(
    path: Union[str, os.PathLike[str]], cwd: Optional[str] = None
) -> str:
    value = os.fspath(path)
    if not os.path.isabs(value):
        value = os.path.join(cwd or os.getcwd(), value)
    return os.path.realpath(value)


@functools.lru_cache(maxsize=512)
def _repo_context_for_directory(directory: str) -> tuple[Optional[str], dict[str, object]]:
    current = Path(directory)
    for candidate_dir in (current, *current.parents):
        candidate = candidate_dir / ".ja-lint.json"
        if not candidate.is_file():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        return str(candidate_dir), raw if isinstance(raw, dict) else {}
    return None, {}


def _repo_context(
    path: Union[str, os.PathLike[str]], cwd: Optional[str] = None
) -> tuple[Optional[str], dict[str, object]]:
    absolute = Path(_absolute_path(path, cwd))
    directory = absolute if absolute.is_dir() else absolute.parent
    return _repo_context_for_directory(str(directory))


def load_repo_config(path: Union[str, os.PathLike[str]]) -> dict[str, object]:
    """Load the nearest parent .ja-lint.json for an absolute target path."""
    return dict(_repo_context(path)[1])


def _glob_match(relpath: str, pattern: str) -> bool:
    """Match repository-relative POSIX paths, including zero-depth **/."""
    path_value = relpath.replace("\\", "/")
    pattern_value = pattern.replace("\\", "/")
    # 先頭の "./" だけを外す。lstrip("./") だと ".docs/x" の "." まで剥がれ、
    # "docs/**" に誤マッチする。
    if path_value.startswith("./"):
        path_value = path_value[2:]
    if pattern_value.startswith("./"):
        pattern_value = pattern_value[2:]
    pieces: list[str] = []
    index = 0
    while index < len(pattern_value):
        if pattern_value.startswith("**/", index):
            pieces.append("(?:.*/)?")
            index += 3
        elif pattern_value.startswith("**", index):
            pieces.append(".*")
            index += 2
        elif pattern_value[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif pattern_value[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(pattern_value[index]))
            index += 1
    return re.fullmatch("".join(pieces), path_value) is not None


def mode_for_path(
    path: Union[str, os.PathLike[str]],
    config: Optional[dict[str, object]] = None,
) -> Optional[str]:
    active_config = _normalize_config(config) if config is not None else load_config()
    extensions = active_config.get("extensions", {})
    if not isinstance(extensions, dict):
        return None
    value = extensions.get(Path(os.fspath(path)).suffix.lower())
    return str(value) if value in {"prose", "code", "json", "html"} else None


def is_target_path(
    path: Union[str, os.PathLike[str]],
    cwd: Optional[str] = None,
    config: Optional[dict[str, object]] = None,
) -> bool:
    """Return whether a hook should inspect path according to config."""
    config = _normalize_config(config) if config is not None else load_config()
    absolute = _absolute_path(path, cwd)
    normalized = absolute.replace(os.sep, "/")
    if mode_for_path(absolute, config) is None:
        return False
    basenames = config.get("exclude_basenames", [])
    if os.path.basename(absolute) in {
        str(item) for item in basenames if isinstance(item, str)
    }:
        return False
    repo_root, repo_config = _repo_context(absolute)
    exclusion_path = normalized
    if repo_root:
        relpath = os.path.relpath(absolute, repo_root).replace(os.sep, "/")
        exclusion_path = "/" + relpath.lstrip("/")
        exclude_globs = repo_config.get("exclude_globs", [])
        if isinstance(exclude_globs, list) and any(
            _glob_match(relpath, item)
            for item in exclude_globs
            if isinstance(item, str)
        ):
            return False
    if FIXTURE_CARVEOUT in normalized:
        return True
    configured_excluded = config.get("exclude_path_substrings", [])
    excluded = list(configured_excluded) if isinstance(configured_excluded, list) else []
    repo_excluded = repo_config.get("exclude_path_substrings", [])
    if isinstance(repo_excluded, list):
        excluded.extend(repo_excluded)
    return not any(
        str(item).replace("\\", "/") in exclusion_path
        for item in excluded
        if isinstance(item, str)
    )


def load_rules(
    profile: Optional[str] = None,
    path: Path = RULES_PATH,
    config: Optional[dict[str, object]] = None,
    repo_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> list[Rule]:
    """Load enabled, profile-matching JSONL rules; malformed rows are ignored."""
    config = config or load_config()
    active_profile = profile or os.environ.get("JA_LINT_PROFILE") or str(
        config.get("default_profile", "business")
    )
    rule_paths = [Path(path)]
    if repo_path is not None:
        repo_root, repo_config = _repo_context(repo_path)
        extra_rules = repo_config.get("extra_rules")
        if repo_root and isinstance(extra_rules, str) and extra_rules:
            extra_path = Path(repo_root) / extra_rules
            if extra_path not in rule_paths:
                rule_paths.append(extra_path)
    rules: list[Rule] = []
    lines: list[str] = []
    for rule_path in rule_paths:
        try:
            lines.extend(rule_path.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    for line in lines:
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("enabled") is not True:
                continue
            scenes = item.get("scenes")
            if not isinstance(scenes, list) or active_profile not in scenes:
                continue
            rule_id = item["id"]
            pattern = item["pattern"]
            severity = item["severity"]
            label = item["label"]
            good = item["good"]
            min_count = item.get("min_count", 1)
            if (
                not all(isinstance(value, str) for value in (rule_id, pattern, severity, label))
                or severity not in {"critical", "warn"}
                or not isinstance(good, list)
                or not all(isinstance(value, str) for value in good)
                or isinstance(min_count, bool)
                or not isinstance(min_count, int)
                or min_count < 1
            ):
                continue
            compiled = re.compile(pattern, re.MULTILINE)
            rules.append(
                Rule(rule_id, compiled, severity, label, tuple(good), min_count)
            )
        except (KeyError, TypeError, ValueError, re.error):
            continue
    return rules


def _mask_span(buffer: list[str], start: int, end: int) -> None:
    for index in range(max(start, 0), min(end, len(buffer))):
        if buffer[index] not in {"\n", "\r"}:
            buffer[index] = " "


def _copy_span(buffer: list[str], text: str, start: int, end: int) -> None:
    for index in range(max(start, 0), min(end, len(buffer))):
        buffer[index] = text[index]


def _blank_buffer(text: str) -> list[str]:
    return [character if character in {"\n", "\r"} else " " for character in text]


def _iter_tag_spans(text: str, multiline: bool = True) -> Iterable[tuple[int, int]]:
    """Yield lightweight HTML/JSX tag spans while respecting quoted > signs."""
    index = 0
    while index < len(text):
        start = text.find("<", index)
        if start < 0:
            return
        probe = start + 1
        if probe < len(text) and text[probe] == "/":
            probe += 1
        if probe < len(text) and text[probe] == ">":
            yield start, probe + 1
            index = probe + 1
            continue
        if probe >= len(text) or text[probe] not in "!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
            index = start + 1
            continue
        quote: Optional[str] = None
        cursor = probe + 1
        while cursor < len(text):
            character = text[cursor]
            if not multiline and character in {"\r", "\n"}:
                break
            if quote is not None:
                if character == "\\":
                    cursor += 2
                    continue
                if character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == ">":
                yield start, cursor + 1
                index = cursor + 1
                break
            cursor += 1
        else:
            return
        if cursor >= len(text) or text[cursor] != ">":
            index = start + 1


def _quoted_contents(text: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    index = start
    while index < end:
        if text[index] not in {"'", '"'}:
            index += 1
            continue
        quote = text[index]
        content_start = index + 1
        index += 1
        while index < end:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                yield content_start, index
                index += 1
                break
            index += 1


def _preprocess_prose(text: str) -> str:
    buffer = list(text)

    for match in re.finditer(r"(?m)^[ \t]*(?:import|export)\b[^\r\n]*(?:\r?\n|$)", text):
        _mask_span(buffer, match.start(), match.end())

    frontmatter = re.match(r"\A(?:\ufeff)?---[ \t]*(?:\r?\n|$)", text)
    if frontmatter:
        closing = re.search(
            r"(?m)^---[ \t]*(?:\r?\n|$)", text[frontmatter.end() :]
        )
        if closing:
            close_start = frontmatter.end() + closing.start()
            close_end = frontmatter.end() + closing.end()
            _mask_span(buffer, 0, close_end)
            frontmatter_body = text[frontmatter.end() : close_start]
            for value_match in re.finditer(
                r"(?m)^(?:title|description):[ \t]*(?P<value>[^\r\n]*)",
                frontmatter_body,
            ):
                value_start = frontmatter.end() + value_match.start("value")
                value_end = frontmatter.end() + value_match.end("value")
                while value_end > value_start and text[value_end - 1] in {" ", "\t"}:
                    value_end -= 1
                if value_start >= value_end:
                    continue
                quote = text[value_start]
                if quote in {"'", '"'}:
                    cursor = value_start + 1
                    while cursor < value_end:
                        if text[cursor] == "\\" and quote == '"':
                            cursor += 2
                            continue
                        if text[cursor] == quote:
                            _copy_span(buffer, text, value_start + 1, cursor)
                            break
                        cursor += 1
                else:
                    comment = re.search(r"[ \t]+#", text[value_start:value_end])
                    if comment:
                        value_end = value_start + comment.start()
                    _copy_span(buffer, text, value_start, value_end)

    position = 0
    fence_char: Optional[str] = None
    fence_size = 0
    fence_start = 0
    for line in text.splitlines(keepends=True):
        line_end = position + len(line)
        visible = "".join(buffer[position:line_end]).rstrip("\r\n")
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})", visible)
        if fence_char is None and marker:
            token = marker.group(1)
            fence_char = token[0]
            fence_size = len(token)
            fence_start = position
        elif fence_char is not None:
            close_pattern = r"^[ \t]*" + re.escape(fence_char) + "{" + str(fence_size) + r",}[ \t]*$"
            if re.match(close_pattern, visible):
                _mask_span(buffer, fence_start, line_end)
                fence_char = None
                fence_size = 0
        position = line_end
    if fence_char is not None:
        _mask_span(buffer, fence_start, len(buffer))

    masked = "".join(buffer)
    # Markdown リンク・画像のリンク先 ](…) は可視テキストでない（目次の anchor に
    # 「ではなく」が含まれて critical になる誤検知を防ぐ）。表示テキスト [..] は残す。
    for match in re.finditer(r"\]\(([^()\r\n]*(?:\([^()\r\n]*\)[^()\r\n]*)*)\)", masked):
        _mask_span(buffer, match.start() + 2, match.end() - 1)
    masked = "".join(buffer)
    for pattern in (
        re.compile(r"\{/\*.*?\*/\}", re.DOTALL),
        re.compile(r"<!--.*?-->", re.DOTALL),
    ):
        for match in pattern.finditer(masked):
            _mask_span(buffer, match.start(), match.end())
        masked = "".join(buffer)

    for pattern in (
        re.compile(r"(`+)([^\r\n]*?)\1"),
        re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+"),
    ):
        for match in pattern.finditer(masked):
            _mask_span(buffer, match.start(), match.end())
        masked = "".join(buffer)

    for start, end in _iter_tag_spans(text, multiline=False):
        if not any(buffer[index].strip() for index in range(start, end)):
            continue
        _mask_span(buffer, start, end)
        for content_start, content_end in _quoted_contents(text, start, end):
            if JAPANESE_RE.search(text[content_start:content_end]):
                _copy_span(buffer, text, content_start, content_end)

    return "".join(buffer)


def _scan_quoted(text: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _scan_template(text: str, start: int) -> int:
    """Return the end of a template literal, including nested templates."""
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            return index + 1
        if text.startswith("${", index):
            index = _template_expression_end(text, index + 2)
            continue
        index += 1
    return len(text)


def _template_expression_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text) and depth:
        character = text[index]
        if character in {"'", '"'}:
            index = _scan_quoted(text, index, character)
            continue
        if character == "`":
            index = _scan_template(text, index)
            continue
        if character == "{" :
            depth += 1
        elif character == "}":
            depth -= 1
        index += 1
    return index


def _copy_nested_template_contents(
    buffer: list[str], text: str, start: int, end: int
) -> None:
    """Copy string and template literal text inside a ${...} expression."""
    cursor = start
    while cursor < end:
        character = text[cursor]
        if character in {"'", '"'}:
            quoted_end = _scan_quoted(text, cursor, character)
            content_end = quoted_end - 1 if quoted_end <= end else end
            _copy_span(buffer, text, cursor + 1, content_end)
            cursor = quoted_end
            continue
        if character == "`":
            template_end = _scan_template(text, cursor)
            _copy_template_contents(buffer, text, cursor, template_end)
            cursor = template_end
            continue
        cursor += 1


def _copy_template_contents(buffer: list[str], text: str, start: int, end: int) -> None:
    cursor = start + 1
    literal_start = cursor
    while cursor < end - 1:
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text.startswith("${", cursor):
            _copy_span(buffer, text, literal_start, cursor)
            expression_start = cursor + 2
            cursor = _template_expression_end(text, expression_start)
            _copy_nested_template_contents(
                buffer, text, expression_start, max(expression_start, cursor - 1)
            )
            literal_start = cursor
            continue
        cursor += 1
    _copy_span(buffer, text, literal_start, end - 1)


# "<" は含めない: JSX の終了タグ </strong> の "/" を正規表現の開始と誤判定し、
# 次の "/" までマスクしてタグ直後の表現を見逃すため（2026-09-02 実害）。
REGEX_PREFIX_CHARS = frozenset("(,=:[!&|?{};+-*%>~^")
REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "delete",
        "do",
        "else",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)


def _is_regex_literal_start(text: str, start: int) -> bool:
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    prefix = text[line_start:start].rstrip()
    if not prefix:
        return True
    if prefix[-1] in REGEX_PREFIX_CHARS:
        return True
    keyword = re.search(r"([A-Za-z_$][A-Za-z0-9_$]*)$", prefix)
    return bool(keyword and keyword.group(1) in REGEX_PREFIX_KEYWORDS)


def _scan_regex_literal(text: str, start: int) -> Optional[int]:
    """Return the end of a JavaScript regex literal, or None if unterminated."""
    index = start + 1
    in_character_class = False
    while index < len(text):
        character = text[index]
        if character in {"\r", "\n"}:
            return None
        if character == "\\":
            index += 2
            continue
        if character == "[":
            in_character_class = True
        elif character == "]" and in_character_class:
            in_character_class = False
        elif character == "/" and not in_character_class:
            index += 1
            while index < len(text) and (
                "A" <= text[index] <= "Z" or "a" <= text[index] <= "z"
            ):
                index += 1
            return index
        index += 1
    return None


def _preprocess_code(text: str) -> str:
    buffer = _blank_buffer(text)
    occupied = [False] * len(text)
    for match in re.finditer(
        r"(?m)^[ \t]*(?:"
        r"import\b|"
        r"export[ \t]+(?:\*|\{[^}\r\n]*\}|type[ \t]+\{[^}\r\n]*\})"
        r"[ \t]+from\b"
        r")[^\r\n;]*(?:;|(?=\r?$))",
        text,
    ):
        occupied[match.start():match.end()] = [True] * (match.end() - match.start())

    index = 0
    while index < len(text):
        if occupied[index]:
            index += 1
            continue
        if text.startswith("//", index) and not (index > 0 and text[index - 1] == ":"):
            # "https://" のようにコロン直後の // は URL なのでコメントにしない
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            occupied[index:end] = [True] * (end - index)
            index = end
            continue
        if text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            end = len(text) if closing < 0 else closing + 2
            occupied[index:end] = [True] * (end - index)
            index = end
            continue
        if text[index] == "/" and _is_regex_literal_start(text, index):
            end = _scan_regex_literal(text, index)
            if end is not None:
                occupied[index:end] = [True] * (end - index)
                index = end
                continue
        quote = text[index]
        if quote in {"'", '"', "`"}:
            end = (
                _scan_template(text, index)
                if quote == "`"
                else _scan_quoted(text, index, quote)
            )
            occupied[index:end] = [True] * (end - index)
            if quote == "`":
                _copy_template_contents(buffer, text, index, end)
            else:
                _copy_span(buffer, text, index + 1, max(index + 1, end - 1))
            index = end
            continue
        index += 1

    depth = 0
    previous_end: Optional[int] = None
    for start, end in _iter_tag_spans(text):
        if occupied[start]:
            continue
        tag = text[start:end]
        closing = tag.startswith("</")
        self_closing = tag.rstrip().endswith("/>")
        if previous_end is not None and depth > 0:
            brace_depth = 0
            for cursor in range(previous_end, start):
                if occupied[cursor]:
                    continue
                if text[cursor] == "{":
                    brace_depth += 1
                elif text[cursor] == "}" and brace_depth:
                    brace_depth -= 1
                elif brace_depth == 0:
                    buffer[cursor] = text[cursor]
        if closing:
            depth = max(0, depth - 1)
        elif not self_closing:
            depth += 1
        previous_end = end
    return "".join(buffer)


def _preprocess_json(text: str) -> str:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return _preprocess_code(text)
    buffer = _blank_buffer(text)
    index = 0
    while index < len(text):
        if text[index] != '"':
            index += 1
            continue
        end = _scan_quoted(text, index, '"')
        closing = max(index + 1, end - 1)
        probe = end
        while probe < len(text) and text[probe].isspace():
            probe += 1
        if probe >= len(text) or text[probe] != ":":
            _copy_span(buffer, text, index + 1, closing)
        index = end
    return "".join(buffer)


def _preprocess_html(text: str) -> str:
    buffer = list(text)
    blocked = [False] * len(text)
    for pattern in (
        re.compile(r"<!--.*?-->", re.DOTALL),
        re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL),
    ):
        for match in pattern.finditer(text):
            _mask_span(buffer, match.start(), match.end())
            blocked[match.start():match.end()] = [True] * (match.end() - match.start())
    attribute_re = re.compile(
        r"(?is)\b(?:alt|title|placeholder)\s*=\s*(['\"])(.*?)\1"
    )
    for start, end in _iter_tag_spans(text):
        if blocked[start]:
            continue
        tag = text[start:end]
        _mask_span(buffer, start, end)
        for match in attribute_re.finditer(tag):
            value_start = start + match.start(2)
            value_end = start + match.end(2)
            _copy_span(buffer, text, value_start, value_end)
    return "".join(buffer)


def preprocess_text(text: str, mode: str = "prose") -> str:
    """Mask non-visible regions without changing length or newline positions."""
    processors = {
        "prose": _preprocess_prose,
        "code": _preprocess_code,
        "json": _preprocess_json,
        "html": _preprocess_html,
    }
    processor = processors.get(mode, _preprocess_prose)
    result = processor(text)
    if len(result) != len(text) or [i for i, c in enumerate(result) if c == "\n"] != [
        i for i, c in enumerate(text) if c == "\n"
    ]:
        raise ValueError("preprocessing must preserve length and newline positions")
    return result


def count_japanese(text: str) -> int:
    return len(JAPANESE_RE.findall(text))


def split_sentences(masked: str, original: Optional[str] = None) -> list[Sentence]:
    """Split at Japanese terminators and newlines while preserving source spans."""
    original = masked if original is None else original
    if len(masked) != len(original):
        raise ValueError("masked and original text must have the same length")
    sentences: list[Sentence] = []

    def add_segment(start: int, end: int, terminator: Optional[str], fragment: bool) -> None:
        if end <= start or not masked[start:end].strip():
            return
        report_start = start
        report_end = end
        while report_start < report_end and original[report_start].isspace():
            report_start += 1
        while report_end > report_start and original[report_end - 1].isspace():
            report_end -= 1
        if report_end <= report_start:
            return
        sentences.append(
            Sentence(
                start=report_start,
                end=report_end,
                line=original.count("\n", 0, report_start) + 1,
                original=original[report_start:report_end],
                masked=masked[report_start:report_end],
                terminator=terminator,
                is_fragment=fragment,
            )
        )

    cursor = 0
    for boundary in BOUNDARY_RE.finditer(masked):
        token = boundary.group(0)
        if token in {"\n", "\r\n"}:
            add_segment(cursor, boundary.start(), None, True)
        else:
            add_segment(cursor, boundary.end(), token, False)
        cursor = boundary.end()
    add_segment(cursor, len(masked), None, True)
    return sentences


def _sentence_for_offset(sentences: Sequence[Sentence], offset: int) -> Optional[Sentence]:
    for sentence in sentences:
        if sentence.start <= offset < sentence.end:
            return sentence
    return None


def _line_sentence(original: str, offset: int) -> str:
    start = original.rfind("\n", 0, offset) + 1
    end = original.find("\n", offset)
    if end < 0:
        end = len(original)
    return original[start:end].strip()


ENDING_PATTERNS: tuple[tuple[Pattern[str], str], ...] = (
    (re.compile(r"ていました$"), "ていました"),
    (re.compile(r"ています$"), "ています"),
    (re.compile(r"ませんでした$"), "ません"),
    (re.compile(r"ません$"), "ません"),
    (re.compile(r"ました$"), "ました"),
    (re.compile(r"でした$"), "でした"),
    (re.compile(r"でしょう$"), "でしょう"),
    (re.compile(r"である$"), "である"),
    (re.compile(r"ます$"), "ます"),
    (re.compile(r"です$"), "です"),
    (re.compile(r"している$"), "している"),
    (re.compile(r"した$"), "した"),
    (re.compile(r"する$"), "する"),
    (re.compile(r"だった$"), "だ"),
    (re.compile(r"だ$"), "だ"),
)


def _sentence_body(sentence: Sentence) -> str:
    return re.sub(r"[。！？]+$", "", sentence.original.strip()).rstrip()


def normalize_ending(sentence: Sentence) -> Optional[str]:
    if sentence.is_fragment:
        return None
    body = _sentence_body(sentence)
    for pattern, normalized in ENDING_PATTERNS:
        if pattern.search(body):
            return normalized
    return None


def _monotone_violations(sentences: Sequence[Sentence], file: str) -> list[Violation]:
    violations: list[Violation] = []
    run: list[Sentence] = []
    run_ending: Optional[str] = None

    def flush() -> None:
        nonlocal run, run_ending
        if run_ending is not None and len(run) >= 3:
            quote = " / ".join(item.original.strip() for item in run[:3])
            violations.append(
                Violation(
                    file=file,
                    line=run[0].line,
                    end_line=run[-1].line,
                    rule_id="style-ending-001",
                    severity="warn",
                    label=f"同じ文末「{run_ending}」が3文以上連続している",
                    sentence=quote,
                    good=(
                        "文の役割に合わせて文末表現を変える",
                        "文を結合または分割してリズムを整える",
                    ),
                )
            )
        run = []
        run_ending = None

    for sentence in sentences:
        ending = normalize_ending(sentence)
        if ending is None:
            flush()
        elif ending == run_ending:
            run.append(sentence)
        else:
            flush()
            run_ending = ending
            run = [sentence]
    flush()
    return violations


POLITE_RE = re.compile(r"(?:ていました|ています|ませんでした|ません|ました|でした|でしょう|ます|です)$")
PLAIN_RE = re.compile(r"(?:である|だった|だ|している|した|する|となる|になる|ない)$")


def _style_class(sentence: Sentence) -> Optional[str]:
    if sentence.is_fragment or sentence.terminator != "。":
        return None
    body = _sentence_body(sentence)
    if POLITE_RE.search(body):
        return "polite"
    if PLAIN_RE.search(body):
        return "plain"
    return None


def _mixed_style_violations(sentences: Sequence[Sentence], file: str) -> list[Violation]:
    polite = [sentence for sentence in sentences if _style_class(sentence) == "polite"]
    plain = [sentence for sentence in sentences if _style_class(sentence) == "plain"]
    total = len(polite) + len(plain)
    if (
        len(polite) < 2
        or len(plain) < 2
        or total == 0
        or min(len(polite), len(plain)) / total < 0.20
    ):
        return []
    examples = sorted((polite[0], plain[0]), key=lambda item: item.start)
    return [
        Violation(
            file=file,
            line=examples[0].line,
            end_line=max(item.line for item in (*polite, *plain)),
            rule_id="style-mixed-001",
            severity="warn",
            label=f"敬体と常体が混在している（敬体{len(polite)}文・常体{len(plain)}文）",
            sentence=" / ".join(item.original.strip() for item in examples),
            good=(
                "文書全体を敬体か常体のどちらかに統一する",
                "引用や固有の表現を除き、段落ごとの文体もそろえる",
            ),
        )
    ]



JSON_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _json_escape_violations(text: str, file: str) -> list[Violation]:
    """JSON・コード内で日本語を \\uXXXX に逃がした値を critical にする。

    生テキストを検査する仕組みのため、エスケープされた日本語は全ルールを素通りする。
    書き直しの代わりにエスケープで検査を回避した実例（2026-09-02）があるので、
    日本語の符号位置をエスケープで書くこと自体を違反とする。
    """
    violations: list[Violation] = []
    seen_lines: set[int] = set()
    for match in JSON_UNICODE_ESCAPE_RE.finditer(text):
        char = chr(int(match.group(1), 16))
        if not JAPANESE_RE.search(char):
            continue
        line_no = text.count("\n", 0, match.start()) + 1
        if line_no in seen_lines:
            continue  # 1行に複数のエスケープがあっても報告は行ごとに1件
        seen_lines.add(line_no)
        violations.append(
            Violation(
                file=file,
                line=text.count("\n", 0, match.start()) + 1,
                rule_id="json-escape-001",
                severity="critical",
                label="日本語を \\u エスケープで書いている",
                sentence=_line_sentence(text, match.start()),
                good=("日本語はエスケープせずそのまま書く", "表現を変えたい場合は文を書き直す"),
            )
        )
    return violations

def lint_text(
    text: str,
    file: str = "<text>",
    profile: Optional[str] = None,
    config: Optional[dict[str, object]] = None,
    rules: Optional[Sequence[Rule]] = None,
    enforce_min_japanese: bool = True,
    mode: Optional[str] = None,
) -> list[Violation]:
    """Lint text; the Japanese threshold gates only prose structure rules."""
    config = _normalize_config(config) if config is not None else load_config()
    active_mode = mode or mode_for_path(file, config) or "prose"
    masked = preprocess_text(text, active_mode)
    sentences = split_sentences(masked, text)
    active_rules = (
        list(rules)
        if rules is not None
        else load_rules(
            profile,
            config=config,
            repo_path=file if file != "<text>" else None,
        )
    )
    violations: list[Violation] = []
    for rule in active_rules:
        matches = list(rule.regex.finditer(masked))
        if len(matches) < rule.min_count:
            continue
        for match in matches:
            sentence = _sentence_for_offset(sentences, match.start())
            quote = sentence.original.strip() if sentence else _line_sentence(text, match.start())
            # 日本語を含まない文（多言語ページの英語・スペイン語行など）は対象外。
            # 全ルールが日本語の言い回しを狙うため、ASCII 記号だけの一致は誤検知になる。
            if not JAPANESE_RE.search(sentence.masked if sentence else quote):
                continue
            line = text.count("\n", 0, match.start()) + 1
            end_line = text.count("\n", 0, match.end()) + 1
            violations.append(
                Violation(
                    file=file,
                    line=line,
                    rule_id=rule.id,
                    severity=rule.severity,
                    label=rule.label,
                    sentence=quote,
                    good=rule.good,
                    end_line=end_line,
                )
            )
    structure_enabled = not enforce_min_japanese or count_japanese(masked) >= int(
        config.get("min_japanese_chars", 30)
    )
    if active_mode == "prose" and structure_enabled:
        violations.extend(_monotone_violations(sentences, file))
        violations.extend(_mixed_style_violations(sentences, file))
    if active_mode in {"code", "json"}:
        # コメント・正規表現リテラルをマスクした後のテキストに対して検査する
        # （/[\u3040-\u309f]/ や説明コメントの表記を回避扱いしないため）
        violations.extend(_json_escape_violations(masked, file))
    return sorted(
        violations,
        key=lambda item: (item.line, 0 if item.severity == "critical" else 1, item.rule_id),
    )


def lint_file(
    path: Union[str, os.PathLike[str]],
    profile: Optional[str] = None,
    config: Optional[dict[str, object]] = None,
    mode: Optional[str] = None,
) -> list[Violation]:
    absolute = _absolute_path(path)
    active_config = _normalize_config(config) if config is not None else load_config()
    if not is_target_path(absolute, config=active_config):
        return []
    text = Path(absolute).read_text(encoding="utf-8", errors="replace")
    return lint_text(
        text,
        file=absolute,
        profile=profile,
        config=active_config,
        mode=mode,
    )


def shorten_sentence(sentence: str, limit: int = 80) -> str:
    compact = re.sub(r"\s+", " ", sentence).strip()
    if len(compact) <= limit:
        return compact
    left = (limit - 1) // 2
    right = limit - 1 - left
    return compact[:left] + "…" + compact[-right:]


def format_violation(violation: Violation, include_file: bool = False) -> str:
    lines: list[str] = []
    if include_file:
        lines.append(f"対象ファイル: {violation.file}")
    lines.extend(
        [
            f"[ja-lint {violation.severity}] {violation.label}（rule: {violation.rule_id}, L{violation.line}）",
            f"  該当文: 「{shorten_sentence(violation.sentence)}」",
            f"  グッドパターン: {' / '.join(violation.good)}",
        ]
    )
    return "\n".join(lines)


def build_warning(
    violations: Sequence[Violation],
    max_count: Optional[int] = 5,
    instruction: str = REWRITE_INSTRUCTION,
    include_files: bool = False,
) -> str:
    shown = list(violations if max_count is None else violations[:max_count])
    blocks = [format_violation(item, include_file=include_files) for item in shown]
    parts = [instruction]
    if blocks:
        parts.append("\n\n".join(blocks))
    if max_count is not None and len(violations) > max_count:
        remainder = len(violations) - max_count
        parts.append(
            f"ほか {remainder} 件（全件は `python3 {BASE_DIR / 'ja_lint.py'} <file>` で確認）"
        )
    return "\n\n".join(parts)


def highest_exit_code(violations: Iterable[Violation]) -> int:
    severities = {item.severity for item in violations}
    if "critical" in severities:
        return 2
    if "warn" in severities:
        return 1
    return 0


def _test_rule(rule_id: str, text: str, profile: Optional[str]) -> int:
    rules = [rule for rule in load_rules(profile) if rule.id == rule_id]
    if not rules:
        print("UNKNOWN RULE")
        return 3
    masked = preprocess_text(text)
    rule = rules[0]
    matched = len(list(rule.regex.finditer(masked))) >= rule.min_count
    print("MATCH" if matched else "NO MATCH")
    return 0 if matched else 1


HUNK_HEADER_RE = re.compile(rb"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _changed_line_numbers(patch: bytes) -> set[int]:
    lines: set[int] = set()
    for match in HUNK_HEADER_RE.finditer(patch):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count:
            lines.update(range(start, start + count))
    return lines


def _decode_git_error(value: bytes) -> str:
    return value.decode(errors="replace").strip()


def _git_name_status_records(output: bytes) -> list[tuple[str, Optional[str], str]]:
    """Parse `git diff --name-status -z` without interpreting path escapes."""
    fields = output.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    records: list[tuple[str, Optional[str], str]] = []
    index = 0
    while index < len(fields):
        status = os.fsdecode(fields[index])
        index += 1
        if status.startswith("R"):
            if index + 1 >= len(fields):
                break
            old_path = os.fsdecode(fields[index])
            new_path = os.fsdecode(fields[index + 1])
            index += 2
            records.append((status, old_path, new_path))
        else:
            if index >= len(fields):
                break
            new_path = os.fsdecode(fields[index])
            index += 1
            records.append((status, None, new_path))
    return records


def _matches_repo_include_globs(path: str) -> bool:
    repo_root, repo_config = _repo_context(path)
    if repo_root is None or "include_globs" not in repo_config:
        return True
    include_globs = repo_config.get("include_globs")
    if not isinstance(include_globs, list) or not all(
        isinstance(pattern, str) for pattern in include_globs
    ):
        raise ValueError(".ja-lint.json の include_globs が不正です")
    relpath = os.path.relpath(path, repo_root).replace(os.sep, "/")
    return any(
        _glob_match(relpath, pattern)
        for pattern in include_globs
    )


def _git_changed_files(
    base_ref: str, cwd: str
) -> tuple[Optional[str], dict[str, set[int]]]:
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return str(error), {}
    if root_result.returncode != 0:
        return root_result.stderr.strip() or "git リポジトリではありません", {}
    repo_root = root_result.stdout.strip()
    _config_root, repo_config = _repo_context(repo_root)
    include_globs = repo_config.get("include_globs")
    if "include_globs" in repo_config and (
        not isinstance(include_globs, list)
        or not all(isinstance(item, str) for item in include_globs)
    ):
        return ".ja-lint.json の include_globs が不正です", {}
    scopes = (
        [f"{base_ref}...HEAD"],
        ["--cached"],
        [],
    )
    changed: dict[str, set[int]] = collections.defaultdict(set)
    for scope in scopes:
        command = [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--diff-filter=AMR",
            "-M",
            *scope,
        ]
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return _decode_git_error(result.stderr) or "git diff に失敗しました", {}
        for status, old_path, new_path in _git_name_status_records(result.stdout):
            pathspecs = [item for item in (old_path, new_path) if item is not None]
            patch_result = subprocess.run(
                [
                    "git",
                    "diff",
                    "-U0",
                    "-z",
                    "--diff-filter=AMR",
                    "-M",
                    *scope,
                    "--",
                    *pathspecs,
                ],
                cwd=repo_root,
                capture_output=True,
                check=False,
            )
            if patch_result.returncode != 0:
                return (
                    _decode_git_error(patch_result.stderr) or "git diff に失敗しました",
                    {},
                )
            absolute = _absolute_path(new_path, repo_root)
            patch_lines = _changed_line_numbers(patch_result.stdout)
            if status.startswith("R") and not patch_lines:
                try:
                    contents = Path(absolute).read_bytes()
                except OSError:
                    contents = b""
                line_count = contents.count(b"\n")
                if contents and not contents.endswith(b"\n"):
                    line_count += 1
                patch_lines.update(range(1, line_count + 1))
            changed[absolute].update(patch_lines)
    untracked_result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if untracked_result.returncode != 0:
        return (
            _decode_git_error(untracked_result.stderr) or "git ls-files に失敗しました",
            {},
        )
    for raw_path in untracked_result.stdout.split(b"\0"):
        if not raw_path:
            continue
        absolute = _absolute_path(os.fsdecode(raw_path), repo_root)
        try:
            newline_count = Path(absolute).read_bytes().count(b"\n")
        except OSError:
            newline_count = 0
        changed[absolute].update(range(1, newline_count + 2))
    config = load_config()
    selected: dict[str, set[int]] = {}
    for path in sorted(changed):
        if not is_target_path(path, config=config):
            continue
        try:
            included = _matches_repo_include_globs(path)
        except ValueError as error:
            return str(error), {}
        if included:
            selected[path] = changed[path]
    return None, selected


def _all_configured_files(cwd: str) -> tuple[Optional[str], list[str]]:
    repo_root, repo_config = _repo_context(cwd)
    if repo_root is None:
        return ".ja-lint.json が見つかりません", []
    include_globs = repo_config.get("include_globs")
    if not isinstance(include_globs, list) or not all(
        isinstance(item, str) for item in include_globs
    ):
        return ".ja-lint.json の include_globs が不正です", []
    config = load_config()
    selected: list[str] = []
    for directory, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules"}]
        for filename in filenames:
            absolute = os.path.join(directory, filename)
            relpath = os.path.relpath(absolute, repo_root).replace(os.sep, "/")
            if not any(_glob_match(relpath, pattern) for pattern in include_globs):
                continue
            if is_target_path(absolute, config=config):
                selected.append(_absolute_path(absolute))
    return None, sorted(set(selected))


def _output_root(cwd: str) -> str:
    repo_root, _repo_config = _repo_context(cwd)
    return repo_root or _absolute_path(cwd)


def _relative_output_path(path: str, base_dir: str) -> str:
    return os.path.relpath(path, base_dir).replace(os.sep, "/")


def _write_report(
    path: str,
    violations: Sequence[Violation],
    base_dir: Optional[str] = None,
) -> None:
    destination = Path(_absolute_path(path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_root = _output_root(os.getcwd()) if base_dir is None else _absolute_path(base_dir)
    ordered_violations = sorted(
        violations,
        key=lambda item: (
            _relative_output_path(item.file, output_root),
            item.line,
            item.rule_id,
        ),
    )
    severity_counts = collections.Counter(item.severity for item in violations)
    rule_counts = collections.Counter(item.rule_id for item in violations)
    relative_names = {
        item.file: _relative_output_path(item.file, output_root)
        for item in ordered_violations
    }
    file_counts = collections.Counter(
        relative_names[item.file] for item in ordered_violations
    )
    details: dict[str, Violation] = {}
    samples: dict[str, list[Violation]] = collections.defaultdict(list)
    for item in ordered_violations:
        details.setdefault(item.rule_id, item)
        if len(samples[item.rule_id]) < 2:
            samples[item.rule_id].append(item)

    lines = [
        "# ja-lint レポート",
        "",
        f"- 総件数: {len(violations)}",
        f"- critical: {severity_counts['critical']}",
        f"- warn: {severity_counts['warn']}",
        "",
        "## ルール別件数",
        "",
        "| rule_id | severity | label | 件数 |",
        "|---|---|---|---:|",
    ]
    for rule_id, count in sorted(rule_counts.items(), key=lambda item: (-item[1], item[0])):
        detail = details[rule_id]
        label = detail.label.replace("|", "\\|")
        lines.append(f"| {rule_id} | {detail.severity} | {label} | {count} |")
    if not rule_counts:
        lines.append("| - | - | - | 0 |")

    lines.extend(
        [
            "",
            "## ファイル別件数（上位30）",
            "",
            "| リポジトリ相対パス | 件数 |",
            "|---|---:|",
        ]
    )
    for file_name, count in sorted(file_counts.items(), key=lambda item: (-item[1], item[0]))[:30]:
        escaped_name = file_name.replace("|", "\\|")  # f-string 式内のバックスラッシュは 3.11 以下で SyntaxError
        lines.append(f"| {escaped_name} | {count} |")
    if not file_counts:
        lines.append("| - | 0 |")

    lines.extend(["", "## ルール別サンプル", ""])
    for rule_id, _count in sorted(rule_counts.items(), key=lambda item: (-item[1], item[0])):
        detail = details[rule_id]
        lines.append(f"### {rule_id}: {detail.label}")
        lines.append("")
        for item in samples[rule_id]:
            lines.append(
                f"- {relative_names[item.file]}:{item.line} — "
                f"{shorten_sentence(item.sentence, 120)}"
            )
        lines.append("")
    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _annotation(violation: Violation, base_dir: str) -> str:
    level = "error" if violation.severity == "critical" else "warning"
    relpath = _relative_output_path(violation.file, base_dir)
    label = violation.label.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return (
        f"::{level} file={relpath},line={violation.line}::"
        f"{violation.rule_id} {label}"
    )


def _exit_for_fail_on(violations: Sequence[Violation], fail_on: str) -> int:
    if fail_on == "none":
        return 0
    code = highest_exit_code(violations)
    if fail_on == "critical" and code < 2:
        return 0
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="日本語ドキュメントの定型表現を検査する")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--test-rule", metavar="RULE_ID")
    parser.add_argument("--text", metavar="TEXT")
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument("--changed", metavar="BASE_REF")
    source_group.add_argument("--all", action="store_true", dest="all_files")
    parser.add_argument(
        "--fail-on",
        choices=("critical", "warn", "none"),
        default="warn",
    )
    parser.add_argument("--report", metavar="PATH_MD")
    parser.add_argument("--github-annotations", action="store_true")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    if args.test_rule:
        if args.text is None:
            parser.error("--test-rule には --text が必要です")
        return _test_rule(args.test_rule, args.text, None)
    if args.files and (args.changed is not None or args.all_files):
        parser.error("files と --changed/--all は同時に指定できません")
    if args.as_json and args.github_annotations:
        parser.error("--json と --github-annotations は同時に指定できません")

    cwd = os.getcwd()
    files = list(args.files)
    changed_lines: Optional[dict[str, set[int]]] = None
    error: Optional[str] = None
    if args.changed is not None:
        error, changed_lines = _git_changed_files(args.changed, cwd)
        files = list(changed_lines)
    elif args.all_files:
        error, files = _all_configured_files(cwd)
    elif not files:
        parser.error("検査対象ファイルを1つ以上指定してください")
    if error is not None:
        print(f"ja-lint: {error}", file=sys.stderr)
        return 4

    all_violations: list[Violation] = []
    per_file: list[tuple[str, list[Violation]]] = []
    had_file_error = False
    for file_name in files:
        absolute = _absolute_path(file_name)
        try:
            violations = lint_file(absolute)
        except OSError as file_error:
            print(f"ja-lint: {absolute}: {file_error}", file=sys.stderr)
            had_file_error = True
            violations = []
        if changed_lines is not None:
            lines = changed_lines.get(absolute, set())
            violations = [
                item
                for item in violations
                if not lines.isdisjoint(range(item.line, int(item.end_line) + 1))
            ]
        per_file.append((absolute, violations))
        all_violations.extend(violations)

    output_root = _output_root(cwd)
    if args.report:
        try:
            _write_report(args.report, all_violations, base_dir=output_root)
        except OSError as error:
            print(f"ja-lint: report: {error}", file=sys.stderr)
            return 4

    if args.as_json:
        print(
            json.dumps(
                [item.as_dict() for item in all_violations],
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.github_annotations:
        annotations = "\n".join(
            _annotation(item, output_root)
            for item in sorted(
                all_violations,
                key=lambda value: (
                    _relative_output_path(value.file, output_root),
                    value.line,
                    value.rule_id,
                ),
            )
        )
        if annotations:
            print(annotations)
    else:
        reports: list[str] = []
        if changed_lines is not None:
            reports.append("ja-lint: 変更行のみ検査")
        for absolute, violations in per_file:
            if violations:
                reports.append(
                    f"{absolute}\n{build_warning(violations, max_count=None)}"
                )
            else:
                reports.append(f"{absolute}: 違反なし")
        print("\n\n".join(reports))
    if had_file_error:
        return 4
    return _exit_for_fail_on(all_violations, args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
