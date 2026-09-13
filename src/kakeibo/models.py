"""Receipt entities and API contracts, independent of HTTP and storage clients."""

import re
from datetime import date as Date
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    UUID4,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

MIN_ITEMS = 1
MAX_ITEMS = 999
JST = ZoneInfo("Asia/Tokyo")
DATE_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"


def validate_receipt_date(value: object) -> Date:
    """Reject timestamps and coercions while accepting internal date objects."""
    if type(value) is Date:
        return value
    if isinstance(value, str) and re.fullmatch(DATE_PATTERN, value):
        return Date.fromisoformat(value)
    raise ValueError("date must be YYYY-MM-DD")


def now_jst() -> datetime:
    return datetime.now(JST)


ReceiptDate = Annotated[Date, BeforeValidator(validate_receipt_date)]
Month = Annotated[str, StringConstraints(pattern=MONTH_PATTERN)]
ExportMonth = Month | Literal["all"]
ReceiptId = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]
Sequence = Annotated[StrictInt, Field(ge=MIN_ITEMS, le=MAX_ITEMS)]


class Category(StrEnum):
    CHILD = "child"
    COUPLE = "couple"
    EXCLUDED = "excluded"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmedItem(Contract):
    name: str
    price: StrictInt
    category: Category


class ParsedItem(ConfirmedItem):
    uncertain: bool


class GeminiItem(Contract):
    """Keep semantic errors intact for the parsing service's warning policy."""

    name: str
    price: Any
    category: str
    uncertain: bool


class GeminiReceipt(Contract):
    store: str
    date: str
    items: list[GeminiItem]
    total: Any


class ReceiptDraft(Contract):
    store: str
    date: ReceiptDate
    items: list[ParsedItem]
    total: StrictInt
    warnings: list[str] = Field(default_factory=list)


class SaveReceipt(Contract):
    client_token: UUID4
    store: str
    date: ReceiptDate
    total: StrictInt
    items: list[ConfirmedItem] = Field(min_length=MIN_ITEMS, max_length=MAX_ITEMS)


class Receipt(Contract):
    """META entity; the confirmed total is deliberately never recalculated."""

    receipt_id: ReceiptId
    client_token: UUID4
    date: ReceiptDate
    store: str
    total: StrictInt
    s3_key: str | None = None
    created_at: datetime = Field(default_factory=now_jst)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(JST)


class LineItem(ConfirmedItem):
    receipt_id: ReceiptId
    seq: Sequence
    date: ReceiptDate
    store: str


class ReceiptCreated(Contract):
    receipt_id: ReceiptId


class ReceiptDeleted(Contract):
    deleted: ReceiptId


class CategoryTotals(Contract):
    child: StrictInt
    couple: StrictInt


class DailyTotals(CategoryTotals):
    date: ReceiptDate


class ReceiptSubtotals(CategoryTotals):
    excluded: StrictInt


class SummaryItem(ConfirmedItem):
    seq: Sequence


class SummaryReceipt(Contract):
    receipt_id: ReceiptId
    date: ReceiptDate
    store: str
    total: StrictInt
    subtotals: ReceiptSubtotals
    items: list[SummaryItem]


class SummaryResponse(Contract):
    month: Month
    daily: list[DailyTotals]
    totals: CategoryTotals
    receipt_count: Annotated[StrictInt, Field(ge=0)]
    receipts: list[SummaryReceipt]
