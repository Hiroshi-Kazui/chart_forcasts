# chart_forecasts 開発ハーネス

このリポジトリには、実装・テスト・レビュー・修正を独立したCodexサブエージェントで進めるローカル開発ハーネスが含まれます。制御プログラムは工程の起動、証跡の検査、状態遷移、合格後の反映だけを担当します。

## 使い方

```powershell
python -m dev_harness run --task tasks/reference-package.json
python -m dev_harness status --run <実行ID> --json
python -m dev_harness stop --run <実行ID>
python -m dev_harness resume --run <実行ID>
```

`run` は実行IDを返し、端末から独立した制御プロセスを起動します。実行・修正には `gpt-5.6-sol` のmedium、テストには `gpt-5.6-sol` のmedium、レビューには `gpt-6-astra` のhighを明示します。指定モデルが利用できなければ工程は失敗し、別モデルへ変更しません。

Windowsでは制御プロセスとハーネスが直接起動する全子プロセスを非表示で実行します。担当への指示でも、新しいコンソールやGUIを開かず、補助プロセスを非表示で起動することを必須にしています。

タスク定義の必須項目は `name`、`requirements`、`edit_scope`、`acceptance_criteria`、`verification_commands` です。検証コマンドはシェル文字列ではなくargv配列で指定します。未知の項目、絶対パス、`..` を含むパスは拒否します。`max_fixes` は0から2です。

実行証跡は `.harness/runs/<実行ID>/`、状態台帳は `.harness/state.sqlite3` に保存されます。工程ごとのディレクトリには、指示、Codex起動引数、JSONLイベント、標準エラー、構造化結果、プロセス結果を保存します。テスト担当が一度限りの検証サービスを呼び、サービスが固定コマンドのargv、終了コード、標準出力、標準エラー、時間切れ、対象コードのハッシュを記録します。制御側は担当の自己申告ではなく、この記録を照合します。

元リポジトリは直接編集させません。開始時に秘密情報・データ・実行結果を除いた作業コピーを作成し、その中で各工程を進めます。テストとレビューが同じコードに合格した後、元ファイルが開始時から変わっていないことと編集範囲内の変更だけであることを確認して反映します。commitとpushは行いません。

テスト用には `DEV_HARNESS_CODEX` でCodex実行ファイルを差し替えられます。差替え先は通常の `codex exec` 引数を受け、`--output-last-message` で指定されたファイルへ `dev_harness/schemas/phase-result.json` に適合するJSONを書きます。`DEV_HARNESS_PROMPT_FILE` には工程指示ファイルのパスが入ります。

## タスク定義の例

`tasks/reference-package.json` は、ロードマップの「第1段階：参照測定パッケージ」をハーネスへ渡すための定義です。このファイルを用意しただけでは参照測定を実装・実行しません。
