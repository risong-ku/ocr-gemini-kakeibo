# 04. リポジトリ構造定義書

## 1. ディレクトリツリー

```
kakeibo/
├── CLAUDE.md                        # Claude Code 向けオーケストレーション指示
├── README.md                        # プロジェクト概要・セットアップ手順
├── pyproject.toml                   # 依存関係・ツール設定（uv 管理）
├── uv.lock                          # 依存ロックファイル（uv が生成）
├── .gitignore                       # Git 除外設定
├── .python-version                  # Python 3.12 固定
├── docs/
│   ├── 01_product_requirements.md   # プロダクト要求定義書
│   ├── 02_functional_design.md      # 機能設計書
│   ├── 03_tech_spec.md              # 技術仕様書
│   ├── 04_repository_structure.md   # リポジトリ構造定義書（本書）
│   └── 05_development_guidelines.md # 開発ガイドライン
├── src/kakeibo/
│   ├── __init__.py                  # パッケージルート
│   ├── main.py                      # FastAPI app factory + Mangum handler
│   ├── config.py                    # 環境変数の読み込み・設定
│   ├── auth.py                      # 共有パスコード認証・署名付きCookie
│   ├── models.py                    # Pydantic モデル（Receipt / LineItem / draft）
│   ├── routers/
│   │   ├── pages.py                 # GET /login, /, /dashboard, /healthz
│   │   ├── receipts.py              # POST /api/receipts/parse, POST/DELETE /api/receipts
│   │   ├── summary.py               # GET /api/summary
│   │   └── export.py                # GET /export.csv
│   ├── services/
│   │   ├── gemini.py                # Gemini API 呼び出し（httpx 直REST・構造化出力）
│   │   └── summary.py               # 日次・月次カテゴリ集計ロジック
│   ├── repositories/
│   │   ├── dynamo.py                # DynamoDB 単一テーブルアクセス
│   │   └── s3.py                    # S3 レシート画像 put/get
│   ├── templates/
│   │   ├── base.html                # 共通レイアウト
│   │   ├── login.html               # パスコード入力画面
│   │   ├── upload.html              # 撮影→縮小→解析→確認・修正・保存画面
│   │   └── dashboard.html           # 月次ダッシュボード画面
│   └── static/
│       ├── app.js                   # アップロード画面ロジック（canvas縮小・確認UI）
│       ├── dashboard.js             # グラフ描画（Chart.js）・月切替
│       └── style.css                # モバイルファーストのスタイル
├── tests/
│   ├── conftest.py                  # 共通 fixture（moto・mocked httpx・test client）
│   └── test_healthz.py              # ヘルスチェックのテスト（以降 src 構造をミラーして追加）
├── scripts/
│   ├── bootstrap_aws.sh             # AWS リソース初期作成（Phase C で実装）
│   ├── deploy.sh                    # zip ビルド + Lambda デプロイ（Phase C で実装）
│   └── smoke_gemini.py              # Gemini 実 API 疎通確認（Phase C で実装）
└── infra/terraform/                 # Terraform 化（Phase D・任意）
```

> `scripts/` 配下の 3 ファイルは Phase C、`infra/terraform/` の中身は Phase D で作成する（Phase A〜B の時点ではディレクトリのみ存在する）。`src/` 配下の各ファイルは Phase A ではスタブで、Phase B で実装する。

## 2. ディレクトリの役割

| ディレクトリ | 役割 |
|---|---|
| `docs/` | 設計文書一式（要求定義〜開発ガイドライン） |
| `src/kakeibo/` | アプリケーション本体（Lambda にデプロイされるパッケージ） |
| `src/kakeibo/routers/` | HTTP エンドポイント定義（リクエスト受付とレスポンス整形のみ） |
| `src/kakeibo/services/` | ビジネスロジック・外部 API 呼び出し（Gemini、集計） |
| `src/kakeibo/repositories/` | AWS リソースアクセス（DynamoDB / S3）の隔離層 |
| `src/kakeibo/templates/` | Jinja2 テンプレート（画面 HTML） |
| `src/kakeibo/static/` | 素の JS / CSS（ビルドなしで配信） |
| `tests/` | pytest テスト（moto + mocked httpx、実 AWS 不使用） |
| `scripts/` | 手動デプロイ・疎通確認スクリプト（Phase C） |
| `infra/terraform/` | IaC 定義（Phase D、SSM SecureString 移行を含む） |

## 3. レイヤリング規則

```
routers → services → repositories
```

- 依存方向は **routers → services → repositories の一方向のみ**。逆方向 import は禁止（例: `services/` から `routers/` を import しない）
- `repositories/` は boto3 を扱う唯一の層。`routers/` `services/` から boto3 を直接呼ばない
- `templates/` と `static/` は **routers（pages.py）からのみ参照**する。services / repositories からは触らない
- `models.py` `config.py` `auth.py` は全層から参照可の横断モジュール

## 4. 配置ルール

| 追加するもの | 置き場所 |
|---|---|
| 新しいエンドポイント | `routers/` に追加（既存 4 ファイルの責務に合わせ、合わなければ新ファイル） |
| 外部 API 呼び出し（Gemini 等） | `services/` に追加（httpx 直 REST、SDK 不使用） |
| AWS アクセス（DynamoDB / S3 / 将来の SSM） | `repositories/` に追加 |
| テスト | `tests/` に `src/kakeibo/` の構造をミラーして配置（例: `tests/services/test_gemini.py`） |
| 画面・UI 資産 | `templates/` / `static/`（フレームワーク・ビルドツールは導入しない） |

## 5. 生成物と手書きの区別

- **手書き（レビュー対象）**: `src/` `tests/` `docs/` `scripts/` `infra/` `pyproject.toml` `CLAUDE.md` `README.md` `.gitignore` `.python-version`
- **生成物（手で編集しない）**: `uv.lock`（`uv` が管理。コミットはする）、`build/`（Lambda zip 用の一時ディレクトリ。コミットしない）
- **`.gitignore` 対象**: `build/`、`*.zip`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.env`、`.DS_Store`、`.claude/settings.local.json`
