#!/usr/bin/env python3
"""Codex / Claude Code PostToolUse hook for ja_lint."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import ja_lint


STATE_DIR = Path(__file__).resolve().parent / "state"
PATH_TOOLS = {"Edit", "Write", "MultiEdit"}
COMMAND_TOOLS = {"Bash"}
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")

# Bash はコマンド文字列しか渡さないため、書き込み先を静的に特定できない。
# コマンド中に現れた対象拡張子のパスのうち、直前に更新されたものだけを検査する。
DEFAULT_MTIME_WINDOW_SEC = 15.0
MAX_BASH_FILES = 20
MAX_LOG_BYTES = 1024 * 1024


def _log_exception(hook_name: str, error: BaseException) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "ja_lint.log"
        message = str(error).replace("\r", " ").replace("\n", " ")[:200]
        line = (
            f"{datetime.now().astimezone().isoformat()} {hook_name} "
            f"{type(error).__name__} {message}\n"
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if log_path.stat().st_size > MAX_LOG_BYTES:
            tail = log_path.read_bytes()[-(MAX_LOG_BYTES // 2):]
            log_path.write_bytes(tail[tail.find(b"\n") + 1 :])
    except BaseException:
        pass


def _state_path(session_id: str) -> Optional[Path]:
    if not SESSION_RE.fullmatch(session_id):
        return None
    return STATE_DIR / f"{session_id}.txt"


def _reported_path(session_id: str) -> Optional[Path]:
    if not SESSION_RE.fullmatch(session_id):
        return None
    return STATE_DIR / f"{session_id}.reported.txt"


def _remember_file(session_id: str, absolute_path: str) -> None:
    state_path = _state_path(session_id)
    if state_path is None:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    try:
        existing = {
            line.strip()
            for line in state_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except FileNotFoundError:
        pass
    if absolute_path not in existing:
        with state_path.open("a", encoding="utf-8") as handle:
            handle.write(absolute_path + "\n")


def _already_reported(session_id: str, absolute_path: str, mtime_ns: int) -> bool:
    """同じ内容（同じ mtime）で二重にブロックしないための記録。"""
    reported = _reported_path(session_id)
    if reported is None:
        return False
    key = f"{absolute_path}\t{mtime_ns}"
    try:
        for line in reported.read_text(encoding="utf-8").splitlines():
            if line.strip() == key:
                return True
    except FileNotFoundError:
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with reported.open("a", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return False


def _candidate_paths(command: str, config: dict) -> list[str]:
    configured = config.get("extensions", {})
    items = configured.keys() if isinstance(configured, dict) else configured
    extensions = [
        str(item).lstrip(".")
        for item in items
        if isinstance(item, str) and str(item).strip(".")
    ]
    if not extensions:
        return []
    alternation = "|".join(re.escape(item) for item in extensions)
    pattern = re.compile(
        r"[A-Za-z0-9_@%+=:,./~-]+\.(?:" + alternation + r")(?![A-Za-z0-9])"
    )
    found: list[str] = []
    for match in pattern.finditer(command):
        candidate = match.group(0)
        if candidate not in found:
            found.append(candidate)
        if len(found) >= MAX_BASH_FILES:
            break
    return found


def _collect_from_bash(payload: dict, config: dict) -> list[str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return []
    cwd = payload.get("cwd")
    window = float(config.get("bash_mtime_window_sec", DEFAULT_MTIME_WINDOW_SEC))
    now = time.time()
    targets: list[str] = []
    for candidate in _candidate_paths(command, config):
        if not ja_lint.is_target_path(candidate, cwd=cwd, config=config):
            continue
        absolute = ja_lint._absolute_path(candidate, cwd)
        try:
            stat = os.stat(absolute)
        except OSError:
            continue
        # 直前に書き換わったファイルだけを対象にする。
        # 読むだけのコマンド（cat / grep 等）は mtime を動かさないので拾わない。
        if now - stat.st_mtime > window:
            continue
        if absolute not in targets:
            targets.append(absolute)
    return targets


def _collect_from_patch(payload: dict, config: dict) -> list[str]:
    """Read Codex apply_patch headers; content lines cannot be headers.

    Codex supplies the freeform patch as tool_input.command. A move replaces
    its Update File target with the destination; deleted files are not linted.
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    paths: list[str] = []
    current: Optional[str] = None
    for line in command.splitlines():
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            if current:
                paths.append(current)
            current = line.split(": ", 1)[1]
        elif line.startswith("*** Move to: ") and current is not None:
            current = line[len("*** Move to: "):]
        elif line.startswith("*** Delete File: ") or line == "*** End Patch":
            if current:
                paths.append(current)
            current = None
    if current:
        paths.append(current)
    targets: list[str] = []
    for path in paths:
        if not ja_lint.is_target_path(path, cwd=payload.get("cwd"), config=config):
            continue
        absolute = ja_lint._absolute_path(path, payload.get("cwd"))
        if os.path.isfile(absolute) and absolute not in targets:
            targets.append(absolute)
    return targets


def main() -> None:
    try:
        if os.environ.get("JA_LINT", "").lower() == "off":
            return
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("permission_mode") == "plan":
            return
        tool_name = payload.get("tool_name")
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        if (
            not isinstance(session_id, str)
            or _state_path(session_id) is None
            or (cwd is not None and not isinstance(cwd, str))
        ):
            return

        config = ja_lint.load_config()
        targets: list[str] = []

        if tool_name in PATH_TOOLS:
            tool_input = payload.get("tool_input")
            if (
                not isinstance(tool_input, dict)
                or not isinstance(tool_input.get("file_path"), str)
                or not tool_input["file_path"]
            ):
                return
            if not ja_lint.is_target_path(
                tool_input["file_path"], cwd=cwd, config=config
            ):
                return
            targets = [ja_lint._absolute_path(tool_input["file_path"], cwd)]
        elif tool_name in COMMAND_TOOLS:
            targets = _collect_from_bash(payload, config)
        elif tool_name == "apply_patch":
            targets = _collect_from_patch(payload, config)
        else:
            return

        if not targets:
            return

        violations = []
        for absolute in targets:
            _remember_file(session_id, absolute)
            found = ja_lint.lint_file(absolute, config=config)
            if not found:
                continue
            try:
                mtime_ns = os.stat(absolute).st_mtime_ns
            except OSError:
                mtime_ns = 0
            if tool_name in COMMAND_TOOLS and _already_reported(
                session_id, absolute, mtime_ns
            ):
                continue
            violations.extend(found)

        if not violations:
            return
        max_count = int(config.get("max_violations_in_warning", 5))
        reason = ja_lint.build_warning(
            violations, max_count=max_count, include_files=len(targets) > 1
        )
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except BaseException as error:
        _log_exception("post_hook", error)
        return


if __name__ == "__main__":
    main()
