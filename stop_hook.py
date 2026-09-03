#!/usr/bin/env python3
"""Claude Code Stop hook that blocks only on remaining critical violations."""

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
SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
MAX_STATE_AGE_SECONDS = 7 * 24 * 60 * 60
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


def _cleanup_old_state() -> None:
    try:
        now = time.time()
        for path in STATE_DIR.glob("*.txt"):
            try:
                if now - path.stat().st_mtime > MAX_STATE_AGE_SECONDS:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        return


def _state_path(session_id: str) -> Optional[Path]:
    if not SESSION_RE.fullmatch(session_id):
        return None
    return STATE_DIR / f"{session_id}.txt"


def main() -> None:
    try:
        if os.environ.get("JA_LINT", "").lower() == "off":
            return
        _cleanup_old_state()
        payload = json.load(sys.stdin)
        if (
            not isinstance(payload, dict)
            or payload.get("stop_hook_active") is True
            or payload.get("permission_mode") == "plan"
        ):
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            return
        state_path = _state_path(session_id)
        if state_path is None or not state_path.is_file():
            return
        config = ja_lint.load_config()
        paths = {
            line.strip()
            for line in state_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        critical: list[ja_lint.Violation] = []
        for file_name in sorted(paths):
            if not os.path.isfile(file_name):
                continue
            if not ja_lint.is_target_path(file_name, config=config):
                continue
            critical.extend(
                item
                for item in ja_lint.lint_file(file_name, config=config)
                if item.severity == "critical"
            )
        if not critical:
            return
        max_count = int(config.get("max_violations_in_warning", 5))
        reason = ja_lint.build_warning(
            critical,
            max_count=max_count,
            instruction=ja_lint.STOP_INSTRUCTION,
            include_files=True,
        )
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except BaseException as error:
        _log_exception("stop_hook", error)
        return


if __name__ == "__main__":
    main()
