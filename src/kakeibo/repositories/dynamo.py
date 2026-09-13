"""Single-table receipt persistence with bounded batch retries and rollback."""

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config

from kakeibo.config import DEFAULT_AWS_REGION
from kakeibo.models import ConfirmedItem, LineItem, Receipt, SaveReceipt

PARTITION_PREFIX = "RECEIPT#"
META_KEY = "META"
ITEM_PREFIX = "ITEM#"
SEQUENCE_WIDTH = 3
FIRST_SEQUENCE = 1
BATCH_SIZE = 25
MAX_BATCH_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.05
RETRY_MULTIPLIER = 2
AWS_CONNECT_TIMEOUT_SECONDS = 2
AWS_READ_TIMEOUT_SECONDS = 5
AWS_MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)

Entity = Receipt | LineItem
DynamoItem = dict[str, Any]


class UnprocessedItemsError(RuntimeError):
    """DynamoDB still has unprocessed operations after bounded retries."""


class DynamoRepository:
    def __init__(self, table_name: str, region_name: str = DEFAULT_AWS_REGION) -> None:
        resource = boto3.resource(
            "dynamodb",
            region_name=region_name,
            config=Config(
                connect_timeout=AWS_CONNECT_TIMEOUT_SECONDS,
                read_timeout=AWS_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": AWS_MAX_ATTEMPTS},
            ),
        )
        self.table = resource.Table(table_name)

    def save_receipt(self, receipt: Receipt, items: Sequence[ConfirmedItem]) -> None:
        """Write a new receipt; services must check client_token before calling.

        On any write failure, query and delete the entire new partition, including
        writes whose response was lost. Cleanup failure must not mask the cause.
        S3 compensation belongs to the calling service.
        """
        confirmed = SaveReceipt(
            **receipt.model_dump(exclude={"receipt_id", "s3_key", "created_at"}),
            items=list(items),
        )
        pk = PARTITION_PREFIX + receipt.receipt_id
        meta = receipt.model_dump(
            mode="json", exclude={"receipt_id"}, exclude_none=True
        )
        rows = [{"PK": pk, "SK": META_KEY, **meta}]
        rows.extend(
            {
                "PK": pk,
                "SK": f"{ITEM_PREFIX}{seq:0{SEQUENCE_WIDTH}d}",
                "date": receipt.date.isoformat(),
                "store": receipt.store,
                **item.model_dump(mode="json"),
            }
            for seq, item in enumerate(confirmed.items, start=FIRST_SEQUENCE)
        )
        try:
            self._batch_write([{"PutRequest": {"Item": row}} for row in rows])
        except Exception:
            try:
                self.delete_receipt(receipt.receipt_id)
            except Exception:
                logger.exception("Receipt cleanup failed for %s", pk)
            raise

    def query_receipt(self, receipt_id: str) -> list[Entity]:
        """Return ITEMs in sequence order and META last (DynamoDB SK order)."""
        return [self._decode(row) for row in self._query_rows(receipt_id)]

    def scan(self) -> list[Entity]:
        """Read every page for downstream aggregation and CSV preparation."""
        return [self._decode(row) for row in self._pages(self.table.scan)]

    def find_by_client_token(self, client_token: UUID | str) -> Receipt | None:
        """Find an existing META, including after empty filtered scan pages."""
        rows = self._pages(
            self.table.scan,
            FilterExpression=Attr("SK").eq(META_KEY)
            & Attr("client_token").eq(str(client_token)),
            ConsistentRead=True,
        )
        for row in rows:
            entity = self._decode(row)
            if isinstance(entity, Receipt):
                return entity
        return None

    def delete_receipt(self, receipt_id: str) -> bool:
        """Delete only this partition; normal deletion retains its S3 image."""
        rows = list(self._query_rows(receipt_id))
        if not rows:
            return False
        self._batch_write(
            [
                {"DeleteRequest": {"Key": {"PK": row["PK"], "SK": row["SK"]}}}
                for row in rows
            ]
        )
        return True

    def _query_rows(self, receipt_id: str) -> Iterator[DynamoItem]:
        return self._pages(
            self.table.query,
            KeyConditionExpression=Key("PK").eq(PARTITION_PREFIX + receipt_id),
            ConsistentRead=True,
        )

    @staticmethod
    def _pages(
        operation: Callable[..., dict[str, Any]], **kwargs: Any
    ) -> Iterator[DynamoItem]:
        while True:
            page = operation(**kwargs)
            yield from page.get("Items", [])
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs["ExclusiveStartKey"] = last_key

    def _batch_write(self, requests: list[DynamoItem]) -> None:
        for offset in range(0, len(requests), BATCH_SIZE):
            pending = requests[offset : offset + BATCH_SIZE]
            for attempt in range(MAX_BATCH_ATTEMPTS):
                response = self.table.meta.client.batch_write_item(
                    RequestItems={self.table.name: pending}
                )
                pending = response.get("UnprocessedItems", {}).get(self.table.name, [])
                if not pending:
                    break
                if attempt == MAX_BATCH_ATTEMPTS - 1:
                    raise UnprocessedItemsError("DynamoDB batch retry limit exceeded")
                time.sleep(RETRY_BASE_SECONDS * RETRY_MULTIPLIER**attempt)

    @staticmethod
    def _decode(row: DynamoItem) -> Entity:
        fields = dict(row)
        fields["receipt_id"] = fields.pop("PK").removeprefix(PARTITION_PREFIX)
        sk = fields.pop("SK")
        # boto3 returns Decimal for all DynamoDB numbers. Convert only integral
        # values so corrupt fractional amounts cannot silently lose precision.
        amount_field = "total" if sk == META_KEY else "price"
        amount = fields.get(amount_field)
        if isinstance(amount, Decimal) and amount == amount.to_integral_value():
            fields[amount_field] = int(amount)
        if sk == META_KEY:
            return Receipt.model_validate(fields)
        fields["seq"] = int(sk.removeprefix(ITEM_PREFIX))
        return LineItem.model_validate(fields)
