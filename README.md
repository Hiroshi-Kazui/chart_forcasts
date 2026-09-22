# chart_forecasts 開発ハーネス

実装・テスト・レビュー・修正を独立したCodexサブエージェントで進め、**最後にClaudeが検収してから**
元リポジトリへ反映するローカル開発ハーネスです。発注元と発注先を別のフォルダに分けています。

```
.codex/codex_harness/     発注先へ渡すもの: 工程ごとのモデル指定、Codex起動引数、工程指示、結果スキーマ
.claude/claude_harness/   発注元が持つもの: 制御プロセス、作業コピー、実行台帳、固定検証、最終レビュー検収
.claude/commands/         Claude Codeから使う手順（最終レビュー）
```

制御プログラムは工程の起動、証跡の検査、状態遷移、検収通過後の反映だけを担当します。

## 使い方

```powershell
python -m claude_harness run --task tasks/reference-package.json
python -m claude_harness status --run <実行ID> --json
python -m claude_harness wait --run <実行ID> --json
python -m claude_harness final-review --run <実行ID> --file <判定JSONのパス>
python -m claude_harness stop --run <実行ID>
python -m claude_harness resume --run <実行ID>
```

開発中に直接実行する場合は `.claude` と `.codex` をモジュール検索パスへ入れてください
（`pytest` は `pyproject.toml` の設定で自動的に通ります）。

`run` は実行IDを返し、端末から独立した制御プロセスを起動します。実行・修正には `gpt-5.6-sol` の
medium、テストには `gpt-5.6-sol` のmedium、レビューには `gpt-6-astra` のhighを明示します。
指定モデルが利用できなければ工程は失敗し、別モデルへ変更しません。

Windowsでは制御プロセスとハーネスが直接起動する全子プロセスを非表示で実行します。担当への指示でも、
新しいコンソールやGUIを開かず、補助プロセスを非表示で起動することを必須にしています。

## 工程と検収

```
実装 → テスト → レビュー(gpt-6-astra) ─合格─→ 最終レビュー待ち（制御プロセスは終了）
                                                  │  Claudeが証跡と差分を確認して判定を提出
                                  合格 ──────────→ 反映 → COMPLETED
                                  不合格 ────────→ 修正 → テスト → レビュー → 最終レビュー待ち
```

テスト担当が一度限りの検証サービスを呼び、サービスが固定コマンドのargv、終了コード、標準出力、
標準エラー、時間切れ、対象コードのハッシュを記録します。制御側は担当の自己申告ではなく、この記録を
照合します。最終レビューでも同じ考え方を適用し、「Claudeが問題なしと言った」だけでは完了しません。

## 最終レビュー

astraレビューに合格すると、状態は `AWAITING_FINAL_REVIEW`、工程は「最終レビュー」になり、
制御プロセスは終了します。このとき `.harness/runs/<実行ID>/final-review-request.json` に、
変更ファイル一覧、各工程の証跡、テスト時のコードハッシュ、原本と作業コピーの位置が保存されます。

判定は次の形式です（スキーマ: `.claude/claude_harness/schemas/final-review.json`）。

```json
{
  "status": "PASS",
  "summary": "受入条件と証跡を照合した結果",
  "findings": [
    {"id": "F1", "severity": "P1", "description": "指摘内容",
     "evidence": ".harness/runs/<実行ID>/phases/02-テスト/verification.json"}
  ],
  "reviewer": {"model": "claude-fable-5-1", "session": "<任意>"}
}
```

制御側は提出時に、スキーマ適合、担当モデルが `claude-fable-5-1` であること、指摘の証跡が実行証跡
ディレクトリ内にあること、作業コピーがテスト時から変わっていないことを検査します。どれかが崩れて
いれば判定を受け付けず、状態は最終レビュー待ちのままです。反映の直前にも同じ検査をやり直します。

最終レビューを待っている時間は、タスクの全体時間上限から除きます（台帳の `excluded_seconds`）。
待機中の実行は `resume` できません。判定の提出だけが次へ進める手段です。

## タスク定義

必須項目は `name`、`requirements`、`edit_scope`、`acceptance_criteria`、`verification_commands` です。
検証コマンドはシェル文字列ではなくargv配列で指定します。未知の項目、絶対パス、`..` を含むパスは
拒否します。`max_fixes` は0から2です。

`tasks/reference-package.json` は、ロードマップの「第1段階：参照測定パッケージ」をハーネスへ渡すための
定義です。このファイルを用意しただけでは参照測定を実装・実行しません。

## 証跡と保存先

実行証跡は `.harness/runs/<実行ID>/`、状態台帳は `.harness/state.sqlite3` に保存されます。工程ごとの
ディレクトリには、指示、Codex起動引数、JSONLイベント、標準エラー、構造化結果、プロセス結果を保存します。

元リポジトリは直接編集させません。開始時に秘密情報・データ・実行結果を除いた作業コピーを作成し、その中で
各工程を進めます。テスト、レビュー、最終レビューが同じコードに合格した後、元ファイルが開始時から変わって
いないことと編集範囲内の変更だけであることを確認して反映します。commitとpushは行いません。

## テスト

テスト用には `DEV_HARNESS_CODEX` でCodex実行ファイルを差し替えられます。差替え先は通常の `codex exec`
引数を受け、`--output-last-message` で指定されたファイルへ
`.codex/codex_harness/schemas/phase-result.json` に適合するJSONを書きます。
`DEV_HARNESS_PROMPT_FILE` には工程指示ファイルのパスが入ります。

```powershell
python -m pytest -q
python -m ruff check .claude/claude_harness .codex/codex_harness tests
```
