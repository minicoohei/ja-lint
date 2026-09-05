"""Codex event regressions; all hook state is isolated in temporary folders."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ja_lint
import post_hook
import stop_hook


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.config = ja_lint.load_config()
        for module in (post_hook, stop_hook):
            state_patch = patch.object(module, "STATE_DIR", self.state)
            state_patch.start()
            self.addCleanup(state_patch.stop)
        env_patch = patch.dict(os.environ, {"JA_LINT": "", "JA_LINT_PROFILE": "business"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def file(self, name, text="この提案は、業務に効く仕組みです。"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def event(self, command, session="codex-1", **updates):
        return dict(tool_name="apply_patch", tool_input={"command": command},
                    cwd=str(self.root), session_id=session, **updates)

    def call(self, module, payload):
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            module.main()
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_multiple_unicode_space_move_delete_and_dedup(self):
        added = self.file("日本語 新規.md")
        moved = self.file("移動 先.md")
        old = self.file("旧.md")  # Even if it exists, lint the destination only.
        self.file("削除.md")
        command = "\n".join([
            "*** Begin Patch", "*** Add File: 日本語 新規.md", "+本文",
            "*** Update File: 旧.md", "*** Move to: 移動 先.md", "@@", "-前", "+後",
            "*** Delete File: 削除.md", "*** Update File: 日本語 新規.md", "@@", "+文",
            "*** End Patch"])
        self.assertEqual([str(added), str(moved)], post_hook._collect_from_patch(self.event(command), self.config))

    def test_session_tracking_fix_delete_and_isolation(self):
        target = self.file("文章.md")
        command = "*** Begin Patch\n*** Update File: 文章.md\n@@\n+本文\n*** End Patch"
        self.assertEqual("block", self.call(post_hook, self.event(command))["decision"])
        self.assertEqual([str(target)], (self.state / "codex-1.txt").read_text().splitlines())
        self.assertIsNone(self.call(stop_hook, {"session_id": "codex-2"}))
        self.assertEqual("block", self.call(stop_hook, {"session_id": "codex-1"})["decision"])
        target.write_text("業務の時間を短縮する仕組みです。", encoding="utf-8")
        self.assertIsNone(self.call(stop_hook, {"session_id": "codex-1"}))
        target.unlink()
        self.assertIsNone(self.call(stop_hook, {"session_id": "codex-1"}))

    def test_exclusions_off_plan_invalid_payload_and_deleted_file(self):
        self.file("AGENTS.md")
        self.file("safe.md")
        command = "*** Begin Patch\n*** Add File: AGENTS.md\n+x\n*** Delete File: safe.md\n*** End Patch"
        self.assertIsNone(self.call(post_hook, self.event(command)))
        valid = "*** Begin Patch\n*** Update File: safe.md\n@@\n+x\n*** End Patch"
        self.assertIsNone(self.call(post_hook, self.event(valid, permission_mode="plan")))
        with patch.dict(os.environ, {"JA_LINT": "off"}):
            self.assertIsNone(self.call(post_hook, self.event(valid)))
        for value in ({}, {"command": None}, "patch"):
            event = self.event(valid)
            event["tool_input"] = value
            self.assertIsNone(self.call(post_hook, event))
        self.assertFalse(self.state.exists())

    def test_legacy_write_and_bash(self):
        target = self.file("legacy.md")
        for name, tool_input in [("Write", {"file_path": str(target)}),
                                 ("Bash", {"command": "touch legacy.md"})]:
            result = self.call(post_hook, dict(tool_name=name, tool_input=tool_input,
                               cwd=str(self.root), session_id=name))
            self.assertEqual("block", result["decision"])
            self.assertEqual([str(target)], (self.state / f"{name}.txt").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
