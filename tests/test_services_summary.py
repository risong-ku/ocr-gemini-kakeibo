"""Summary and CSV use scanned LineItems, never META totals for aggregation."""

import codecs
import csv
import io
from datetime import date
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kakeibo.models import ConfirmedItem, Receipt
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.services.summary import SummaryService, encode_csv

FIRST_ID = "01J5ZAAAAAAAAAAAAAAAAAAAAA"
SECOND_ID = "01J5ZC8YV3Q4R6T8W9XABCDEF0"
THIRD_ID = "01J5ZC8YV3Q4R6T8W9XABCDEF1"
PRIOR_ID = "01J5ZC8YV3Q4R6T8W9XABCDEF2"
COLUMNS = ["receipt_id", "date", "store", "item", "price", "category"]


@pytest.fixture
def summary_service(dynamo_repo: DynamoRepository) -> SummaryService:
    return SummaryService(dynamo_repo)


@pytest.fixture
def monthly_receipts(
    dynamo_repo: DynamoRepository, stored_receipt: Receipt
) -> list[Receipt]:
    receipts: list[Receipt] = []
    scenarios = [
        (
            FIRST_ID,
            "2026-08-25",
            [
                ("おむつ", 500, "child"),
                ("値引き", -100, "child"),
                ("パン", 200, "couple"),
                ("ポイント", -50, "excluded"),
            ],
        ),
        (SECOND_ID, "2026-08-25", [("牛乳", 300, "couple")]),
        (THIRD_ID, "2026-08-26", [("本", 900, "excluded")]),
        (PRIOR_ID, "2026-07-31", [("過去", 9999, "child")]),
    ]
    # Insertion order and ULID order deliberately differ from purchase-date order.
    for receipt_id, purchased, entries in reversed(scenarios):
        receipt = stored_receipt.model_copy(
            update={
                "receipt_id": receipt_id,
                "date": date.fromisoformat(purchased),
                "client_token": uuid4(),
                "total": 12345,
            }
        )
        dynamo_repo.save_receipt(
            receipt,
            [
                ConfirmedItem(name=name, price=price, category=category)
                for name, price, category in entries
            ],
        )
        receipts.append(receipt)
    return receipts


def test_summary_filters_month_preserves_discounts_and_sorts_receipts(
    summary_service: SummaryService,
    monthly_receipts: list[Receipt],
    dynamo_repo: DynamoRepository,
) -> None:
    table = dynamo_repo.table
    with (
        patch.object(table, "scan", wraps=table.scan) as scan,
        patch.object(
            table, "query", side_effect=AssertionError("must reuse scanned data")
        ),
    ):
        result = summary_service.get_summary("2026-08").model_dump(mode="json")
    scan.assert_called_once()
    assert result["month"] == "2026-08"
    assert result["daily"] == [
        {"date": "2026-08-25", "child": 400, "couple": 500},
        {"date": "2026-08-26", "child": 0, "couple": 0},
    ]
    assert result["totals"] == {"child": 400, "couple": 500}
    assert result["receipt_count"] == 3
    assert [receipt["receipt_id"] for receipt in result["receipts"]] == [
        THIRD_ID,
        SECOND_ID,
        FIRST_ID,
    ]
    assert result["receipts"][2] == {
        "receipt_id": FIRST_ID,
        "date": "2026-08-25",
        "store": "西松屋",
        "total": 12345,
        "subtotals": {"child": 400, "couple": 200, "excluded": -50},
        "items": [
            {"seq": 1, "name": "おむつ", "price": 500, "category": "child"},
            {"seq": 2, "name": "値引き", "price": -100, "category": "child"},
            {"seq": 3, "name": "パン", "price": 200, "category": "couple"},
            {"seq": 4, "name": "ポイント", "price": -50, "category": "excluded"},
        ],
    }


def test_summary_subtotals_include_all_excluded_receipt(
    summary_service: SummaryService, monthly_receipts: list[Receipt]
) -> None:
    result = summary_service.get_summary("2026-08")
    assert result.receipts[0].receipt_id == THIRD_ID
    assert result.receipts[0].subtotals.model_dump() == {
        "child": 0,
        "couple": 0,
        "excluded": 900,
    }
    assert result.receipts[1].subtotals.model_dump() == {
        "child": 0,
        "couple": 300,
        "excluded": 0,
    }
    assert result.totals.model_dump() == {"child": 400, "couple": 500}


@pytest.mark.parametrize("month", ["2026-01", "2026-12", "2024-02"])
def test_summary_empty_month_returns_zero_and_empty_collections(
    summary_service: SummaryService, monthly_receipts: list[Receipt], month: str
) -> None:
    assert summary_service.get_summary(month).model_dump(mode="json") == {
        "month": month,
        "daily": [],
        "totals": {"child": 0, "couple": 0},
        "receipt_count": 0,
        "receipts": [],
    }


def test_summary_empty_table(summary_service: SummaryService) -> None:
    assert summary_service.get_summary("2026-08").receipt_count == 0
    assert summary_service.csv_rows("all") == []
    assert (
        encode_csv([]).decode("utf-8-sig")
        == "receipt_id,date,store,item,price,category\r\n"
    )


@pytest.mark.parametrize(
    "month", ["2026-00", "2026-13", "2026-8", "26-08", "2026-01\n", "", "all"]
)
def test_summary_invalid_month_fails_before_scan(
    summary_service: SummaryService, dynamo_repo: DynamoRepository, month: str
) -> None:
    with patch.object(dynamo_repo.table, "scan") as scan:
        with pytest.raises(ValidationError):
            summary_service.get_summary(month)
    scan.assert_not_called()


@pytest.mark.parametrize("month", ["2026-00", "2026-13", "2026-8", "ALL", ""])
def test_csv_invalid_month_fails_before_scan(
    summary_service: SummaryService, dynamo_repo: DynamoRepository, month: str
) -> None:
    with patch.object(dynamo_repo.table, "scan") as scan:
        with pytest.raises(ValidationError):
            summary_service.csv_rows(month)
    scan.assert_not_called()


def test_csv_month_and_all_sort_by_date_id_and_sequence(
    summary_service: SummaryService, monthly_receipts: list[Receipt]
) -> None:
    rows = summary_service.csv_rows("2026-08")
    assert rows == [
        {
            "receipt_id": FIRST_ID,
            "date": "2026-08-25",
            "store": "西松屋",
            "item": "おむつ",
            "price": 500,
            "category": "child",
        },
        {
            "receipt_id": FIRST_ID,
            "date": "2026-08-25",
            "store": "西松屋",
            "item": "値引き",
            "price": -100,
            "category": "child",
        },
        {
            "receipt_id": FIRST_ID,
            "date": "2026-08-25",
            "store": "西松屋",
            "item": "パン",
            "price": 200,
            "category": "couple",
        },
        {
            "receipt_id": FIRST_ID,
            "date": "2026-08-25",
            "store": "西松屋",
            "item": "ポイント",
            "price": -50,
            "category": "excluded",
        },
        {
            "receipt_id": SECOND_ID,
            "date": "2026-08-25",
            "store": "西松屋",
            "item": "牛乳",
            "price": 300,
            "category": "couple",
        },
        {
            "receipt_id": THIRD_ID,
            "date": "2026-08-26",
            "store": "西松屋",
            "item": "本",
            "price": 900,
            "category": "excluded",
        },
    ]
    all_rows = summary_service.csv_rows("all")
    assert all_rows[0]["receipt_id"] == PRIOR_ID
    assert all_rows[1:] == rows
    assert summary_service.csv_rows("2026-09") == []


def test_csv_uses_lineitem_date_store_without_meta_and_escapes_utf8(
    summary_service: SummaryService,
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
) -> None:
    dynamo_repo.save_receipt(
        stored_receipt,
        [ConfirmedItem(name='おむつ,"M"\n特価', price=-123, category="excluded")],
    )
    table = dynamo_repo.table
    table.delete_item(Key={"PK": f"RECEIPT#{stored_receipt.receipt_id}", "SK": "META"})
    table.update_item(
        Key={"PK": f"RECEIPT#{stored_receipt.receipt_id}", "SK": "ITEM#001"},
        UpdateExpression="SET #s = :s, #d = :d",
        ExpressionAttributeNames={"#s": "store", "#d": "date"},
        ExpressionAttributeValues={":s": '店,"支店"', ":d": "2024-02-29"},
    )
    with patch.object(
        table, "query", side_effect=AssertionError("CSV must not query META")
    ):
        rows = summary_service.csv_rows("2024-02")
    output = encode_csv(rows)
    assert output.startswith(codecs.BOM_UTF8)
    assert output.count(codecs.BOM_UTF8) == 1
    reader = csv.DictReader(io.StringIO(output.decode("utf-8-sig"), newline=""))
    assert reader.fieldnames == COLUMNS
    assert list(reader) == [
        {
            "receipt_id": stored_receipt.receipt_id,
            "date": "2024-02-29",
            "store": '店,"支店"',
            "item": 'おむつ,"M"\n特価',
            "price": "-123",
            "category": "excluded",
        }
    ]


def test_summary_item_order_does_not_depend_on_scan_order(
    summary_service: SummaryService,
    monthly_receipts: list[Receipt],
    dynamo_repo: DynamoRepository,
) -> None:
    table = dynamo_repo.table
    original = table.scan

    def reverse_scan(**kwargs: Any) -> dict[str, Any]:
        page = original(**kwargs)
        page["Items"].reverse()
        return page

    with patch.object(table, "scan", side_effect=reverse_scan):
        result = summary_service.get_summary("2026-08")
        rows = summary_service.csv_rows("2026-08")
    assert [item.seq for item in result.receipts[-1].items] == [1, 2, 3, 4]
    assert [row["item"] for row in rows[:4]] == ["おむつ", "値引き", "パン", "ポイント"]
