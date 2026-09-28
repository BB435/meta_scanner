# 運用設計

更新日：2026-09-28。ここでは現在のCLIを使う手順と制限を示します。Windowsタスク登録と本運用の資源制御は未実装・未検証です。

## 準備

Windows 11 x64、通常版CPython 3.14以降、`uv`を使用します。`config.example.toml`を無視対象の`config.local.toml`へコピーし、入力ルート、Tesseract、学習データ、`llama-server.exe`、GGUFモデル、モデルSHA-256を実機に合わせます。設定例の入力は`tests/`です。対象文書は読取可能、`var/`は書込可能にしてください。モデルと原本・抽出本文はGitに入れません。

```powershell
uv sync --python 3.14
Copy-Item config.example.toml config.local.toml
uv run meta-scanner doctor --config config.local.toml
uv run meta-scanner scan --config config.local.toml --dry-run
uv run meta-scanner run --config config.local.toml --ignore-window
uv run meta-scanner status --config config.local.toml
```

`doctor`は設定、OCRファイルの有無、モデルファイルのSHA-256を確認します。実際のOCR・推論やメモリ・空き容量の診断はしません。`scan --dry-run`は候補件数と合計バイト数を返し、DBや原本を変更しません。`run`は通常22:00–翌06:00の設定枠内だけ動き、`--ignore-window`は手動処理用です。`--extract-only`を付けるとAIを起動せず、原文抜粋の暫定結果を作ります。`status`は直近実行の状態と4件数（`scanned`、`extracted`、`reused`、`errors`）を返します。

## 設定の有効範囲

`config.py`は必須4形式、任意の`txt/md`、重複しない実在ルート、単一ワーカー、厳密ハッシュ、シンボリックリンク非追跡、ループバックの推論URL、主要な抽出・OCR・AI設定を検証します。`ppt/doc/xls`と`legacy.enabled=true`は拒否します。設定例には将来用の項目も残っていますが、**未知のキーや未実装項目は一律には拒否されず、無視される場合があります**。現在有効な値は[設定読込コード](../src/meta_scanner/config.py)に合わせて確認してください。

## 終了コードと結果

| コード | 現在の意味 |
| --- | --- |
| `0` | 対象枠外で無処理、または新規エラーなしで完走。要確認レコードがあっても`0`。 |
| `10` | 走査または個別ファイルのエラーがある状態で完走。 |
| `11` | 夜間枠やローカルAI起動障害などで処理を保留。 |
| `20` | 設定、OCR資産、モデルファイル等の起動前確認に失敗。 |
| `21` | 別の`run`が同じDBで稼働中。 |
| `30` | 捕捉したDB・I/O・実行時の全体障害。 |

DBは`var/catalog.sqlite3`、本文は`var/cache/`、出力は`var/export/`（設定例）です。ルート走査を完走した実行だけ`catalog.jsonl`、`errors.jsonl`、`run.json`を更新します。カタログの各行は内容SHA-256ごとに1件で、全所在を含みます。AI生成も抜粋プレビューも`review_required`なので、原文確認なしに確定情報として取り込まないでください。

## エラーと再開

| 状況 | 現在の扱い |
| --- | --- |
| `PROTECTED`、`CORRUPT`、`DECODE_ERROR`、`LIMIT_EXCEEDED` | ファイル単位で記録。ハッシュ取得後に保存した失敗は同じ内容・抽出版で再利用する。サイズ上限などハッシュ前の失敗は次回も判定する。 |
| `SOURCE_CHANGED`、`IO_ERROR` | 該当パスの現行結果を外し、次回走査で再確認する。 |
| `METADATA_INVALID`などAI生成失敗 | 保存済み本文と完了したAI分割結果を次回使う。 |
| OCR途中の夜間枠終了 | 完了済みPDFページ・Office画像のOCR文字列を保存し、次回は先頭から抽出し直してOCR済み単位を再利用する。 |
| `NO_TEXT` | 抽出警告付きの暫定レコードを作る。 |

終了5分前から新しいファイル・OCR単位・AI分割を始めません。実行中の1回のOCR呼出しには設定したページタイムアウトが適用されますが、呼出し途中の即時停止はありません。保留時は出力を更新しないため、既存の`catalog.jsonl`は前回完走時の内容です。`run.json`には`complete_roots`を記録しますが、3出力ファイルの世代単位の整合性保証は未実装です。障害復旧時はまず`status`、次に`errors.jsonl`と`var/cache/llama-server.log`を確認します。

## 本運用までの作業

実文書でのOCR・日本語品質評価、16GB実機でのメモリ/所要時間測定、Windowsタスクスケジューラ登録、容量監視、バックアップ、世代別出力、`retry`/`export`コマンドを追加します。SQLiteがWAL稼働中ならDBファイルだけをコピーせず、アプリ停止中かSQLiteのバックアップAPIで採取してください。
