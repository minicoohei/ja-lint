# ja-lint

AI が書く日本語の「型」を機械検出し、書き込み直後に書き直しを差し戻す検査器です。依存ゼロの純 Python 1 ファイルで、Claude Code の Hook・CLI・GitHub Actions のどこからでも同じルールで動きます。

対象は翻訳調・AI 文体・硬すぎる言い回しです。例: `〜することができます` `〜させていただき` `〜において` `A — B` のダッシュ連結、`AではなくB` の連発、`重要なのは` の前置き、`効く` `様々な` の空語。ルールは JSONL に 1 行 1 本で、正規表現と言い換え候補を持ちます（現在 50 本 + 文末反復・敬体常体混在の構造ルール 2 本）。

実績: ai-agent.camp（Next.js、ブログ 187 本・i18n 辞書・LP）と Copilot Camp（MDX 教材 180 本）に導入し、critical 2,355 件を本文からゼロにしたうえで、PR の変更行だけを CI で検査して再発を止めています。

## 何が違うか

- **ユーザーに見える文言だけを検査する。** `.md/.mdx` は本文、`.ts/.tsx` は文字列リテラルと JSX テキスト、`.json` は値、`.html` はテキストノードだけを残し、コメント・import・キー名・タグをマスクしてから照合します
- **書き直しを要求する。** 検出した文には「NG ワードの置換禁止、文ごと書き直す」という指示と言い換え候補が付きます。語の置換だけだと同じ問題が別の語形で残るためです
- **CI は変更行だけ。** `--changed <base>` は PR で追加・変更された行の違反だけを報告するので、既存の負債が残っていても無関係な PR は落ちません
- **回避を塞ぐ。** 日本語を `\uXXXX` でエスケープして検査をすり抜ける、といった手口自体を違反にしています（実際に起きました）

## セットアップ

### 1. CLI として使う

```bash
git clone https://github.com/minicoohei/ja-lint ~/.claude/hooks/ja_lint
python3 ~/.claude/hooks/ja_lint/ja_lint.py path/to/file.md          # 人間可読
python3 ~/.claude/hooks/ja_lint/ja_lint.py --json path/to/file.mdx   # 機械可読
python3 ~/.claude/hooks/ja_lint/tests/run_tests.py                   # 自己テスト
```

exit code: 0 = 違反なし / 1 = warn のみ / 2 = critical あり / 4 = 設定・git エラー。

### 2. Claude Code の Hook にする

`examples/settings.json` の `hooks` を `~/.claude/settings.json` に足します。PostToolUse（Edit / Write / MultiEdit / Bash）で書き込み直後に検査して差し戻し、Stop で critical が残っていればセッション終了をブロックします。無効化は環境変数 `JA_LINT=off`。

### 3. リポジトリの CI にする

```bash
mkdir -p tools/ja_lint
cp ~/.claude/hooks/ja_lint/{ja_lint.py,rules.jsonl,config.json} tools/ja_lint/
cp examples/.ja-lint.json .ja-lint.json          # 検査対象の include / exclude glob
cp examples/ja-lint.yml .github/workflows/       # PR の変更行だけ検査、critical で fail
```

`package.json` に足すなら:

```json
"check:ja": "python3 tools/ja_lint/ja_lint.py --changed origin/main --fail-on critical",
"check:ja:all": "python3 tools/ja_lint/ja_lint.py --all --fail-on none --report docs/reports/ja-lint-latest.md"
```

## CLI オプション

| オプション | 用途 |
|---|---|
| `<file...>` | 指定ファイルを検査 |
| `--changed <base-ref>` | `<base-ref>...HEAD` + ステージ済み + 作業ツリー + 未追跡の変更行だけを検査 |
| `--all` | `.ja-lint.json` の `include_globs` を全件検査 |
| `--fail-on critical\|warn\|none` | exit code を決める閾値（既定 warn） |
| `--report <path.md>` | ルール別・ファイル別の集計レポートを書き出す |
| `--github-annotations` | `::error file=…,line=…::` 形式で出力 |
| `--json` | 違反を JSON 配列で出力 |
| `--test-rule <id> --text "<文>"` | ルール 1 本の MATCH / NO MATCH を実測 |

## リポジトリ側設定 `.ja-lint.json`

対象ファイルから上方向に探して最初に見つかったものを使います。

```json
{
  "include_globs": ["content/**/*.md", "src/**/*.tsx"],
  "exclude_globs": ["docs/**", "**/*.test.ts"],
  "exclude_path_substrings": ["/generated/"],
  "extra_rules": "tools/ja_lint/rules.local.jsonl"
}
```

## ルールを育てる

`rules.jsonl` に 1 行足して `--test-rule` で NG 文が MATCH、正常文が NO MATCH になることを実測してから使います。

```json
{"id": "sasete-001", "pattern": "させていただ(?:き|く|け)", "severity": "critical", "label": "過剰な謙譲「させていただく」", "good": ["「〜します」と言い切る"], "scenes": ["business"], "enabled": true, "added": "2026-09-02", "min_count": 1}
```

- `severity`: 文意が壊れる・強く避けたい表現は `critical`、乱用・単調は `warn`
- `min_count`: ファイル内でこの回数以上出たときだけ報告（「〜することで」のように 1 回なら自然な表現向け）
- Claude Code でルールを育てる手順は `skill/SKILL.md`（指摘 → 正規表現化 → 実測テストの一気通貫）

## 設計上の割り切り

- 正規表現の「型」検出です。一文の長さ、語順、段落構造の均質さは見ません（文体そのものの改善は別の道具の仕事です）
- 引用文・固有名詞・法律名・お客様の声は検査で赤くなりますが、書き換えてはいけません。運用では「原文維持の一覧を報告する」を完了条件にしています
- TS/TSX の走査は簡易トークナイザです。`if (x) /re/.test(y)` のような文頭の正規表現リテラルは取りこぼすことがあります

## 謝辞

- 禁止語カタログの一部は [coji/natural-japanese](https://github.com/coji/natural-japanese)（MIT）を参考にしました
- 「〜を実行する」の冗長表現は [textlint-ja/textlint-rule-ja-no-redundant-expression](https://github.com/textlint-ja/textlint-rule-ja-no-redundant-expression)（MIT）の辞書を参考にしました

## License

MIT
