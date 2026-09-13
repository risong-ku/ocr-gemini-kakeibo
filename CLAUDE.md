# CLAUDE.md — kakeibo

## プロジェクト概要

夫婦2人用の家計簿WebアプリMVP。レシート画像をGemini APIで品目単位に読み取り、各品目を子ども費(child)/夫婦生活費(couple)/対象外(excluded)に分類・集計し、夫婦間の月次費用分担を支援する。スタックは FastAPI + Jinja2 + 素のHTML/JS を Lambda Function URL (Mangum, arm64) で動かし、DynamoDB(単一テーブル) + S3 を使うAWSサーバーレス構成。ユーザーは夫婦2人、iPhone Safariからの利用が前提。

## ドキュメントマップ

| 文書 | 答える問い |
|---|---|
| [README.md](README.md) | 何のアプリか・セットアップ・デプロイの入口 |
| [docs/01_product_requirements.md](docs/01_product_requirements.md) | 誰の何の課題を解くか・スコープ/非スコープ (プロダクト要求定義書) |
| [docs/02_functional_design.md](docs/02_functional_design.md) | 画面・API・ユーザーフローがどう動くか (機能設計書) |
| [docs/03_tech_spec.md](docs/03_tech_spec.md) | 技術選定・AWS構成・データモデル・Gemini連携の詳細 (技術仕様書) |
| [docs/04_repository_structure.md](docs/04_repository_structure.md) | どのコードがどこにあるか・レイヤ規則 (リポジトリ構造定義書) |
| [docs/05_development_guidelines.md](docs/05_development_guidelines.md) | コーディング規約・テスト方針・開発フロー (開発ガイドライン) |

## ディレクトリ構造（要約）

```
src/kakeibo/
├── main.py          # FastAPI app factory + Mangum handler
├── config.py  auth.py  models.py
├── routers/         # pages, receipts, summary, export
├── services/        # gemini, summary
├── repositories/    # dynamo, s3
├── templates/       # base, login, upload, dashboard
└── static/          # app.js, dashboard.js, style.css
tests/               # pytest + moto (実AWS不使用)
scripts/             # bootstrap_aws.sh, deploy.sh, smoke_gemini.py (Phase C)
infra/terraform/     # Phase D
docs/                # 01〜05 仕様文書
```

## 開発コマンド

```bash
uv sync                                # 依存インストール
uv run pytest                          # テスト
uv run ruff check .                    # リント
uv run ruff format .                   # フォーマット
uv run uvicorn kakeibo.main:create_app --factory --reload   # ローカル起動
```

ローカル/デプロイに必要な環境変数:

| 変数 | 用途 |
|---|---|
| `GEMINI_API_KEY` | Gemini APIキー |
| `SESSION_SECRET` | 署名付きCookieの秘密鍵 (itsdangerous) |
| `APP_PASSCODE` | 共有ログインパスコード |

## スペック駆動プロセス

- **docs/ の文書が正**。実装と文書が食い違ったら文書を先に直してから実装する
- 仕様変更はまず該当文書 (01〜05) を更新し、合意してから実装に着手する
- フェーズ: A=スキャフォールド+文書 → B=コア実装(TDD) → C=手動デプロイ → D=Terraform化(任意)
- **各フェーズ末にユーザーレビューゲート**。承認なしに次フェーズへ進まない

## 規約要点

- パッケージ管理は **uvのみ。pip直接使用禁止** (`uv add` / `uv sync` / `uv run`)
- コード・識別子・コメントは**英語**、UI文言・ドキュメントは**日本語**
- レイヤ規則: **routers → services → repositories** の一方向。逆方向importは禁止
- テストは**実AWSを使わない**。DynamoDB/S3は**moto**、Gemini呼び出しはhttpxをmock
- Gemini連携はhttpx直REST (SDK不使用)。JSON schema構造化出力
- 秘密情報はハードコード禁止。MVPはLambda環境変数、Phase DでSSM SecureStringへ移行
