# meta-scanner

Windows 11上で文書を夜間処理し、データカタログ用のタイトル・要約・キーワードを作成するPythonプロジェクトです。日本語文書を重視し、文書内容は外部送信せず、無料で利用できるローカルAIを使用します。

## 現在の状態

指定ルートを走査し、内容ハッシュで重複・更新を判定します。pptx/docx/xlsx/pdfを直接読み、PDFページとOffice内の画像はTesseractでOCRします。DOCXは本文とヘッダー・フッターの段落・表を抽出します。抽出本文を保存した後、CPU版llama.cppとローカルGGUFモデルで日本語のタイトル・要約・キーワードを生成します。長文は分割し、根拠の引用を原文と照合します。完了したPDFページ・Office画像のOCR結果とAI分割結果はSQLiteに保存し、次回実行で再利用します。生成結果はすべて`review_required`としてJSONLへ出力します。自動テストは開始段階で、DOCXヘッダー・フッター表の抽出を確認しています。旧Office形式、LibreOffice変換、抽出本文の途中からの再開、品質評価、タスク登録は未実装です。

## 開発資料

1. [要件定義](docs/requirements.md)：対象、重複・更新の定義、品質・受け入れ条件。
2. [基本設計](docs/architecture.md)：処理の流れ、抽出方式、状態管理、データ設計。
3. [技術選定](docs/technology.md)：候補と公式資料、Python 3.14対応の確認範囲。
4. [開発計画](docs/development-plan.md)：着手順、完了条件、評価用文書。
5. [運用設計](docs/operations.md)：夜間実行、再開、エラー・容量管理。
6. [設定例](config.example.toml) / [カタログ出力スキーマ](schemas/catalog-record.schema.json)。

## 前提

- Windows 11 x64、メモリ16GB、GPUなし。Python 3.14以降（初期検証は通常版CPython 3.14）。
- 数十MB程度のファイルが1,000件超。初回は複数夜の処理を許容する設計案です。
- 必須形式：`.pptx`、`.docx`、`.xlsx`、`.pdf`。
- オプション：`.ppt`、`.doc`、`.xls`、`.txt`、`.md`。
- SQLiteで状態を管理し、UTF-8 JSON Linesをカタログ連携の初期形式とします。

## 開発環境の開始

リポジトリのルートで実行します。Pythonの取得が必要な場合、`uv sync`はネットワークを使用します。

```powershell
uv sync --python 3.14
uv run meta-scanner --help
uv build
Copy-Item config.example.toml config.local.toml
```

`config.local.toml`の入力ルートを実環境に合わせて編集します。設定例では`tests/`を入力にしています。Tesseract本体と日本語・英語の学習データ、`llama-server.exe`、GGUFモデルを配置してください。`llm.model_sha256`はモデルの実際のSHA-256に合わせます。`doctor`はモデルのハッシュも確認します。`legacy`節は将来用で、旧Office形式は指定できません。

```powershell
uv run meta-scanner doctor --config config.local.toml
uv run meta-scanner scan --config config.local.toml --dry-run
uv run meta-scanner run --config config.local.toml --ignore-window
uv run meta-scanner run --config config.local.toml --ignore-window --extract-only
uv run meta-scanner status --config config.local.toml
```

通常の`run`は設定した夜間枠内だけ処理します。`--ignore-window`は手動実行用です。`--extract-only`はローカルAIを起動せず、原文抜粋の暫定レコードを作ります。結果は`var/catalog.sqlite3`、抽出本文は`var/cache/`、カタログは`var/export/catalog.jsonl`に保存します。出力レコードはすべて`review_required`です。AI生成結果には`generation_method=local_llm`と根拠情報が入ります。原本の品質評価が終わるまで本番のカタログ結果として扱わないでください。

ファイル中断後は保存済みの抽出本文、OCR単位結果、AI分割結果を使って再開します。OCRで中断した文書は先頭から抽出し直しますが、完了済みOCR単位は再認識しません。旧Office変換、JSONL世代切替、タスク登録、実文書での日本語品質評価は[開発計画](docs/development-plan.md)の残作業です。`uv.lock`は追加された依存を固定します。
