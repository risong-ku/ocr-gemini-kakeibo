"""Offline Gemini contracts exercised through httpx's transport boundary."""

import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from kakeibo.services.gemini import (
    PROMPT,
    RESPONSE_SCHEMA,
    GeminiParseError,
    GeminiService,
)

IMAGE = b"\xff\xd8receipt\xff\xd9"
API_KEY = "test-key"


@pytest.fixture
def gemini_payload() -> dict[str, Any]:
    return {
        "store": "西松屋",
        "date": "2026-08-25",
        "items": [
            {"name": "おむつ", "price": 1480, "category": "child", "uncertain": False},
            {"name": "ﾊﾝｶﾁ", "price": 258, "category": "couple", "uncertain": True},
            {
                "name": "ポイント利用",
                "price": -100,
                "category": "excluded",
                "uncertain": False,
            },
        ],
        "total": 1638,
    }


def response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
    )


def test_parse_receipt_sends_spec_request_and_preserves_draft(
    gemini_payload: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(gemini_payload)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        draft = GeminiService(API_KEY, client).parse_receipt(IMAGE)

    assert draft.model_dump(mode="json") == {**gemini_payload, "warnings": []}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert (
        str(request.url)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=test-key"
    )
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 25
    )
    body = json.loads(request.content)
    assert body["contents"] == [
        {
            "parts": [
                {"text": PROMPT},
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(IMAGE).decode("ascii"),
                    }
                },
            ]
        }
    ]
    assert body["generationConfig"] == {
        "responseMimeType": "application/json",
        "responseSchema": RESPONSE_SCHEMA,
    }
    spec = (
        Path(__file__).resolve().parents[1] / "docs/02_functional_design.md"
    ).read_text()
    assert PROMPT == spec.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert RESPONSE_SCHEMA == json.loads(
        spec.split("### 5.3", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
    )


def test_parse_receipt_mismatch_warns_without_changing_total(
    gemini_payload: dict[str, Any],
) -> None:
    gemini_payload["total"] = 1738
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response(gemini_payload))
    ) as client:
        draft = GeminiService(API_KEY, client).parse_receipt(IMAGE)
    assert draft.total == 1738
    assert draft.warnings == [
        "品目合計 1638円 がレシート記載合計 1738円 と一致しません"
    ]


@pytest.mark.parametrize(
    "item_total,total,rate",
    [
        (4536, 4990, "1.10"),
        (4536, 4989, "1.10"),
        (4536, 4991, "1.10"),
        (1000, 1080, "1.08"),
        (1000, 1079, "1.08"),
        (1000, 1081, "1.08"),
        (4536, 4988, None),
        (4536, 4992, None),
        (1000, 1078, None),
        (1000, 1082, None),
        (4536, 4536, None),
    ],
)
def test_parse_receipt_tax_hint(
    gemini_payload: dict[str, Any], item_total: int, total: int, rate: str | None
) -> None:
    gemini_payload["items"] = [
        {"name": "食事", "price": item_total, "category": "couple", "uncertain": False}
    ]
    gemini_payload["total"] = total
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response(gemini_payload))
    ) as client:
        draft = GeminiService(API_KEY, client).parse_receipt(IMAGE)
    assert draft.total == total
    assert sum(item.price for item in draft.items) == item_total
    expected = f"品目合計 {item_total}円 がレシート記載合計 {total}円 と一致しません"
    if rate is not None:
        expected += f"（品目が税抜表示の可能性: ×{rate} で一致）"
    assert draft.warnings == ([expected] if item_total != total else [])


@pytest.mark.parametrize("raw_date", ["", None, "2026-02-30", "not-a-date", 123])
def test_parse_receipt_missing_or_invalid_date_uses_jst_and_warns(
    gemini_payload: dict[str, Any], raw_date: object
) -> None:
    gemini_payload["date"] = raw_date

    def clock() -> datetime:
        return datetime.fromisoformat("2026-08-31T16:00:00+00:00")

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response(gemini_payload))
    ) as client:
        draft = GeminiService(API_KEY, client, clock=clock).parse_receipt(IMAGE)
    assert draft.date.isoformat() == "2026-09-01"
    assert any("日付" in warning for warning in draft.warnings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "1480"),
        ("price", 1.5),
        ("price", True),
        ("total", None),
        ("category", "unknown"),
    ],
)
def test_parse_receipt_semantic_errors_return_editable_warned_draft(
    gemini_payload: dict[str, Any], field: str, value: object
) -> None:
    if field == "total":
        gemini_payload[field] = value
    else:
        gemini_payload["items"][0][field] = value
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: response(gemini_payload))
    ) as client:
        draft = GeminiService(API_KEY, client).parse_receipt(IMAGE)
    assert draft.warnings
    if field == "total":
        assert draft.total == 0
    elif field == "price":
        assert draft.items[0].price == 0
        assert draft.items[0].uncertain is True
    else:
        assert draft.items[0].category == "couple"
        assert draft.items[0].uncertain is True


@pytest.mark.parametrize(
    "failure", ["timeout", "5xx", "outer_json", "inner_json", "invalid_encoding"]
)
@pytest.mark.parametrize("recover", [True, False])
def test_parse_receipt_retries_only_once(
    gemini_payload: dict[str, Any], failure: str, recover: bool
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if recover and len(requests) == 2:
            return response(gemini_payload)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "5xx":
            return httpx.Response(503)
        if failure == "outer_json":
            return httpx.Response(200, text="not JSON")
        if failure == "invalid_encoding":
            return httpx.Response(200, content=b"\xff")
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "not JSON"}]}}]}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        patch("kakeibo.services.gemini.time.sleep") as sleep,
    ):
        service = GeminiService(API_KEY, client)
        if recover:
            assert service.parse_receipt(IMAGE).total == 1638
        else:
            with pytest.raises(GeminiParseError, match="gemini_parse_failed"):
                service.parse_receipt(IMAGE)
    assert len(requests) == 2
    sleep.assert_called_once_with(1)


@pytest.mark.parametrize(
    "failure", [400, 401, 403, 429, 302, "connection", "blocked", "missing_uncertain"]
)
def test_parse_receipt_nonretryable_failure_is_typed_and_immediate(
    gemini_payload: dict[str, Any], failure: int | str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure)
        if failure == "connection":
            raise httpx.ConnectError("connection failed", request=request)
        if failure == "blocked":
            return httpx.Response(200, json={"candidates": []})
        del gemini_payload["items"][0]["uncertain"]
        return response(gemini_payload)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        patch("kakeibo.services.gemini.time.sleep") as sleep,
    ):
        with pytest.raises(GeminiParseError, match="gemini_parse_failed"):
            GeminiService(API_KEY, client).parse_receipt(IMAGE)
    assert len(requests) == 1
    sleep.assert_not_called()
