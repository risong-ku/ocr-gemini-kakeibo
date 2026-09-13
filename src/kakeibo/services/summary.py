"""Scan-based monthly summaries and BOM UTF-8 CSV preparation."""

import csv
import io
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import TypedDict

from pydantic import TypeAdapter

from kakeibo.models import (
    Category,
    CategoryTotals,
    DailyTotals,
    ExportMonth,
    LineItem,
    Month,
    Receipt,
    ReceiptSubtotals,
    SummaryItem,
    SummaryReceipt,
    SummaryResponse,
)
from kakeibo.repositories.dynamo import DynamoRepository

ALL_MONTHS = "all"
CSV_COLUMNS = ("receipt_id", "date", "store", "item", "price", "category")
CSV_ENCODING = "utf-8-sig"
MONTH_ADAPTER = TypeAdapter(Month)
EXPORT_MONTH_ADAPTER = TypeAdapter(ExportMonth)


class CsvRow(TypedDict):
    receipt_id: str
    date: str
    store: str
    item: str
    price: int
    category: str


class SummaryService:
    def __init__(self, dynamo: DynamoRepository) -> None:
        self.dynamo = dynamo

    def get_summary(self, month: str) -> SummaryResponse:
        month = MONTH_ADAPTER.validate_python(month)
        entities = self.dynamo.scan()
        daily: dict[date, CategoryTotals] = {}
        totals = CategoryTotals(child=0, couple=0)
        grouped: dict[str, list[LineItem]] = defaultdict(list)
        receipts: list[Receipt] = []
        for entity in entities:
            if not _matches_month(entity.date, month):
                continue
            if isinstance(entity, Receipt):
                receipts.append(entity)
                continue
            grouped[entity.receipt_id].append(entity)
            day = daily.setdefault(entity.date, CategoryTotals(child=0, couple=0))
            if entity.category == Category.CHILD:
                day.child += entity.price
                totals.child += entity.price
            elif entity.category == Category.COUPLE:
                day.couple += entity.price
                totals.couple += entity.price

        receipts.sort(
            key=lambda receipt: (receipt.date, receipt.receipt_id), reverse=True
        )
        return SummaryResponse(
            month=month,
            daily=[
                DailyTotals(date=day, **daily[day].model_dump())
                for day in sorted(daily)
            ],
            totals=totals,
            receipt_count=len(receipts),
            receipts=[
                SummaryReceipt(
                    receipt_id=receipt.receipt_id,
                    date=receipt.date,
                    store=receipt.store,
                    total=receipt.total,
                    subtotals=ReceiptSubtotals(
                        **{
                            category.value: sum(
                                item.price
                                for item in grouped[receipt.receipt_id]
                                if item.category == category
                            )
                            for category in Category
                        }
                    ),
                    items=[
                        SummaryItem(
                            seq=item.seq,
                            name=item.name,
                            price=item.price,
                            category=item.category,
                        )
                        for item in sorted(
                            grouped[receipt.receipt_id], key=lambda item: item.seq
                        )
                    ],
                )
                for receipt in receipts
            ],
        )

    def csv_rows(self, month: str) -> list[CsvRow]:
        """Use only denormalized LineItems; META is unnecessary for export."""
        month = EXPORT_MONTH_ADAPTER.validate_python(month)
        items = [
            entity
            for entity in self.dynamo.scan()
            if isinstance(entity, LineItem) and _matches_month(entity.date, month)
        ]
        items.sort(key=lambda item: (item.date, item.receipt_id, item.seq))
        return [
            CsvRow(
                receipt_id=item.receipt_id,
                date=item.date.isoformat(),
                store=item.store,
                item=item.name,
                price=item.price,
                category=item.category.value,
            )
            for item in items
        ]


def _matches_month(purchased: date, month: str) -> bool:
    if month == ALL_MONTHS:
        return True
    return f"{purchased.year:04d}-{purchased.month:02d}" == month


def encode_csv(rows: Iterable[CsvRow]) -> bytes:
    """Include a header even for empty exports; csv handles quotes and newlines."""
    with io.StringIO(newline="") as buffer:
        writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue().encode(CSV_ENCODING)
