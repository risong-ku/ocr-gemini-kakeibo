"""Validation and serialization of the documented API contracts."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from kakeibo.models import (
    ConfirmedItem,
    ExportMonth,
    GeminiReceipt,
    LineItem,
    Month,
    Receipt,
    ReceiptCreated,
    ReceiptDeleted,
    ReceiptDraft,
    SaveReceipt,
    SummaryResponse,
)


@pytest.mark.parametrize(
    "count,valid", [(0, False), (1, True), (999, True), (1000, False)]
)
def test_save_receipt_item_count_boundary(
    receipt_payload: dict[str, Any], count: int, valid: bool
) -> None:
    receipt_payload["items"] = [receipt_payload["items"][0]] * count

    if not valid:
        with pytest.raises(ValidationError):
            SaveReceipt.model_validate(receipt_payload)
        return

    result = SaveReceipt.model_validate(receipt_payload)

    assert len(result.items) == count


@pytest.mark.parametrize("category", ["child", "couple", "excluded"])
def test_confirmed_item_negative_integer_and_category_are_preserved(
    category: str,
) -> None:
    item = ConfirmedItem(name="値引き", price=-100, category=category)

    assert item.model_dump() == {"name": "値引き", "price": -100, "category": category}


@pytest.mark.parametrize("value", ["1480", 1480.0, 1.5, True, None])
@pytest.mark.parametrize("field", ["price", "total"])
def test_save_receipt_noninteger_amount_is_rejected(
    receipt_payload: dict[str, Any], field: str, value: Any
) -> None:
    target = receipt_payload["items"][0] if field == "price" else receipt_payload
    target[field] = value

    with pytest.raises(ValidationError):
        SaveReceipt.model_validate(receipt_payload)


@pytest.mark.parametrize(
    "value", ["2026-8-25", "2026-02-29", "2026-13-01", "2026-08-25T00:00:00", "", 0]
)
def test_save_receipt_invalid_date_is_rejected(
    receipt_payload: dict[str, Any], value: Any
) -> None:
    receipt_payload["date"] = value

    with pytest.raises(ValidationError):
        SaveReceipt.model_validate(receipt_payload)


def test_save_receipt_empty_store_and_mismatched_total_are_preserved(
    receipt_payload: dict[str, Any],
) -> None:
    receipt_payload.update(store="", date="2024-02-29")

    result = SaveReceipt.model_validate(receipt_payload)

    assert result.store == ""
    assert result.date == date(2024, 2, 29)
    assert result.total == 1738
    assert sum(item.price for item in result.items) != result.total
    assert result.model_dump(mode="json") == receipt_payload


@pytest.mark.parametrize(
    "value", [None, "invalid", "6f1d2c1e-6a5b-1e2f-9c3a-8b7d5e4f1a20"]
)
def test_save_receipt_invalid_client_token_is_rejected(
    receipt_payload: dict[str, Any], value: Any
) -> None:
    receipt_payload["client_token"] = value

    with pytest.raises(ValidationError):
        SaveReceipt.model_validate(receipt_payload)


def test_save_receipt_missing_client_token_is_rejected(
    receipt_payload: dict[str, Any],
) -> None:
    del receipt_payload["client_token"]

    with pytest.raises(ValidationError):
        SaveReceipt.model_validate(receipt_payload)


@pytest.mark.parametrize("extra", [{"uncertain": True}, {"category": "other"}])
def test_save_receipt_invalid_item_is_rejected(
    receipt_payload: dict[str, Any], extra: dict[str, Any]
) -> None:
    receipt_payload["items"][0].update(extra)

    with pytest.raises(ValidationError):
        SaveReceipt.model_validate(receipt_payload)


def test_gemini_and_draft_are_separate_from_save_contract() -> None:
    raw = GeminiReceipt(store="", date="", total=0, items=[])
    draft = ReceiptDraft(
        store="",
        date="2026-08-25",
        total=100,
        warnings=["要確認"],
        items=[{"name": "", "price": 0, "category": "couple", "uncertain": True}],
    )

    assert raw.date == ""
    assert raw.items == []
    assert draft.items[0].uncertain is True
    assert set(draft.model_dump()) == {"store", "date", "total", "items", "warnings"}


def test_stored_receipt_and_item_preserve_identity_and_jst(
    receipt_payload: dict[str, Any],
) -> None:
    receipt_id = "01J5ZC8YV3Q4R6T8W9XABCDEF0"
    receipt = Receipt(
        receipt_id=receipt_id,
        **{key: value for key, value in receipt_payload.items() if key != "items"},
        s3_key=f"receipts/2026/08/{receipt_id}.jpg",
        created_at=datetime(2026, 8, 24, 16, tzinfo=UTC),
    )
    item = LineItem(
        receipt_id=receipt_id,
        seq=999,
        date=receipt.date,
        store=receipt.store,
        **receipt_payload["items"][0],
    )

    assert receipt.created_at.isoformat() == "2026-08-25T01:00:00+09:00"
    assert Receipt(
        **receipt.model_dump(exclude={"created_at"})
    ).created_at.utcoffset() == timedelta(hours=9)
    assert item.date == receipt.date
    assert item.store == receipt.store
    assert "uncertain" not in item.model_dump()
    with pytest.raises(ValidationError):
        Receipt(**{**receipt.model_dump(), "created_at": datetime(2026, 8, 25)})


@pytest.mark.parametrize("seq", [0, 1000, "1", True])
def test_line_item_invalid_sequence_is_rejected(seq: Any) -> None:
    with pytest.raises(ValidationError):
        LineItem(
            receipt_id="01J5ZC8YV3Q4R6T8W9XABCDEF0",
            seq=seq,
            date="2026-08-25",
            store="",
            name="",
            price=0,
            category="couple",
        )


@pytest.mark.parametrize("value", ["2026-8", "2026-00", "2026-13", "2026-08\n", "all"])
def test_month_invalid_value_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Month).validate_python(value)


def test_summary_and_id_responses_match_documented_shapes() -> None:
    receipt_id = "01J5ZC8YV3Q4R6T8W9XABCDEF0"
    payload = {
        "month": "2026-08",
        "daily": [{"date": "2026-08-25", "child": 0, "couple": -100}],
        "totals": {"child": 0, "couple": -100},
        "receipt_count": 1,
        "receipts": [
            {
                "receipt_id": receipt_id,
                "date": "2026-08-25",
                "store": "",
                "total": -100,
                "subtotals": {"child": 0, "couple": -100, "excluded": 0},
                "items": [
                    {"seq": 1, "name": "値引き", "price": -100, "category": "couple"}
                ],
            }
        ],
    }

    summary = SummaryResponse.model_validate(payload)

    assert summary.model_dump(mode="json") == payload
    assert summary.receipts[0].items[0].seq == 1
    assert ReceiptCreated(receipt_id=receipt_id).model_dump() == {
        "receipt_id": receipt_id
    }
    assert ReceiptDeleted(deleted=receipt_id).model_dump() == {"deleted": receipt_id}
    assert TypeAdapter(ExportMonth).validate_python("all") == "all"
    assert TypeAdapter(Month).validate_python("2026-12") == "2026-12"
    payload["totals"]["excluded"] = 0
    with pytest.raises(ValidationError):
        SummaryResponse.model_validate(payload)
