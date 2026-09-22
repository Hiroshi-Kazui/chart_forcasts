---
description: ハーネスの最終レビュー（検収）を行い、判定を提出する
argument-hint: "[実行ID]"
---

# 最終レビュー

発注先（Codex）の作業が終わり、ハーネスが反映直前で止まっている実行を検収します。
判定できるのは **claude-fable-5-1** のセッションだけです。別モデルで実行している場合は、
`/model fable` に切り替えてからこのコマンドを使ってください。

## 手順

1. **完了通知を受け取る。** 実行IDが分かっているなら次をバックグラウンド実行し、終了を待ちます。
   引数で実行IDを渡されていない場合は、利用者に尋ねてください。

   ```
   python -m claude_harness wait --run <実行ID> --json
   ```

   状態が `AWAITING_FINAL_REVIEW` なら検収に進みます。`COMPLETED` `FAILED` `BLOCKED` `STOPPED`
   の場合は検収対象ではないので、その状態を報告して終わります。

2. **依頼内容を読む。** `.harness/runs/<実行ID>/final-review-request.json` に、タスク名、要件、
   受入条件、編集範囲、変更ファイル一覧、各工程の証跡ディレクトリ、テスト時のコードハッシュ、
   原本と作業コピーの位置が入っています。

3. **差分を自分で確かめる。** `work_root` 配下の変更ファイルを実際に読み、`baseline` と
   `frozen_requirements`、テスト工程の `verification.json`、レビュー工程の `result.json` と
   突き合わせます。担当の自己申告や要約を根拠にしないでください。
   **製品コードと作業コピーは変更しません。** 変更すると判定が拒否されます。

4. **判定を書く。** 形式は `.claude/claude_harness/schemas/final-review.json` です。

   ```json
   {
     "status": "PASS",
     "summary": "受入条件と証跡を照合した結果",
     "findings": [],
     "reviewer": {"model": "claude-fable-5-1", "session": "<任意>"}
   }
   ```

   不合格にするときは `status` を `FAIL` にし、`findings` へ指摘を入れます。`severity` は
   P0〜P3、`evidence` には **必ず** `.harness/runs/<実行ID>/` 配下の証跡パスを書きます。
   証跡の無い指摘は受け付けられません。P0〜P2 を含む判定を `PASS` にすることもできません。
   判定ファイルは一時ディレクトリに書いてください（リポジトリ直下に置くと反映が競合します）。

5. **提出する。**

   ```
   python -m claude_harness final-review --run <実行ID> --file <判定JSONのパス>
   ```

   合格なら制御プロセスが再開し、編集範囲内の変更だけを原本へ反映して `COMPLETED` になります。
   不合格なら指摘が修正工程へ渡り、テストとレビューをやり直します。修正回数の上限に達している
   場合は実行が `FAILED` で終わります。

6. **結果を 1 行で報告する。** 反映されたか、差し戻したか、どちらかを述べます。

## 判定が拒否される条件

制御側は次を機械的に検査します。通らなければ状態は変わりません。

- 判定JSONが固定スキーマに適合しない
- `reviewer.model` が `claude-fable-5-1` でない
- 指摘の `evidence` が実行証跡ディレクトリの外を指している
- 作業コピーがテスト実行時から変更されている
