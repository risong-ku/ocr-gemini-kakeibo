"""Gemini REST parsing and recoverable draft validation; no persistence."""

import base64
import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from kakeibo.models import (
    JST,
    Category,
    GeminiReceipt,
    ParsedItem,
    ReceiptDraft,
    now_jst,
    validate_receipt_date,
)
from kakeibo.repositories.s3 import JPEG_CONTENT_TYPE

GENERATE_CONTENT_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)
TIMEOUT_SECONDS = 25
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1
UNKNOWN_AMOUNT = 0
FIRST_ITEM_NUMBER = 1
TAX_RATES: tuple[float, ...] = (1.10, 1.08)
TAX_TOLERANCE_YEN = 1
# Verbatim functional design §5.2; deliberately independent of docs at runtime.
PROMPT = """あなたは日本のスーパー・ドラッグストア・ベビー用品店のレシートを読み取る係です。
添付のレシート画像から、店名・日付・品目一覧・合計金額を抽出し、
各品目を次の3カテゴリに分類してください。

# カテゴリ分類ガイド
- child（子ども費）: ベビー・子ども向けと分かるもの。
  - ベビー向け消耗品: おむつ、おしりふき、赤ちゃん用お菓子、離乳食、粉ミルク など
  - ベビー向け雑貨: おもちゃ、絵本、ベビー用品（哺乳瓶、ベビー用洗剤・スキンケア等）
  - 子ども向け衣類: 子ども服・子ども靴
- couple（夫婦生活費）: 家族で使うもの全般。child でも個人の買い物でもないものはすべてここ。
  例: 食材、調味料、飲料、酒（食費として扱う）、外食、日用品、洗剤、
  ティッシュ、キッチン用品
- excluded（対象外): 個人の買い物、および支出の実態がない行。
  例: 大人の服、本、趣味のためのもの、個人用の雑貨、
  ポイント利用行、レジ袋以外の手数料調整行
  （値引き行・割引行はここに入れない。下記「分類の規則」2 に従い対象品目と同じカテゴリにする）

# 店名ヒント
- アカチャンホンポ・西松屋・バースデイ等のベビー専門店のレシートは、
  品目名が略称で読めなくても child に倒してください。

# 分類の規則
1. 上記の分類ガイドで判断できない品目は、category を couple にした上で
   その品目の uncertain を true にしてください（後で人が確認・修正します）。
   ガイドどおりに分類できた品目の uncertain は false にしてください。
2. 値引き行・割引行（「値引」「割引」「セール」等）は、金額を負数にした上で、
   対象品目と同じカテゴリにしてください。対象品目が不明なら couple にしてください。
3. ポイント利用行（「ポイント利用」「ポイント値引」等）は金額を負数にし、
   category は excluded にしてください。
4. 消費税行・小計行・合計行・預り金・釣銭は品目に含めないでください。
   税込価格が品目行に併記されている場合は税込価格を採用してください。

# 分類例（品目名 → category / uncertain）
以下は表記と分類の対応例です。同じ考え方で分類してください。
- ｵﾑﾂ M 62枚 → child
- ｷｯｽﾞ ﾊﾟｰｶｰ → child
- ﾘﾆｭｳｼｮｸ ｶﾎﾞﾁｬ → child
- ｷﾞｭｳﾆｭｳ → couple
- ﾋﾞｰﾙ 350ml×6 → couple（酒は食費）
- ｷｯﾁﾝﾍﾟｰﾊﾟｰ → couple
- ﾌﾞﾝｺ → excluded（本は個人の買い物）
- ﾒﾝｽﾞ ｼｬﾂ → excluded（大人の服は個人の買い物）
- ﾎﾟｲﾝﾄ ﾘﾖｳ → excluded（金額は負数）
- ｶﾞﾄｳﾁｮｺ → couple, uncertain=true（大人用か子ども用か判断できない）
- ﾚﾄﾙﾄ ｶﾚｰ ｱﾏｸﾁ → couple, uncertain=true（子ども向けの可能性があるが確定できない）

# 抽出の規則
- 品目名は半角カナ略称（例: ｷｯｽﾞｼｬﾂ）でもそのまま転記してください。
  意味が明確な場合のみ自然な日本語に直してもよい（例: ｷｯｽﾞｼｬﾂ → キッズシャツ）。
- price は日本円の整数（税込）。値引き・ポイント行のみ負数を許可します。
- date はレシート記載の購入日を YYYY-MM-DD 形式で。読み取れない場合は空文字にしてください。
- total はレシート記載の「合計」（税込・値引き後）を転記してください。
- store は店舗ブランド名を短く（例: 「西松屋」「ライフ」）。"""

# Functional design §5.3, including per-item uncertainty.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "store": {"type": "STRING"},
        "date": {
            "type": "STRING",
            "description": "YYYY-MM-DD。読み取れない場合は空文字",
        },
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "price": {
                        "type": "INTEGER",
                        "description": "日本円。値引き・ポイント行は負数",
                    },
                    "category": {
                        "type": "STRING",
                        "enum": ["child", "couple", "excluded"],
                    },
                    "uncertain": {
                        "type": "BOOLEAN",
                        "description": "分類ガイドで判断できなかった場合のみ true",
                    },
                },
                "required": ["name", "price", "category", "uncertain"],
            },
        },
        "total": {"type": "INTEGER"},
    },
    "required": ["store", "date", "items", "total"],
}


class GeminiParseError(RuntimeError):
    """Upstream parsing failed; the HTTP layer maps this to 502."""

    def __init__(self) -> None:
        super().__init__("gemini_parse_failed")


class GeminiService:
    """Use an injected, caller-owned HTTP client for connection reuse and testing."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime] = now_jst,
    ) -> None:
        self.api_key = api_key
        self.client = client
        self.clock = clock

    def parse_receipt(
        self, image: bytes, *, content_type: str = JPEG_CONTENT_TYPE
    ) -> ReceiptDraft:
        request_body = {
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {
                            "inline_data": {
                                "mime_type": content_type,
                                "data": base64.b64encode(image).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.client.post(
                    GENERATE_CONTENT_URL,
                    params={"key": self.api_key},
                    json=request_body,
                    timeout=TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
                response.raise_for_status()
                envelope = response.json()
                parts = envelope["candidates"][0]["content"]["parts"]
                raw = json.loads("".join(part["text"] for part in parts))
                return self._build_draft(raw)
            except (
                httpx.TimeoutException,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as error:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GeminiParseError() from error
            except httpx.HTTPStatusError as error:
                if not error.response.is_server_error or attempt == MAX_ATTEMPTS - 1:
                    raise GeminiParseError() from error
            except (
                httpx.RequestError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError,
            ) as error:
                raise GeminiParseError() from error
            time.sleep(RETRY_DELAY_SECONDS)
        raise GeminiParseError()  # Defensive: every final attempt exits above.

    def _build_draft(self, raw: Any) -> ReceiptDraft:
        """Keep semantic problems editable despite the strict draft contract.

        Unknown amounts become zero, unknown categories become uncertain couple,
        and invalid dates become JST today. Warnings retain invalid source values;
        these placeholders must be reviewed and are never persisted here.
        """
        if isinstance(raw, dict):
            raw = dict(raw)
            raw_date = raw.get("date")
            raw["date"] = "" if raw_date is None else str(raw_date)
        parsed = GeminiReceipt.model_validate(raw)
        warnings: list[str] = []
        try:
            receipt_date = validate_receipt_date(parsed.date)
        except ValueError:
            receipt_date = self.clock().astimezone(JST).date()
            warnings.append(
                f"日付を読み取れませんでした（{parsed.date!r}）。今日の日付 {receipt_date.isoformat()} で補完しました。確認してください"
            )

        items: list[ParsedItem] = []
        for number, item in enumerate(parsed.items, start=FIRST_ITEM_NUMBER):
            uncertain = item.uncertain
            price = self._amount(
                item.price, f"品目 {number}（{item.name}）の金額", warnings
            )
            if type(item.price) is not int:
                uncertain = True
            try:
                category = Category(item.category)
            except ValueError:
                category = Category.COUPLE
                uncertain = True
                warnings.append(
                    f"品目 {number} のカテゴリ {item.category!r} を判定できません。夫婦生活費として確認してください"
                )
            items.append(
                ParsedItem(
                    name=item.name, price=price, category=category, uncertain=uncertain
                )
            )

        total = self._amount(parsed.total, "レシート合計", warnings)
        item_total = sum(item.price for item in items)
        if item_total != total:
            warning = (
                f"品目合計 {item_total}円 がレシート記載合計 {total}円 と一致しません"
            )
            for rate in TAX_RATES:
                if abs(round(item_total * rate) - total) <= TAX_TOLERANCE_YEN:
                    warning += f"（品目が税抜表示の可能性: ×{rate:.2f} で一致）"
                    break
            warnings.append(warning)
        return ReceiptDraft(
            store=parsed.store,
            date=receipt_date,
            items=items,
            total=total,
            warnings=warnings,
        )

    @staticmethod
    def _amount(value: object, label: str, warnings: list[str]) -> int:
        if type(value) is int:
            return value
        warnings.append(
            f"{label} {value!r} は整数ではありません。0円で補完しました。修正してください"
        )
        return UNKNOWN_AMOUNT
