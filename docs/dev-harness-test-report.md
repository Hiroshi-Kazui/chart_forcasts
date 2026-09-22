# 開発ハーネス独立試験報告

実施日: 2026-09-21  
対象: `dev_harness`  
最終製品ディレクトリ hash: `988114411417b774b2cd21742b2da2e6bbbb005416a9681f5bfbd4ba99a25757`

## 結論

疑似エージェントによる単体・CLI統合試験、Windows非表示起動試験、実際の指定モデルによる失敗から修正・再テスト・レビューまでの一巡に合格した。

- 単体試験: 30 passed、1 skipped
- CLI統合試験: 16 passed
- 静的検査: `python -m ruff check dev_harness tests` 合格
- skip: Windows環境でsymlink作成権限がない場合の動的試験1件。scope/path拒否は動的試験に合格した。junction拒否は実装の静的確認に留まり、junctionを作成する動的試験は実施していない
- Windows非表示起動: 子プロセスの `GetConsoleWindow()` は0、起動前後のforeground window handleは不変。可視ウィンドウや前面奪取は発生しなかった

主な回帰範囲は、正常系、テスト失敗からFIX、レビューP1からFIX、不正schema、未実行・偽造・過去版の検証証跡、scope外変更、原子的反映、競合拒否、固定名staging衝突、timeoutと子孫終了、Job割当失敗、stop/resume、複数回resumeの予算累積、controller異常死、FIX上限、欠落requirement file、要件原文固定、反映直前STOP、二重起動、モデル固定である。

## 実モデル最終検収

成功run: `20260921-224957-e7b9a03f`  
証跡root: `C:\develop\chart_forecasts_harness_fix_trial\.harness\runs\20260921-224957-e7b9a03f`

工程と結果:

1. `01-実装`: `gpt-5.6-sol` / medium。検収用の初回成果物 `a - b` を作成
2. `02-テスト`: `gpt-5.6-sol` / medium。固定argvを検証broker経由で実行し、exit 1、`AssertionError`、`success=false` を保存
3. `03-修正`: `gpt-5.6-sol` / medium。制御側がargv、exit code、timeout状態、stderrをFIX promptへ引き渡し、`a + b` に修正
4. `04-テスト`: `gpt-5.6-sol` / medium。同じ固定argvでexit 0、`success=true`。修正後code hashと証跡code hashが一致
5. `05-レビュー`: `gpt-6-astra` / high。製品コードを変更せずPASS、P0-P2なし
6. 最終状態: `COMPLETED`、`fix_count=1`、反映対象は `calc.py` のみ

主要証跡:

- 各工程のモデル・argv・cwd: `phases/*/invocation.json`
- 初回失敗: `phases/02-テスト/verification.json`
- 修正後成功: `phases/04-テスト/verification.json`
- 制御判断と失敗引継ぎ: `control-decisions.jsonl`
- Astra結果: `phases/05-レビュー/result.json`
- 反映対象: `changes.json`

## 検収中に発見して修正した事項

- `20260921-223349-de306be9`: WindowsでPATH上の`codex.ps1`を直接CreateProcessできず起動失敗。`.cmd`/`.ps1`解決と非表示起動を修正
- `20260921-223810-e41ef04d`: 実装工程内の自己修正により意図した独立FIX履歴が作れず、工程不一致をAstraが検出してBLOCKED。検収タスクを工程別指示へ修正
- `20260921-224226-d17b856b`: 初回検証のbroker証跡は失敗だったが、test agentの誤報とFIXへの具体的失敗情報不足をAstraがP2判定。検証clientが`success=false`でexit 1を返して証跡を表示するよう修正し、controllerからFIXとレビューへ具体的証跡を引き継ぐよう修正

先行runはいずれも合格実績には数えていない。最終成功runはこれらの修正後に新しくコピーされた同一ランタイムで実行した。

## 実モデルruntimeと最終製品の差分

成功runのruntime作成後、`controller`の全工程共通promptへ「要件、受入条件、必要入力に不明点や矛盾があれば推測せずBLOCKEDにする」という指示1文を追加した。この文言以外に、成功runのruntimeと最終製品コードの差分はない。

この最終文言追加については、全工程promptにBLOCKED指示が含まれることを確認する実子なしの短回帰3件と、`ruff`による静的検査に合格した。文言追加後の実モデル再実行は行っていない。したがって、上記の実モデル成功証跡は文言追加直前のruntimeに対するものであり、「最終ソースの全変更を実モデルで再実行済み」という意味ではない。

## 補足

実モデル試験は元リポジトリ外の小さい一時プロジェクトで行い、実データやAPIキーを読み取っていない。モデル代替は行っていない。GitHub上の実行、commit、pushも行っていない。
