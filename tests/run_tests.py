#!/usr/bin/env python3
"""Self-contained test suite for ja_lint."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATE_DIR = ROOT / "state"
sys.path.insert(0, str(ROOT))

import ja_lint  # noqa: E402


def subprocess_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("JA_LINT", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(updates)
    return env


def initialize_git_repo(root: Path) -> None:
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "ja-lint@example.test"],
        ["git", "config", "user.name", "ja-lint test"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)


def git_commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=root, check=True, capture_output=True
    )


class JaLintCoreTests(unittest.TestCase):
    def test_ng_fixture_rules_warning_and_aggregates(self) -> None:
        violations = ja_lint.lint_file(FIXTURES / "ng_mixed.md")
        rule_ids = {item.rule_id for item in violations}
        self.assertTrue(
            {"kiku-001", "juyo-001", "style-ending-001", "style-mixed-001"}
            <= rule_ids
        )
        warning = ja_lint.build_warning(violations, max_count=None)
        self.assertIn("この改善は受付業務に効きます。", warning)
        self.assertIn("グッドパターン:", warning)
        self.assertIn("何がどれだけ変わるか", warning)
        self.assertIn("担当者は申請内容を確認します。", warning)

    def test_clean_english_and_code_fixtures_have_no_violations(self) -> None:
        for name in ("clean.md", "english.md", "code_only.md"):
            with self.subTest(name=name):
                self.assertEqual([], ja_lint.lint_file(FIXTURES / name))

    def test_preprocessing_preserves_offsets_and_masks_regions(self) -> None:
        text = (
            "---\ntitle: 効く資料\n---\n"
            "本文は具体的に説明します。\n"
            "```\n効くということです。\n```\n"
            "URL https://example.test/効く と `効く` <b>効く</b>\n"
        )
        masked = ja_lint.preprocess_text(text)
        self.assertEqual(len(text), len(masked))
        self.assertEqual(text.count("\n"), masked.count("\n"))
        self.assertNotIn("title", masked)
        self.assertNotIn("https://", masked)
        self.assertNotIn("<b>", masked)
        self.assertEqual(4, masked.count("\n", 0, masked.index("本文")) + 1)

    def test_prose_masks_mdx_and_html_comments(self) -> None:
        text = (
            "本文は具体的な手順を説明します。\n"
            "{/* コメント内の表現は\nAではなくBです */}\n"
            "<!-- HTMLコメント内の表現も\nCではなくDです -->\n"
            "```mdx\n{/* フェンス内ではなくコメントです */}\n```\n"
        )
        masked = ja_lint.preprocess_text(text, "prose")
        self.assertEqual(len(text), len(masked))
        self.assertEqual(text.count("\n"), masked.count("\n"))
        self.assertNotIn("ではなく", masked)

    def test_frontmatter_checks_only_title_and_description_values(self) -> None:
        text = (
            "---\n"
            'title: "A ではなく B"\n'
            "description: 'C ではなく D'\n"
            "slug: E-ではなく-F\n"
            "tags:\n"
            "  - GではなくH\n"
            "---\n"
            "本文は具体的な手順を説明します。\n"
        )
        violations = ja_lint.lint_text(text, file="sample.mdx", mode="prose")
        dehanaku = [item for item in violations if item.rule_id == "dehanaku-001"]
        self.assertEqual([2, 3], [item.line for item in dehanaku])

    def test_kiku_does_not_match_effect_or_valid(self) -> None:
        rules = ja_lint.load_rules()
        # 仕様表は新規15本 + kanou-001置換1件。総数は29 + 15 = 44本。
        self.assertGreaterEqual(len(rules), 44)
        initial_ids = {
            "kiku-001", "dehanaku-001", "suiryo-001", "kanou-001", "juyo-001",
            "jitsugen-001", "okonau-001", "fukushi-001", "samazama-001",
            "kyocho-001", "katsuyo-001", "tsumari-001", "hiyu-001", "toiu-001",
            "setsuzoku-001",
        }
        self.assertLessEqual(initial_ids, {item.id for item in rules})
        new_ids = {
            "oite-001", "kanshite-001", "sasete-001", "itashimasu-001",
            "goriyou-001", "kotode-001", "jikkou-001", "omoware-001",
            "katachi-001", "teikyou-001", "anata-001", "suishou-001",
            "natteori-001", "juyo-002", "hitsuyou-001",
        }
        self.assertLessEqual(new_ids, {item.id for item in rules})
        rule = next(item for item in rules if item.id == "kiku-001")
        self.assertIsNone(rule.regex.search("この施策には効果があり、有効です。"))
        self.assertIsNotNone(rule.regex.search("この施策は現場に効きます。"))

    def test_target_path_rules_and_fixture_carveout(self) -> None:
        self.assertFalse(ja_lint.is_target_path(ROOT / "ja_lint.py"))
        excluded_config = dict(ja_lint.load_config())
        excluded_config["exclude_path_substrings"] = list(excluded_config["exclude_path_substrings"]) + ["/tests/"]
        self.assertFalse(ja_lint.is_target_path(ROOT / "tests" / "notes.md", config=excluded_config))
        self.assertTrue(ja_lint.is_target_path(FIXTURES / "ng_mixed.md"))
        self.assertFalse(ja_lint.is_target_path("README.rst", cwd=str(ROOT)))

    def test_extraction_modes_keep_only_visible_japanese(self) -> None:
        expected_counts = {
            "sample.mdx": 1,
            "sample.tsx": 2,
            "sample.json": 1,
            "sample.html": 1,
        }
        for name, expected in expected_counts.items():
            with self.subTest(name=name):
                violations = ja_lint.lint_file(FIXTURES / name)
                self.assertEqual(
                    expected,
                    sum(item.rule_id == "sasete-001" for item in violations),
                )

    def test_all_preprocessors_preserve_length_and_newlines(self) -> None:
        for name in ("sample.mdx", "sample.tsx", "sample.json", "sample.html"):
            with self.subTest(name=name):
                text = (FIXTURES / name).read_text(encoding="utf-8")
                mode = ja_lint.mode_for_path(name)
                masked = ja_lint.preprocess_text(text, mode or "prose")
                self.assertEqual(len(text), len(masked))
                self.assertEqual(
                    [index for index, value in enumerate(text) if value == "\n"],
                    [index for index, value in enumerate(masked) if value == "\n"],
                )

    def test_repo_exclude_glob_and_legacy_extension_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"exclude_globs": ["docs/**/*.md"]}),
                encoding="utf-8",
            )
            target = root / "docs" / "nested" / "target.md"
            target.parent.mkdir(parents=True)
            target.write_text("日本語の説明です。" * 4, encoding="utf-8")
            self.assertFalse(ja_lint.is_target_path(target))

            legacy_path = root / "legacy.json"
            legacy_path.write_text(
                json.dumps({"extensions": [".md", ".abc"]}), encoding="utf-8"
            )
            legacy = ja_lint.load_config(legacy_path)
            self.assertEqual({".md": "prose", ".abc": "prose"}, legacy["extensions"])
            self.assertEqual("prose", ja_lint.mode_for_path("x.abc", legacy))

    def test_structure_rules_are_prose_only(self) -> None:
        text = (
            'const a = "担当者は内容を確認します。";\n'
            'const b = "管理者は結果を確認します。";\n'
            'const c = "監査者は記録を確認します。";\n'
            'const d = "利用者は画面を確認します。";\n'
        )
        violations = ja_lint.lint_text(
            text,
            file="sample.ts",
            rules=[],
            enforce_min_japanese=False,
            mode="code",
        )
        self.assertFalse(
            {"style-ending-001", "style-mixed-001"}
            & {item.rule_id for item in violations}
        )

    def test_short_text_runs_regex_rules_but_not_structure_rules(self) -> None:
        critical = ja_lint.lint_text(
            'export const label = "ご案内させていただきます"',
            file="short.ts",
            mode="code",
        )
        self.assertIn("sasete-001", {item.rule_id for item in critical})

        short_repetition = "確認します。\n確認します。\n確認します。"
        self.assertLess(ja_lint.count_japanese(short_repetition), 30)
        structure = ja_lint.lint_text(
            short_repetition,
            file="short.md",
            rules=[],
            mode="prose",
        )
        self.assertFalse(
            {"style-ending-001", "style-mixed-001"}
            & {item.rule_id for item in structure}
        )

    def test_url_regex_literal_and_nested_template_extraction(self) -> None:
        url_text = "[こちら](https://example.com)ではなく、別案です。"
        url_violations = ja_lint.lint_text(url_text, file="sample.md", mode="prose")
        self.assertIn("dehanaku-001", {item.rule_id for item in url_violations})

        regex_then_string = (
            "const escaped = /[&<>\"']/g; "
            'const label = "ご案内させていただきます";'
        )
        regex_violations = ja_lint.lint_text(
            regex_then_string, file="sample.ts", mode="code"
        )
        self.assertIn("sasete-001", {item.rule_id for item in regex_violations})

        nested = "const label = `${cond ? `こちらではなく別です` : `別文です`}`;"
        nested_violations = ja_lint.lint_text(
            nested, file="sample.ts", mode="code"
        )
        self.assertIn("dehanaku-001", {item.rule_id for item in nested_violations})

    def test_template_expression_copies_normal_string_contents(self) -> None:
        text = (
            'const first = `${ok ? "これはAではなくBです" : "別です"}`;\n'
            "const second = `${ok ? 'これはCではなくDです' : '別です'}`;"
        )
        violations = ja_lint.lint_text(text, file="sample.ts", mode="code")
        dehanaku = [item for item in violations if item.rule_id == "dehanaku-001"]
        self.assertEqual([1, 2], [item.line for item in dehanaku])

    def test_code_import_export_mask_boundaries(self) -> None:
        text = (
            'import label from "ご案内させていただきます";\n'
            'export { label } from "ご案内させていただきます";\n'
            'export type { Label } from "ご案内させていただきます";\n'
            'export const label = "ご案内させていただきます from Copilot";\n'
            'import x from "x"; const tail = "ご案内させていただきます";\n'
        )
        violations = ja_lint.lint_text(text, file="sample.ts", mode="code")
        sasete = [item for item in violations if item.rule_id == "sasete-001"]
        self.assertEqual([4, 5], [item.line for item in sasete])

    def test_exclude_substrings_use_repo_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "memory" / "repo"
            repo.mkdir(parents=True)
            (repo / ".ja-lint.json").write_text(
                json.dumps({"exclude_path_substrings": ["/memory/"]}),
                encoding="utf-8",
            )
            config = {
                "extensions": {".md": "prose"},
                "exclude_path_substrings": ["/memory/"],
            }
            target = repo / "src" / "a.md"
            target.parent.mkdir()
            target.write_text("通常の説明です。\n", encoding="utf-8")
            excluded = repo / "memory" / "a.md"
            excluded.parent.mkdir()
            excluded.write_text("通常の説明です。\n", encoding="utf-8")

            self.assertTrue(ja_lint.is_target_path(target, config=config))
            self.assertFalse(ja_lint.is_target_path(excluded, config=config))

    def test_repo_extra_rules_are_added(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"extra_rules": "extra.jsonl"}), encoding="utf-8"
            )
            (root / "extra.jsonl").write_text(
                json.dumps(
                    {
                        "id": "repo-only-001",
                        "pattern": "固有の検査語",
                        "severity": "warn",
                        "label": "リポジトリ固有ルール",
                        "good": ["具体的に書く"],
                        "scenes": ["business"],
                        "enabled": True,
                        "added": "2026-09-02",
                        "min_count": 1,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            target = root / "target.md"
            target.write_text(
                "この文章には固有の検査語があります。利用者向けの説明と確認手順を詳しく記載します。",
                encoding="utf-8",
            )
            self.assertIn(
                "repo-only-001", {item.rule_id for item in ja_lint.lint_file(target)}
            )


class JaLintCliTests(unittest.TestCase):
    def run_cli(
        self, *args: str, cwd: Optional[str] = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "ja_lint.py"), *args],
            text=True,
            capture_output=True,
            cwd=cwd or str(ROOT),
            env=subprocess_env(),
            check=False,
        )

    def test_cli_exit_codes(self) -> None:
        clean = self.run_cli(str(FIXTURES / "clean.md"))
        critical = self.run_cli(str(FIXTURES / "ng_mixed.md"))
        self.assertEqual(0, clean.returncode, clean.stdout + clean.stderr)
        self.assertEqual(2, critical.returncode, critical.stdout + critical.stderr)
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as handle:
            handle.write(
                "担当者はしっかり確認し、結果を台帳に記録します。"
                "期限と確認項目は手順書に明記されています。"
            )
            handle.flush()
            warning = self.run_cli(handle.name)
        self.assertEqual(1, warning.returncode, warning.stdout + warning.stderr)

    def test_json_and_test_rule_modes(self) -> None:
        result = self.run_cli("--json", str(FIXTURES / "ng_mixed.md"))
        self.assertEqual(2, result.returncode)
        rows = json.loads(result.stdout)
        self.assertTrue(any(row["rule_id"] == "kiku-001" for row in rows))
        self.assertEqual(
            {
                "file", "line", "end_line", "rule_id", "severity", "label",
                "sentence", "good",
            },
            set(rows[0]),
        )

        match = self.run_cli(
            "--test-rule", "kiku-001", "--text", "この変更は受付業務に効きます。"
        )
        no_match = self.run_cli(
            "--test-rule", "kiku-001", "--text", "この変更には効果があり有効です。"
        )
        self.assertEqual((0, "MATCH"), (match.returncode, match.stdout.strip()))
        self.assertEqual((1, "NO MATCH"), (no_match.returncode, no_match.stdout.strip()))

        unknown = self.run_cli("--test-rule", "nonexistent-999", "--text", "x")
        self.assertEqual((3, "UNKNOWN RULE"), (unknown.returncode, unknown.stdout.strip()))

    def test_new_rule_match_and_no_match_table(self) -> None:
        cases = {
            "kanou-001": ("確認を行うことが可能です", "この設定は可能性が高い"),
            "oite-001": ("会議においては結果を確認します", "机の上に資料を置いてください"),
            "kanshite-001": ("設定に関して説明します", "設定に対して意見を述べます"),
            "sasete-001": ("結果を説明させていただきます", "結果を説明します"),
            "itashimasu-001": ("確認いたします。連絡いたします。送付いたします", "一度だけ確認いたします"),
            "goriyou-001": ("この機能をご利用いただけます", "詳しくはご利用ガイドを確認します"),
            "kotode-001": ("確認することで進み、保存することで残り、共有することで届きます", "確認すると進みます"),
            "jikkou-001": ("検証を実行します", "実行者を確認します"),
            "omoware-001": ("この結果は妥当と思われます", "根拠から妥当だと判断します"),
            "katachi-001": ("一覧という形で示します", "一覧で示します"),
            "teikyou-001": ("機能を提供します。情報を提供します", "提供者を確認します"),
            "anata-001": ("あなたの設定とあなたの履歴を確認します", "自分の設定を確認します"),
            "suishou-001": ("保存することを推奨します", "保存してください"),
            "natteori-001": ("この画面は確認用となっております", "この画面は確認用です"),
            "juyo-002": ("事前に確認することが重要です", "事前に確認してください"),
            "hitsuyou-001": ("設定する必要があります。保存する必要があります", "設定してください"),
        }
        for rule_id, (match_text, no_match_text) in cases.items():
            with self.subTest(rule_id=rule_id):
                match = self.run_cli("--test-rule", rule_id, "--text", match_text)
                no_match = self.run_cli(
                    "--test-rule", rule_id, "--text", no_match_text
                )
                self.assertEqual((0, "MATCH"), (match.returncode, match.stdout.strip()))
                self.assertEqual(
                    (1, "NO MATCH"), (no_match.returncode, no_match.stdout.strip())
                )

    def test_dash_subtitle_requires_meaningful_text_on_both_sides(self) -> None:
        cases = (
            ("見出し — 説明", 0, "MATCH"),
            ("| — |", 1, "NO MATCH"),
            ('値 ?? "—"', 1, "NO MATCH"),
            ("考える力——これが答えです", 0, "MATCH"),
        )
        for value, exit_code, output in cases:
            with self.subTest(value=value):
                result = self.run_cli(
                    "--test-rule", "dash-subtitle-001", "--text", value
                )
                self.assertEqual(
                    (exit_code, output),
                    (result.returncode, result.stdout.strip()),
                )

    def test_all_fail_on_report_and_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            docs = root / "docs"
            docs.mkdir()
            (root / ".ja-lint.json").write_text(
                json.dumps(
                    {
                        "include_globs": ["docs/**/*.md"],
                        "exclude_globs": ["docs/ignored*.md"],
                    }
                ),
                encoding="utf-8",
            )
            target = docs / "target.md"
            target.write_text(
                "利用者向けの詳しい結果を説明させていただきます。"
                "確認項目と手順は画面に表示されています。",
                encoding="utf-8",
            )
            (docs / "ignored.md").write_text(
                "利用者向けの詳しい結果を説明させていただきます。" * 2,
                encoding="utf-8",
            )
            report = root / "reports" / "lint.md"
            result = self.run_cli(
                "--all", "--report", str(report), cwd=str(root)
            )
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(target.resolve()), result.stdout)
            self.assertNotIn("ignored.md", result.stdout)
            report_text = report.read_text(encoding="utf-8")
            self.assertIn("総件数", report_text)
            self.assertIn("sasete-001", report_text)
            self.assertIn("docs/target.md", report_text)
            self.assertNotIn(str(target.resolve()), report_text)

            critical = self.run_cli(
                "--fail-on", "critical", str(target), cwd=str(root)
            )
            none = self.run_cli("--fail-on", "none", str(target), cwd=str(root))
            self.assertEqual(2, critical.returncode)
            self.assertEqual(0, none.returncode)

            annotations = self.run_cli(
                "--github-annotations", str(target), cwd=str(root)
            )
            self.assertIn(
                "::error file=docs/target.md,line=1::sasete-001 ",
                annotations.stdout,
            )

    def test_fail_on_warn_critical_and_none_for_warning_only(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as handle:
            handle.write(
                "担当者はしっかり確認し、結果を台帳に記録します。"
                "期限と確認項目は手順書に明記されています。"
            )
            handle.flush()
            results = {
                value: self.run_cli("--fail-on", value, handle.name)
                for value in ("warn", "critical", "none")
            }
        self.assertEqual(1, results["warn"].returncode)
        self.assertEqual(0, results["critical"].returncode)
        self.assertEqual(0, results["none"].returncode)

    def test_changed_collects_modified_target_and_non_repo_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            docs = root / "docs"
            docs.mkdir()
            target = docs / "changed.md"
            target.write_text(
                "利用者向けの確認手順を画面に表示します。内容を順番に確認してください。",
                encoding="utf-8",
            )
            initialize_git_repo(root)
            git_commit_all(root, "initial")
            target.write_text(
                "利用者向けの詳しい結果を説明させていただきます。"
                "確認項目と手順は画面に表示されています。",
                encoding="utf-8",
            )
            result = self.run_cli("--changed", "HEAD", cwd=str(root))
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(target.resolve()), result.stdout)

        with tempfile.TemporaryDirectory() as non_repo:
            error = self.run_cli("--changed", "HEAD", cwd=non_repo)
            self.assertEqual(4, error.returncode)
            self.assertIn("ja-lint:", error.stderr)

    def test_changed_reports_only_added_or_modified_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            target = root / "docs" / "changed.md"
            target.parent.mkdir()
            target.write_text(
                "既存の結果を説明させていただきます。\n", encoding="utf-8"
            )
            initialize_git_repo(root)
            git_commit_all(root, "initial violation")

            with target.open("a", encoding="utf-8") as handle:
                handle.write("これは問題のない追加行です。\n")
            clean = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(0, clean.returncode, clean.stdout + clean.stderr)
            self.assertIn("変更行のみ検査", clean.stdout)
            self.assertNotIn("sasete-001", clean.stdout)

            with target.open("a", encoding="utf-8") as handle:
                handle.write("追加結果をご案内させていただきます。\n")
            violation = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(
                2, violation.returncode, violation.stdout + violation.stderr
            )
            self.assertIn("sasete-001", violation.stdout)
            self.assertIn("L3", violation.stdout)

    def test_changed_keeps_multiline_match_when_only_second_line_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps(
                    {
                        "include_globs": ["docs/**/*.md"],
                        "extra_rules": "extra.jsonl",
                    }
                ),
                encoding="utf-8",
            )
            (root / "extra.jsonl").write_text(
                json.dumps(
                    {
                        "id": "multiline-001",
                        "pattern": "開始行です。\\n変更後です。",
                        "severity": "critical",
                        "label": "複数行の回帰テスト",
                        "good": ["行範囲で判定する"],
                        "scenes": ["business"],
                        "enabled": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            target = root / "docs" / "changed.md"
            target.parent.mkdir()
            target.write_text("開始行です。\n変更前です。\n", encoding="utf-8")
            initialize_git_repo(root)
            git_commit_all(root, "initial")
            target.write_text("開始行です。\n変更後です。\n", encoding="utf-8")

            result = self.run_cli(
                "--json", "--changed", "HEAD", "--fail-on", "critical",
                cwd=str(root),
            )
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            row = next(
                item for item in json.loads(result.stdout)
                if item["rule_id"] == "multiline-001"
            )
            self.assertEqual((1, 2), (row["line"], row["end_line"]))

    def test_changed_keeps_newly_completed_structure_violations(self) -> None:
        cases = (
            (
                "style-ending-001",
                "担当者は内容を確認します。\n管理者は結果を保存します。\n",
                "監査者は記録を確認します。\n",
                (1, 3),
            ),
            (
                "style-mixed-001",
                "担当者は内容を確認します。\n管理者は結果を保存します。\n"
                "監査者は記録を確認する。\n",
                "利用者は画面を確認する。\n",
                (1, 4),
            ),
        )
        for rule_id, initial, added, expected_range in cases:
            with self.subTest(rule_id=rule_id), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / ".ja-lint.json").write_text(
                    json.dumps({"include_globs": ["docs/**/*.md"]}),
                    encoding="utf-8",
                )
                target = root / "docs" / "changed.md"
                target.parent.mkdir()
                target.write_text(initial, encoding="utf-8")
                initialize_git_repo(root)
                git_commit_all(root, "initial")
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(added)

                result = self.run_cli("--json", "--changed", "HEAD", cwd=str(root))
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                row = next(
                    item
                    for item in json.loads(result.stdout)
                    if item["rule_id"] == rule_id
                )
                self.assertEqual(
                    expected_range,
                    (row["line"], row["end_line"]),
                )

    def test_changed_includes_committed_staged_and_renamed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            docs = root / "docs"
            docs.mkdir()
            committed = docs / "committed.md"
            committed.write_text("既存の結果を説明させていただきます。\n", encoding="utf-8")
            initialize_git_repo(root)
            git_commit_all(root, "initial")

            with committed.open("a", encoding="utf-8") as handle:
                handle.write("これは問題のない追加行です。\n")
            git_commit_all(root, "clean committed line")
            clean_commit = self.run_cli(
                "--changed", "HEAD~1", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(
                0, clean_commit.returncode, clean_commit.stdout + clean_commit.stderr
            )
            with committed.open("a", encoding="utf-8") as handle:
                handle.write("結果をご案内させていただきます。\n")
            git_commit_all(root, "violating committed line")
            bad_commit = self.run_cli(
                "--changed", "HEAD~1", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(2, bad_commit.returncode)

            staged = docs / "staged.md"
            staged.write_text("通常の説明です。\n", encoding="utf-8")
            git_commit_all(root, "staged fixture")
            with staged.open("a", encoding="utf-8") as handle:
                handle.write("結果をご案内させていただきます。\n")
            subprocess.run(["git", "add", str(staged)], cwd=root, check=True)
            unstaged = subprocess.run(
                ["git", "diff", "--name-only"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual("", unstaged.stdout)
            staged_result = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(2, staged_result.returncode)
            subprocess.run(["git", "reset", "--quiet", "HEAD"], cwd=root, check=True)
            staged.write_text("通常の説明です。\n", encoding="utf-8")

            old_path = docs / "old-name.md"
            old_path.write_text("通常の説明です。\n" * 10, encoding="utf-8")
            git_commit_all(root, "rename fixture")
            new_path = docs / "new-name.md"
            subprocess.run(
                ["git", "mv", str(old_path), str(new_path)], cwd=root, check=True
            )
            with new_path.open("a", encoding="utf-8") as handle:
                handle.write("結果をご案内させていただきます。\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            rename_status = subprocess.run(
                ["git", "diff", "--cached", "--name-status", "-M"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(rename_status.stdout.startswith("R"), rename_status.stdout)
            renamed = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(2, renamed.returncode, renamed.stdout + renamed.stderr)
            self.assertIn(str(new_path.resolve()), renamed.stdout)

    def test_changed_pure_rename_marks_all_new_path_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            old_path = root / "docs" / "old.md"
            old_path.parent.mkdir()
            old_path.write_text(
                "既存の結果をご案内させていただきます。\n", encoding="utf-8"
            )
            initialize_git_repo(root)
            git_commit_all(root, "initial")
            new_path = root / "docs" / "new.md"
            subprocess.run(
                ["git", "mv", str(old_path), str(new_path)], cwd=root, check=True
            )
            rename_status = subprocess.run(
                ["git", "diff", "--cached", "--name-status", "-M"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(rename_status.stdout.startswith("R100"))

            result = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(new_path.resolve()), result.stdout)
            self.assertIn("sasete-001", result.stdout)

    def test_changed_honors_include_globs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            outside = root / "notes" / "outside.md"
            outside.parent.mkdir()
            outside.write_text("通常の説明です。\n", encoding="utf-8")
            initialize_git_repo(root)
            git_commit_all(root, "initial")
            with outside.open("a", encoding="utf-8") as handle:
                handle.write("結果をご案内させていただきます。\n")
            result = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("outside.md", result.stdout)

    def test_changed_includes_untracked_files_and_honors_include_globs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".ja-lint.json").write_text(
                json.dumps({"include_globs": ["docs/**/*.md"]}), encoding="utf-8"
            )
            initialize_git_repo(root)
            git_commit_all(root, "initial")
            target = root / "docs" / "untracked.md"
            target.parent.mkdir()
            target.write_text(
                "結果をご案内させていただきます。\n", encoding="utf-8"
            )
            outside = root / "notes" / "untracked.md"
            outside.parent.mkdir()
            outside.write_text(
                "結果をご案内させていただきます。\n", encoding="utf-8"
            )

            result = self.run_cli(
                "--changed", "HEAD", "--fail-on", "critical", cwd=str(root)
            )
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(target.resolve()), result.stdout)
            self.assertNotIn(str(outside.resolve()), result.stdout)

    def test_changed_invalid_include_globs_is_config_error(self) -> None:
        for include_globs in ("docs/**/*.md", ["docs/**/*.md", 7]):
            with self.subTest(include_globs=include_globs), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / ".ja-lint.json").write_text(
                    json.dumps({"include_globs": include_globs}), encoding="utf-8"
                )
                target = root / "docs" / "changed.md"
                target.parent.mkdir()
                target.write_text("通常の説明です。\n", encoding="utf-8")
                initialize_git_repo(root)
                git_commit_all(root, "initial")
                with target.open("a", encoding="utf-8") as handle:
                    handle.write("追加の説明です。\n")

                result = self.run_cli("--changed", "HEAD", cwd=str(root))
                self.assertEqual(4, result.returncode, result.stdout + result.stderr)
                self.assertIn("include_globs が不正です", result.stderr)

    def test_file_read_error_is_fail_closed_after_other_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.md"
            target.write_text(
                "結果をご案内させていただきます。\n", encoding="utf-8"
            )
            missing = root / "missing.md"

            result = self.run_cli(str(target), str(missing), cwd=str(root))
            self.assertEqual(4, result.returncode, result.stdout + result.stderr)
            self.assertIn("sasete-001", result.stdout)
            self.assertIn(str(target.resolve()), result.stdout)
            self.assertIn(str(missing.resolve()), result.stderr)


class HookTests(unittest.TestCase):
    def run_hook(
        self, script: str, payload: str, env: Optional[Dict[str, str]] = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / script)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=str(ROOT),
            env=env or subprocess_env(),
            check=False,
        )

    def test_post_hook_blocks_and_off_is_silent(self) -> None:
        session_id = "ja_lint_test_post"
        state_path = STATE_DIR / f"{session_id}.txt"
        try:
            state_path.unlink(missing_ok=True)
            payload = json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(FIXTURES / "ng_mixed.md")},
                    "session_id": session_id,
                    "cwd": "/tmp",
                }
            )
            result = self.run_hook("post_hook.py", payload)
            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("block", response["decision"])
            self.assertIn("丸ごと書き直すこと", response["reason"])
            repeated = self.run_hook("post_hook.py", payload)
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(
                [str((FIXTURES / "ng_mixed.md").resolve())],
                state_path.read_text(encoding="utf-8").splitlines(),
            )

            off = self.run_hook("post_hook.py", payload, subprocess_env(JA_LINT="off"))
            self.assertEqual(0, off.returncode)
            self.assertEqual("", off.stdout)
        finally:
            state_path.unlink(missing_ok=True)

    def test_stop_hook_guard_and_critical_block(self) -> None:
        session_id = "ja_lint_test_stop"
        state_path = STATE_DIR / f"{session_id}.txt"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            state_path.write_text(str(FIXTURES / "ng_mixed.md") + "\n", encoding="utf-8")
            guarded = self.run_hook(
                "stop_hook.py",
                json.dumps({"session_id": session_id, "stop_hook_active": True}),
            )
            self.assertEqual(0, guarded.returncode)
            self.assertEqual("", guarded.stdout)
            planned = self.run_hook(
                "stop_hook.py",
                json.dumps(
                    {
                        "session_id": session_id,
                        "stop_hook_active": False,
                        "permission_mode": "plan",
                    }
                ),
            )
            self.assertEqual(0, planned.returncode)
            self.assertEqual("", planned.stdout)

            result = self.run_hook(
                "stop_hook.py",
                json.dumps({"session_id": session_id, "stop_hook_active": False}),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual("block", response["decision"])
            self.assertTrue(
                response["reason"].startswith(
                    "ja-lint: セッション終了前に以下の critical 違反を書き直すこと"
                )
            )
            self.assertNotIn("style-ending-001", response["reason"])
        finally:
            state_path.unlink(missing_ok=True)

    def test_hook_errors_are_silent_and_successful(self) -> None:
        log_path = STATE_DIR / "ja_lint.log"
        before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        for script in ("post_hook.py", "stop_hook.py"):
            with self.subTest(script=script):
                result = self.run_hook(script, "not-json")
                self.assertEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
        appended = log_path.read_text(encoding="utf-8")[len(before):]
        self.assertIn("post_hook JSONDecodeError", appended)
        self.assertIn("stop_hook JSONDecodeError", appended)



class DashAdjacentToMaskedRegionTest(unittest.TestCase):
    """タグやインラインコードはマスクで空白になるため、片側だけ内容があればダッシュ連結として扱う。"""

    PAD = "この文は検査を有効にするための三十文字以上の日本語の本文です。\n"

    def _ids(self, text, file="/x/a.mdx"):
        return [item.rule_id for item in ja_lint.lint_text(text, file=file)]

    def test_strong_tag_before_dash_is_reported(self):
        text = self.PAD + "<Step><strong>Intune</strong> — 管理者がポリシーを構成していること。</Step>\n"
        self.assertIn("dash-subtitle-001", self._ids(text))

    def test_inline_code_before_dash_is_reported(self):
        text = self.PAD + "- `pwd` — 現在のディレクトリを確認\n"
        self.assertIn("dash-subtitle-001", self._ids(text))

    def test_table_blank_cell_is_ignored(self):
        text = self.PAD + "| 項目 | 値 |\n|---|---|\n| 単価 | — |\n"
        self.assertNotIn("dash-subtitle-001", self._ids(text))


class JsonUnicodeEscapeTest(unittest.TestCase):
    def test_escaped_japanese_in_json_is_critical(self):
        text = '{"a": "いま直すと一番\\u52b9くクセは何か", "b": "三十文字以上の説明文をここに置いておきます。"}'
        ids = [item.rule_id for item in ja_lint.lint_text(text, file="/x/a.json")]
        self.assertIn("json-escape-001", ids)

    def test_ascii_escape_is_ignored(self):
        text = '{"a": "tab\\u0009here", "b": "三十文字以上の説明文をここに置いておきます。"}'
        ids = [item.rule_id for item in ja_lint.lint_text(text, file="/x/a.json")]
        self.assertNotIn("json-escape-001", ids)

    def test_escaped_japanese_in_tsx_is_critical_with_common_label(self):
        text = '<p>{"\\u3054\\u6848\\u5185"}</p>'
        violations = ja_lint.lint_text(text, file="/x/a.tsx", mode="code")
        escaped = [
            item for item in violations if item.rule_id == "json-escape-001"
        ]
        self.assertTrue(escaped)
        self.assertTrue(all(item.severity == "critical" for item in escaped))
        self.assertEqual(
            {"日本語を \\u エスケープで書いている"},
            {item.label for item in escaped},
        )


class JsxClosingTagNotRegexTest(unittest.TestCase):
    PAD = 'const pad = "三十文字以上にするための説明文をここに置いておきます。";\n'

    def _ids(self, text):
        return [item.rule_id for item in ja_lint.lint_text(text, file="/x/a.tsx")]

    def test_dash_after_closing_tag_is_reported(self):
        self.assertIn("dash-subtitle-001", self._ids(self.PAD + "<p><strong>重要</strong> — 説明です</p>\n"))

    def test_dehanaku_after_closing_tag_is_reported(self):
        self.assertIn("dehanaku-001", self._ids(self.PAD + "<p><strong>重要</strong>ではなく直接書きます</p>\n"))

    def test_real_regex_literal_still_masked(self):
        text = self.PAD + 'const r = /[&<>"\']/g;\nconst m = "ご案内させていただきます";\n'
        self.assertIn("sasete-001", self._ids(text))


class EscapeAndUrlInCodeTest(unittest.TestCase):
    PAD = 'const pad = "三十文字以上にするための説明文をここに置いておきます。";\n'

    def _ids(self, text, file="/x/a.tsx"):
        return [item.rule_id for item in ja_lint.lint_text(text, file=file)]

    def test_regex_char_range_is_not_escape_violation(self):
        self.assertNotIn("json-escape-001", self._ids(self.PAD + "const r = /[\\u3040-\\u309f]/;\n", "/x/a.ts"))

    def test_comment_escape_is_not_violation(self):
        self.assertNotIn("json-escape-001", self._ids(self.PAD + "// example escape: \\u65e5\n", "/x/a.ts"))

    def test_string_escape_is_still_violation(self):
        self.assertIn("json-escape-001", self._ids(self.PAD + 'const l = "\\u3054\\u6848\\u5185";\n', "/x/a.ts"))

    def test_url_in_jsx_text_is_not_comment(self):
        self.assertIn("dehanaku-001", self._ids(self.PAD + "const x = <p>詳しくは https://example.com。AではなくBです。</p>;\n"))


class MarkdownLinkTargetMaskTest(unittest.TestCase):
    PAD = "この文は検査を有効にするための三十文字以上の日本語の本文です。\n"

    def _ids(self, text):
        return [item.rule_id for item in ja_lint.lint_text(text, file="/x/a.md")]

    def test_anchor_target_is_not_linted(self):
        self.assertNotIn("dehanaku-001", self._ids(self.PAD + "2. [配るのは同梱物一式](#2-配るのはツールではなく同梱物一式)\n"))

    def test_link_text_is_still_linted(self):
        self.assertIn("dehanaku-001", self._ids(self.PAD + "2. [配るのはツールではなく同梱物一式](#2-anchor)\n"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
