# 技術仕様書 — kakeibo

夫婦2人用家計簿Webアプリ MVP の技術仕様を定義する。プロダクト要求は [01_product_requirements.md](./01_product_requirements.md)、機能設計は [02_functional_design.md](./02_functional_design.md) を参照。

---

## 1. 技術スタック

| 区分 | 技術 | バージョン方針 |
|---|---|---|
| 言語 | Python | 3.12（`.python-version` で固定。Lambda ランタイムと一致させる） |
| パッケージ管理 | uv | 最新安定版。**pip 直接使用禁止**。依存は `pyproject.toml` + `uv.lock` で固定 |
| Web フレームワーク | FastAPI | `>=0.115`（`uv.lock` で固定） |
| Lambda アダプタ | Mangum | `>=0.17` |
| テンプレート | Jinja2 | FastAPI 同梱の `Jinja2Templates` を使用 |
| フロントエンド | 素の HTML/JS + CSS | ビルドツール不使用。モバイルファースト（iPhone Safari 対象） |
| グラフ | Chart.js | **CDN 読み込み**（バンドル・zip 同梱しない）。メジャーバージョンを URL で固定（例: `chart.js@4`） |
| HTTP クライアント | httpx | `>=0.27`。Gemini API 呼び出し用（SDK 不使用、§4 参照） |
| multipart 解析 | python-multipart | **ランタイム依存**（dev ではない）。FastAPI が `UploadFile` / `Form` で `multipart/form-data` を解析するのに必須（`POST /api/receipts/parse`・`POST /api/receipts`）。zip にも同梱する |
| データモデル | Pydantic | FastAPI が依存に含むが、直接 import するため明示依存として宣言 |
| Cookie 署名 | itsdangerous | `>=2.2` |
| AWS SDK | boto3 | **zip に同梱しない**（Lambda ランタイム同梱版を使用、§5 参照）。ローカル開発では dev 依存として追加 |
| ID 生成 | python-ulid | レシート ID（ULID）生成用 |
| Lint/Format | ruff | dev 依存 |
| テスト | pytest + pytest-cov + moto | dev 依存。AWS はすべて moto でモック、Gemini は httpx モック。**Phase B の本体実装・自動テストでは実 AWS・実 API を使わない**（唯一の例外は Phase B 最終ステップの手動スモーク。下記） |
| ローカルサーバ | uvicorn | dev 依存。`uv run uvicorn kakeibo.main:create_app --factory --reload`。Lambda 上では Mangum が担うため zip に同梱しない |

### Phase B における実 Gemini API の扱い（2026-09-12 決定）

Phase B の本体は **httpx モックのみ**で実装・テストする。その上で、**Phase B の最終ステップ**として手動スモークテスト `scripts/smoke_gemini.py` を実行する。

- 実レシート **2〜3 枚**を実 Gemini API に通し、抽出精度と分類（02 §5.2 のプロンプト・分類例）を調整する
- **オーナーによる Gemini API キーの発行が前提**。キーは環境変数 `GEMINI_API_KEY` で渡し、**リポジトリにコミットしない**
- 手動実行のスクリプトであり、`uv run pytest` の自動テストには含めない（CI・通常のテスト実行で実 API を叩かない）
- このスモークの完了（プロンプト調整を含む）を **Phase B の完了条件**とする（02 §5.2）

## 2. Lambda 構成

| 項目 | 値 | 理由 |
|---|---|---|
| リージョン | ap-northeast-1 | ユーザーは国内アクセスのみ |
| ランタイム | Python 3.12 | 開発環境と一致 |
| アーキテクチャ | arm64 (Graviton) | x86_64 より単価が約2割安く、本用途で互換性問題なし |
| メモリ | 512–1024 MB | 512 MB を初期値とし、画像 base64 処理・Gemini 応答待ちのレイテンシを見て調整。画像はリクエスト中メモリ上に保持するが、縮小済み JPEG 200–600KB + base64 で高々 1MB 程度のため 512 MB で十分 |
| タイムアウト | 60 秒 | Gemini 解析（画像1枚で数秒〜十数秒）+ リトライ余地（S3 put は確定保存リクエスト側） |
| 呼び出し方式 | **Lambda Function URL** | 下記 |
| ハンドラ | `kakeibo.main.handler`（Mangum で FastAPI アプリをラップ） | 単一 Lambda に全ルートを載せるモノリス構成 |

### Function URL 採用理由（API Gateway 不採用の理由）

- **無料**: Function URL 自体に課金がない。API Gateway HTTP API はリクエスト課金が発生する
- **29 秒制限がない**: API Gateway は統合タイムアウト 29 秒（既定）で、Gemini 解析が遅延した場合に切断されるリスクがある。Function URL は Lambda タイムアウト（60 秒）まで待てる
- 本アプリはユーザー2人・認証は自前（署名 Cookie）のため、API Gateway のオーソライザ・スロットリング等の機能を必要としない

AuthType は `NONE` とし、認証はアプリ層（§6）で行う。

## 3. 制約と設計判断

### 3-1. リクエストサイズ制限 → クライアント側 canvas 縮小

Lambda Function URL のペイロード上限は 6MB であり、multipart/base64 経由では実効上限がさらに小さくなる。iPhone のカメラ原画（3〜10MB, HEIC）はそのまま送れない。

**判断**: アップロード前にクライアント側で canvas に描画して縮小する。

- 長辺最大 1600px、JPEG 品質 0.8 → 実測想定 200–600KB
- レシート OCR 用途では 1600px で品目文字の判読に十分（縮小しすぎによる精度低下と、サイズ超過のバランス点）
- サーバー側での画像変換（Pillow 等）が不要になり、zip サイズ・メモリ・実行時間をすべて節約できる
- 縮小済み画像は解析（`POST /api/receipts/parse`）と確定保存（`POST /api/receipts`）の 2 回送信されるが、いずれも 6MB 上限に対して十分小さい

### 3-2. HEIC 対策

iPhone Safari のカメラ入力は HEIC を返しうるが、canvas の `toDataURL('image/jpeg')` / `toBlob` の**出力は常に JPEG** になるため、サーバー・Gemini に HEIC が届くことはない。

- Safari が `<input type="file" accept="image/*">` の時点で HEIC→JPEG 自動変換する挙動もあるが、バージョン依存のため**実機（iPhone Safari）での確認を Phase C の受け入れ項目に含める**
- canvas 読み込みに失敗する形式が来た場合はクライアント側でエラー表示し、アップロードさせない

### 3-3. レスポンス側も Lambda 経由で画像を返さない

Function URL はレスポンスにも 6MB 制限があり、また画像配信で Lambda の実行時間を消費するのは無駄なため、**保存済みレシート画像を Lambda がプロキシ配信するエンドポイントは作らない**。

- MVP のダッシュボードは画像表示を含まない（一覧はテキストのみ）
- 将来画像を見せる必要が出た場合は **S3 presigned GET URL** を発行して直接 S3 から取得させる（拡張パスとして明記。バケットは非公開のまま）

### 3-4. 集計は Scan ベース

年間 ~500 レシート × 6 品目 ≈ 1MB/年 未満であり、単一テーブルの Scan で月次集計しても RCU・レイテンシとも問題にならない。データ増大時は `MONTH#YYYY-MM` をキーとする GSI を追加する拡張パスを取る（データモデル詳細は 02_functional_design.md）。

### 3-5. バックアップは CSV + S3 画像で割り切る

**判断（2026-09-12）**: 自動バックアップ・PITR（Point-in-Time Recovery）は MVP では有効化しない。復旧手段は CSV エクスポート（画像ありのレシートは `receipt_id` と `date` 列から S3 キー `receipts/YYYY/MM/<receipt_id>.jpg` を一意に復元できる。`YYYY/MM` はレシート日付の年月）+ S3 画像原本とする。

- 手入力レシートには画像原本がない（明示的な「手入力で登録」から画像なしで保存した場合）。S3 オブジェクトは作られず、品目データの復旧元は CSV のみ。CSV の先頭列 `receipt_id` と品目の列・順序は画像ありと共通
- 年 1〜2 回の手動 CSV 取得（`GET /export.csv?month=all`）を運用ルールとする
- PITR は必要性が出てから有効化する（容量比例課金、本規模では月数円〜十数円）
- CSV から DynamoDB へ書き戻す復元スクリプトは Phase D 任意（品目データは CSV に全列揃っているため、必要時に書けば足りる）

### 3-6. S3 への画像保存は確定保存時のみ（2026-09-12 決定）

**判断**: 画像を S3 に書くのは `POST /api/receipts`（確定保存）の中だけとし、`POST /api/receipts/parse` では S3 に書かない。

- `parse` は受け取った JPEG を Lambda メモリ上で base64 化して Gemini に渡すだけ（ULID も採番せず、`s3_key` も返さない）。ブラウザは縮小済み Blob を保存完了までメモリに保持する
- 保存キーは `receipts/YYYY/MM/<ulid>.jpg` で、**`YYYY/MM` は確定したレシート日付の年月**（アップロード月ではない）。7月のレシートを8月に登録しても `receipts/2026/07/` に入り、CSV の `date` 列と一致する
- 確認画面を離脱しても S3 には何も残らないため、**孤児画像が構造上発生しない**
- 画像なしの手入力保存では S3 操作をすべて省略し、META の `s3_key` 属性も省略する
- 画像ありの書き込み順序は S3 put → DynamoDB。DynamoDB 書き込みに失敗した場合は S3 オブジェクトを best effort で削除して 500 を返す（画像ありの登録で DB だけが残る状態を避ける）
- `DELETE /api/receipts/{id}` は従来どおり DB のみ削除し、S3 画像は保持する

### 3-8. 保存はチャンク書き込み + `client_token` による冪等化（2026-09-12 決定）

`BatchWriteItem` は 1 リクエスト **25 アイテム**までで、複数リクエストにまたがる書き込みは**アトミックではない**。1 レシート = META 1 件 + ITEM 最大 999 件 = 最大 1000 アイテムのため、**最大 40 リクエスト**（META を別リクエストで書く実装なら 41）に分割される。

- 途中のチャンクが失敗した場合は、**部分的に書かれたレシートを残さない**。当該 `PK` の書き込み済みアイテムを削除（`DELETE /api/receipts/{id}` と同じ経路を再利用）し、S3 オブジェクトも削除したうえで 500 を返す（後始末は best effort）
- 再送信による二重登録は `client_token`（UUID v4・確認画面が 1 回だけ生成）で防ぐ。同一 `client_token` の META が既に存在すれば書き込まずに既存 `receipt_id` を 201 で返す
- 重複確認は MVP 規模では **Scan + フィルタ**で足りる（§3-4 と同根拠）。データ増加時は `client_token` を PK とする GSI を追加する
- 詳細な API 契約は 02_functional_design.md §4.5

### 3-7. タイムゾーンは JST に統一

Lambda は UTC で動作するため、日付を扱う箇所では明示的に JST（Asia/Tokyo）へ変換する。アプリが扱う日付はすべて JST であり、`created_at` は `+09:00` 付きの ISO8601 で保存する。`date` を Gemini が抽出できなかった場合の「今日の日付で補完」も JST の今日を用いる（02_functional_design.md §4.4）。

## 4. Gemini API 仕様

| 項目 | 内容 |
|---|---|
| モデル | `gemini-2.5-flash`（無料枠対象を想定） |
| 呼び出し | `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent` へ httpx で直接 POST |
| 認証 | API キー（環境変数 `GEMINI_API_KEY`、§6） |
| 入力 | 縮小済み JPEG を Lambda メモリ上で base64 化し inline_data として送信（S3 を経由しない）+ 分類ガイド付きプロンプト |
| 出力 | 構造化出力（`response_mime_type: application/json` + `response_schema`）で下記スキーマを強制 |
| タイムアウト | HTTP タイムアウト **25 秒**。リトライは 1 回・待機 1 秒のため、最悪ケースは **25 + 1 + 25 = 51 秒 < Lambda タイムアウト 60 秒**（旧仕様の 30 秒では 30 + 1 + 30 = 61 秒で超過するため 25 秒に変更。02 §5.1 / §5.4） |

出力スキーマ:

```json
{
  "store": "string",
  "date": "YYYY-MM-DD",
  "items": [{"name": "string", "price": "int（円・値引きは負数可）", "category": "child|couple|excluded", "uncertain": "bool"}],
  "total": "int"
}
```

- `uncertain: bool` は分類ガイドで判断できなかった品目のみ `true`。Pydantic モデル（`ParsedItem` 相当）にも `uncertain: bool` を持たせ、確認・修正ビューでの黄色ハイライト／「要確認 N 件」表示に使う。確定保存（`POST /api/receipts`）では送らず DynamoDB にも保存しない（ドラフト表示専用の属性）

### httpx 直 REST 採用理由（SDK 不使用）

- `google-genai` SDK は依存ツリーが大きく、Lambda zip の肥大とコールドスタート悪化を招く。呼び出しは単一エンドポイントの POST 1 種類のみで、SDK の抽象化に価値がない
- httpx は既に依存に含まれ、テスト時のモック（`httpx.MockTransport` / respx）が容易

### 無料枠レート

gemini-2.5-flash の無料枠は目安として **10 RPM / 250 リクエスト日 程度**（2026-08 時点の公開情報ベース）。本アプリの利用頻度（1日数レシート）では十分だが、**無料枠の条件は変動するため実装時（Phase B/C）に最新の公式ドキュメントを確認する**。

- レート超過（HTTP 429）時は `/api/receipts/parse` がエラーメッセージを返し、ユーザーに時間をおいた再試行を促す（自動リトライは MVP では入れない）
- プロンプト内容（分類ガイド・精度リスク項目）は 02_functional_design.md に定義

## 5. パッケージング（zip ビルド）

Lambda へは zip でデプロイする（コンテナ不使用。イメージ管理コストが本規模に見合わないため）。

### macOS からのクロスプラットフォームビルド

開発機は macOS のため、Lambda（aarch64 / manylinux）向けにプラットフォーム指定でビルドする。

実装は [`scripts/deploy.sh`](../scripts/deploy.sh) を参照。`pyproject.toml` の `[project].dependencies` から boto3 / botocore を除いた実行時依存を抽出し、`uv.lock` のバージョンを制約としてインストールする。`uv pip install` には `--target build/ --python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary :all:` を指定し、`src/kakeibo` をコピーして `lambda.zip` を作成する。zip から boto3 / botocore・tests・`__pycache__`・バイトコードを除外する。

```bash
bash scripts/deploy.sh --build-only  # ローカルで zip 作成のみ
bash scripts/deploy.sh               # zip 作成 + Lambda コード更新
bash scripts/deploy.sh --env         # 環境変数も更新
```

### 同梱しないもの

| 対象 | 理由 |
|---|---|
| boto3 / botocore | **Lambda Python ランタイムに同梱**されているため zip から除外し、サイズを大幅節約。ローカルテスト用には dev 依存として別途インストール |
| Chart.js | CDN 読み込み（§1） |
| dev 依存（pytest, moto, ruff 等） | `--no-dev` 相当の解決で除外 |

## 6. 認証・秘密情報管理

### 認証

- 共有パスコード方式（ユーザーは夫婦2人のみ。個別アカウントは作らない）
- `POST /login` でパスコード照合 → **itsdangerous による署名付き Cookie** を発行（有効期間 約90日、`HttpOnly` / `Secure` / `SameSite=Lax`）
- 署名ペイロードは `{"auth": true, "pv": <パスフレーズ指紋>}`（`pv` = `sha256(APP_PASSCODE).hexdigest()[:8]`）。検証は署名 + `max_age` + `pv` 一致で行い、**`APP_PASSCODE` を変更すると発行済み Cookie が全端末で即時無効化される**（02 §6.1）
- 未認証時: `/api/*` は 401、ページは `/login` へ 303 リダイレクト。`/export.csv` はブラウザが直接開く URL のため**ページ扱い（303）**とする。認証不要（public）は `/healthz`・`/login`・`/static/*` の 3 つのみ（[02_functional_design.md](./02_functional_design.md) §6.2 の middleware 挙動に準拠）
- CSRF 対策: `SameSite=Lax` により他サイト起点の POST / DELETE には Cookie が付与されないため、CSRF トークンは設けない。全ての状態変更 API は POST / DELETE のみ（GET で状態変更しない）
- `Secure` 属性は環境変数 `COOKIE_SECURE`（既定 `true`）で切り替える。ローカル開発（http://localhost）で Safari 等が Cookie を保存しない場合のみ `false`、本番（Function URL は常に HTTPS）は常に `true`。**秘密情報ではない**ため下表（秘密情報）には含めない
- ログアウト機能は MVP では設けない（端末は夫婦各自の iPhone で共用端末を想定しない）。無効化が必要な場合は `APP_PASSCODE` 変更で全端末を落とす

**設計判断: ログイン試行制限なし + パスフレーズ要件（2026-09-12 決定）**

ログイン試行回数制限・ロックアウトは設けない（利用者2人、締め出し事故のリスクの方が現実的）。代わりに `APP_PASSCODE` は 12 文字以上のパスフレーズとし、iPhone のキーチェーン保存を前提とする。Cookie 有効期間 90 日のため入力頻度は低い。

- **文字種の強制はしない**（英大小・数字・記号の混在を要求しない）。チェックするのは**長さ 12 文字以上のみ**で、アプリ起動時に検査し、満たさない場合も起動は継続して警告ログを出す（warn-only）
- 照合は `secrets.compare_digest` による constant-time 比較（02 §6.1）
- 総当たり耐性はパスフレーズのエントロピーで担保する。短い数字パスコードは使わない

### 秘密情報

| 秘密情報 | MVP（Phase C） | 将来（Phase D） |
|---|---|---|
| `GEMINI_API_KEY` | Lambda 環境変数 | SSM Parameter Store SecureString |
| `SESSION_SECRET`（Cookie 署名鍵） | Lambda 環境変数 | 同上 |
| `APP_PASSCODE` | Lambda 環境変数 | 同上 |

- `COOKIE_SECURE` は秘密情報ではない設定値のため上表に含めない（Lambda 環境変数に平文で設定する。既定 `true`）
- コードへのハードコード禁止。ローカル開発では**ローカル用の dotenv ファイル**（`.env`。`.gitignore` 対象）から環境変数を読み込む。読み込みは**開発時のみ**で、Lambda 上では常に実際の環境変数を使う

### 設定用環境変数（秘密情報ではない）

| 環境変数 | 用途 | 備考 |
|---|---|---|
| `KAKEIBO_TABLE_NAME` | DynamoDB テーブル名（既定 `kakeibo`） | 秘密情報ではない |
| `KAKEIBO_BUCKET_NAME` | レシート画像の S3 バケット名 | 秘密情報ではない |
| `AWS_REGION` | リージョン（`ap-northeast-1`） | Lambda では実行環境が自動設定する。ローカルのみ明示 |
| `COOKIE_SECURE` | Cookie の `Secure` 属性（既定 `true`） | 秘密情報ではない |

これらは**秘密情報ではない**ため上の秘密情報表には含めず、Lambda 環境変数に平文で設定してよい。ローカルでは上記の dotenv ファイルにまとめる（テストでは `monkeypatch` で差し替える）。
- Terraform 化（Phase D）の際に SSM SecureString へ移行し、Lambda には起動時取得またはパラメータ参照で渡す

## 7. コスト見積り（月額）

| サービス | 使用量想定 | 月額 |
|---|---|---|
| Lambda | 数百リクエスト/月 × 512MB × 数秒。無料枠（100万リクエスト・40万GB秒/月, 常時無料）内 | 0円 |
| Lambda Function URL | 追加課金なし | 0円 |
| DynamoDB | on-demand。~1MB/年・読み書きとも無料枠（25GB, 常時無料）内 | 0円 |
| S3 | レシート画像 ~600KB × ~40枚/月 ≈ 25MB/月 増加（確定保存されたレシートのみ）。数年分でも数GB未満 | 0〜数円（1GB あたり約 3.6円/月） |
| Gemini API | 無料枠内（gemini-2.5-flash, §4） | 0円 |
| CloudWatch Logs | 少量。無料枠（5GB 取り込み）内 | 0円 |
| **合計** | | **月 0〜数十円**（実質 S3 ストレージのみが漸増） |

前提: 東京リージョン・2026-08 時点の料金。無料枠条件の変更があり得るため、Phase C デプロイ時に Billing アラート（例: 100円）を設定する。

## 8. 開発ツール

| ツール | 用途 | 備考 |
|---|---|---|
| uv | パッケージ管理・仮想環境・スクリプト実行 | **pip 直接使用禁止**。`uv add` / `uv sync` / `uv run` を使用 |
| ruff | Lint + Format | `uv run ruff check .` / `uv run ruff format .` |
| pytest | テスト | `uv run pytest -v`。ロジックは TDD で書く（Phase B） |
| moto | AWS モック | DynamoDB / S3 をローカル完結でテスト。実 AWS は Phase B では使わない |
| httpx モック | Gemini 呼び出しのテスト | `httpx.MockTransport` 等で実 API を叩かずに検証 |
| `scripts/smoke_gemini.py` | 実 Gemini API への手動スモーク | **Phase B 最終ステップのみ**手動実行（§1）。自動テストには含めない |

コーディング規約・レイヤ規則（routers → services → repositories）・ディレクトリ構成は [04_repository_structure.md](./04_repository_structure.md) と [05_development_guidelines.md](./05_development_guidelines.md) に従う。
