# 技術選定と検証状況

更新日：2026-09-28。採用済みの実装と、性能・品質評価が必要な項目を分けて記載します。実際の依存範囲は[pyproject.toml](../pyproject.toml)、解決済みの版は[uv.lock](../uv.lock)を参照してください。

## 現在の構成

| 用途 | 採用済み | 現在の使い方 |
| --- | --- | --- |
| 実行環境 | Windows 11 x64、CPython 3.14以降、uv | `uv.lock`でPython依存を固定。3.14以降の各版の動作保証は未評価。 |
| 状態・設定 | 標準`sqlite3`、`hashlib`、`tomllib` | SQLite WAL、全量SHA-256、TOML設定。DB移行は起動時の簡易な列追加。 |
| docx/pptx/xlsx | python-docx、python-pptx、openpyxl、lxml | 標準APIで文字を取得し、OOXML ZIPで保護の一部と内部画像を扱う。 |
| PDF | pypdf、pypdfium2 | 前者で暗号化確認と文字抽出、後者でOCR用ページ描画。LibreOfficeは不要。 |
| 日本語候補語 | SudachiPy、SudachiDict-full | AIを使わない暫定メタデータのキーワード抽出に使用。AI生成では候補語を直接入力していない。 |
| 画像・OCR | Pillow、Tesseract、`jpn`/`eng`/`jpn_vert`学習データ | CPUで実行。PDFは全ページOCR、Officeは内部画像をOCR。学習データの配布系統は設定パスだけでは確定できない。 |
| ローカルAI | CPU版llama.cpp `llama-server.exe`、GGUF | `127.0.0.1`へHTTP接続し、JSONスキーマを指定。GPUレイヤー0、1並列、思考モード無効。 |
| 設定例のモデル | `Qwen3.5-4B-Q4_K_M.gguf` | `C:\Tools\llama.cpp\models\`を参照し、設定のSHA-256と実ファイルを照合。配布元・リビジョン・ライセンスの台帳化は未完了。 |

設定例の`context_tokens=4096`、`chunk_tokens=1800`、`max_output_tokens=768`は初期値です。実装は日本語向けにチャンクを保守的な文字数へ換算します。これらの値が16GB RAMで十分速く、品質基準を満たすかは未測定です。旧Officeの`ppt/doc/xls`向けLibreOffice変換は候補のままで、コードからは起動しません。

## 確認済みと未確認

- Python 3.14用の依存を`uv.lock`に記録し、指定されたllama.cpp実行物とGGUFのパスを[設定例](../config.example.toml)に反映済みです。`doctor`はモデルファイルのハッシュとOCR資産の有無を確認します。
- 実文書での必須4形式の抽出範囲、保護検知の網羅性、日本語OCR精度、AI要約品質、メモリピーク、1夜の処理量は未評価です。`doctor`の成功だけではこれらを確認できません。
- PDFのページ描画とOCRを全ページに行うため、テキスト主体のPDFでも処理時間が増えます。大きな文書と1,000件超の実測が必要です。
- 本運用前に、GGUFの**実際の取得元**とリビジョン、ライセンス、SHA-256、llama.cppビルドと学習データの配布元を台帳化します。モデル名から利用条件を推定しません。

## 実装の参照資料

- [python-docx API](https://python-docx.readthedocs.io/en/latest/api/document.html)、[python-pptx](https://python-pptx.readthedocs.io/en/latest/user/quickstart.html)、[openpyxl読取専用モード](https://openpyxl.readthedocs.io/en/stable/optimized.html)：Office形式の読み取り範囲。
- [pypdfの暗号化文書](https://pypdf.readthedocs.io/en/stable/user/encryption-decryption.html)、[pypdfium2](https://pypi.org/project/pypdfium2/)：PDFの読取と描画。
- [Tesseract導入資料](https://tesseract-ocr.github.io/tessdoc/Installation.html)、[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)：外部実行物とローカル推論API。
- [Windows Task Scheduler](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-start-page)：将来の定期実行候補。現在は手動CLI運転です。
