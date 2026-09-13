# 05. 開発ガイドライン

kakeibo リポジトリの開発規約。コード・識別子・コマンドは英語、UI 表示文言とドキュメントは日本語とする。

## 1. パッケージ管理（uv のみ・pip 直接使用禁止）

| 操作 | コマンド |
|---|---|
| 依存追加 | `uv add <package>` |
| 開発依存追加 | `uv add --dev <package>` |
| 依存同期 | `uv sync` |
| スクリプト実行 | `uv run <command>`（例: `uv run pytest`, `uv run ruff check .`） |

依存は `pyproject.toml` で管理し、`uv.lock` をコミットする。`pip install` を直接叩かない。

## 2. コーディング規約

- lint・format は ruff に一元化する: `uv run ruff check --fix .` / `uv run ruff format .`
- 全関数に型ヒント必須（引数・戻り値）
- ネストを深くせず早期リターンを使う
- マジックナンバー禁止。定数は `UPPER_SNAKE_CASE` でモジュール先頭に定義する
- 1 ファイル 200–400 行を目安とし、800 行を上限とする
- レイヤ規則: `routers → services → repositories`。逆方向 import 禁止

## 3. 命名規約

| 対象 | 規約 | 例 |
|---|---|---|
| 変数・関数 | 英語 snake_case | `parse_receipt()`, `receipt_id` |
| クラス | 英語 PascalCase | `LineItem`, `ReceiptDraft` |
| 定数 | UPPER_SNAKE_CASE | `MAX_IMAGE_PX = 1600` |
| UI 表示文言 | 日本語 | 「子ども費」「夫婦生活費」「対象外」 |
| テスト関数 | `test_{対象}_{条件}_{期待結果}` | `test_parse_receipt_with_invalid_image_returns_400` |

## 4. テスト規約

- AAA パターン（Arrange / Act / Assert）で書く
- pytest + moto を使用。**実 AWS・実 Gemini API の呼び出しは禁止**
  - DynamoDB / S3 → moto でモック
  - Gemini API → httpx をモック（`respx` または `unittest.mock`）
- モックは外部境界（AWS・Gemini・HTTP）のみ。内部ロジックはモックしない
- カバレッジ目安 80%: `uv run pytest --cov=src --cov-report=term-missing`
- 実 API 呼び出しが許されるのは `scripts/smoke_gemini.py` のみ（手動実行専用・pytest 対象外）
- 共通フィクスチャは `tests/conftest.py` に置く

## 5. Git 規約

- ブランチ: `main` を基幹とし、作業は `feature/xxx` ブランチで行う
- コミットメッセージ: 英語・命令形（例: `Add receipt parse endpoint`, `Fix CSV BOM handling`）
- 運用はローカル完結。各フェーズ末のユーザーレビューゲート前に push は不要
- コミット禁止物: `.env`、API キー等の秘密情報、`build/` 等の生成物（`.gitignore` で除外）

## 6. Definition of Done

変更は以下をすべて満たしたときに完了とする。

- [ ] `uv run pytest` が全件パス（新規ロジックにはテストを追加済み）
- [ ] `uv run ruff check .` と `uv run ruff format --check .` がクリーン
- [ ] 仕様に影響する変更は `docs/` の該当文書を更新済み
- [ ] 画面に関わる変更は iPhone Safari 相当（モバイル幅）での手動確認済み
