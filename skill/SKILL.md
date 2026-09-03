---
name: ja-lint
description: AIが書く日本語ドキュメントのNGワード検査（Hooks連動）のルール管理。ユーザーが日本語表現を指摘したとき・「ルールに追加」「ja-lint」と言われたとき・手動フル検査を頼まれたとき・会話履歴からルール候補を抽出したいときに使う。指摘→正規表現ルール化→実測テストまでを一気通貫で行う。
---

# ja-lint — 日本語NGワード検査のルール管理

検査本体は `~/.claude/hooks/ja_lint/` にある（PostToolUse で書き込み直後に検査、Stop で critical 残存をブロック）。このスキルはそのルールを育てる・手動で回すための手順。

- ルール正本: `~/.claude/hooks/ja_lint/rules.jsonl`（1行1ルール）
- 設定: `~/.claude/hooks/ja_lint/config.json`（対象拡張子・除外パス・既定プロファイル）
- 全体無効化: 環境変数 `JA_LINT=off` ／ プロファイル切替: `JA_LINT_PROFILE`（既定 `business`）
- 抽出モード: `.md/.txt/.mdx`=`prose`、`.ts/.tsx/.js/.jsx`=`code`、`.json`=`json`、`.html`=`html`。コードや構造をマスクし、frontmatter の `title` / `description` を含むユーザー可視文言だけを検査する

## 1. 指摘 → ルール登録（主用途）

ユーザーが「この表現やめて」「〜が変」と日本語表現を指摘したら、その場でルール化する。

1. 指摘から抽出する: NG表現の正規表現 `pattern` / 重さ `severity`（同じ問題が残ると文意が壊れる・ユーザーが強く嫌う表現 = `critical`、単なる乱用・単調 = `warn`）/ 書き直しの方向 `good`（2〜3個。ユーザーの言い換え例があれば最優先で採用）
2. 重複確認: `grep <代表語> ~/.claude/hooks/ja_lint/rules.jsonl` — 既存ルールがあれば pattern/good を更新し、新規は作らない
3. 追記する1行の形式:
   ```json
   {"id": "<slug>-001", "pattern": "<正規表現>", "severity": "warn", "label": "<何が悪いか一言>", "good": ["<書き直し例1>", "<例2>"], "scenes": ["business"], "enabled": true, "added": "<今日>", "min_count": 1}
   ```
   - `min_count` は「乱用」系（N回以上出たら指摘）のときだけ 2 以上にする
   - pattern は部分一致で広く当たりすぎないか必ず考える（例: 「効く」を `効` にすると「効果」に誤マッチ）
4. 実測してから完了報告する（登録しただけで報告しない）:
   ```bash
   python3 ~/.claude/hooks/ja_lint/ja_lint.py --test-rule <id> --text "<指摘されたNG文>"   # → MATCH
   python3 ~/.claude/hooks/ja_lint/ja_lint.py --test-rule <id> --text "<誤検知しそうな正常文>" # → NO MATCH
   ```
5. 報告には id・pattern・good と MATCH/NO MATCH の実測結果を含める

## 2. 手動フル検査

```bash
python3 ~/.claude/hooks/ja_lint/ja_lint.py <対象ファイル...>        # 人間可読
python3 ~/.claude/hooks/ja_lint/ja_lint.py --json <対象ファイル...> # 機械可読
python3 ~/.claude/hooks/ja_lint/ja_lint.py --changed <base-ref>    # 変更行だけ（commit・stage・worktree の和集合）
python3 ~/.claude/hooks/ja_lint/ja_lint.py --all                   # リポ設定の全対象
```
`--changed` はファイル全体でなく追加・変更行の違反だけを報告する。`--fail-on critical|warn|none` で CI の失敗条件を選ぶ（既定 `warn`）。`--report <path.md>` は集計レポート、`--github-annotations` は GitHub Actions 注釈を出す。exit code: 0=違反なし / 1=warn のみ / 2=critical あり / 4=CLI・git・設定エラー。30文字の日本語文字数閾値は文末連続・文体混在の構造ルールだけに適用し、JSONL の正規表現ルールは短文でも検査する。検出された文は語の置換でなく**文ごと書き直す**（フックの警告と同じ契約）。

リポジトリ固有設定は対象ファイルから上方向に最初に見つかる `.ja-lint.json` を使う。

- `include_globs`: `--all` と `--changed` の対象（リポルート相対、`**` 対応）
- `exclude_globs`: Hook/CLI 共通の除外
- `exclude_path_substrings`: グローバル除外への追加
- `extra_rules`: リポルート相対の追加 JSONL

`--test-rule` は MATCH=0 / NO MATCH=1。存在しない ID は `UNKNOWN RULE` を出して exit 3。

## 3. 会話履歴からルール候補を抽出（週次・手動起動）

「履歴からルール候補を出して」と言われたら:

1. `~/.claude/projects/*/` 直下のセッション JSONL から直近1〜2週間分を対象に、ユーザー発話のうち日本語表現への修正指摘（「〜やめて」「〜が変」「〜に直して」等）を抽出する。JSONL は巨大なので Python でユーザーロールのみ絞ってから読む（全文をコンテキストに展開しない）
2. 繰り返し出る指摘を頻度順に集計し、ルール候補（pattern / severity / good 案）として最大10件提案する
3. **登録はユーザー承認後**。承認された分だけ手順1の 3〜5 で登録・実測する

## してはいけないこと

- rules.jsonl に不正な JSON 行・未テストの正規表現を残す（不正行は黙ってスキップされ、気づかず死ぬ）
- 検査本体（ja_lint.py / post_hook.py / stop_hook.py）をこのスキルの作業でついでに改修する（本体変更は別タスクとして切る）
- CLAUDE.md や MEMORY.md にNGワード実例を書き足す（除外対象だが、他ツールが読む文書に例文を撒かない）
