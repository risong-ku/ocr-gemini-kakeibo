"""DynamoDB persistence using moto with fault injection at the AWS boundary."""

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from kakeibo.models import ConfirmedItem, LineItem, Receipt
from kakeibo.repositories.dynamo import DynamoRepository, UnprocessedItemsError

MAX_BATCH_SIZE = 25
LARGE_ITEM_COUNT = 999
PAGE_SIZE = 2


def test_save_receipt_writes_exact_entities(
    dynamo_repo: DynamoRepository,
    dynamo_table: Any,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    dynamo_repo.save_receipt(stored_receipt, confirmed_items)

    rows = sorted(dynamo_table.scan()["Items"], key=lambda row: row["SK"])
    expected_meta = stored_receipt.model_dump(mode="json", exclude={"receipt_id"})
    expected_meta.update(PK=f"RECEIPT#{stored_receipt.receipt_id}", SK="META")
    assert rows[-1] == expected_meta
    assert rows[:-1] == [
        {
            "PK": expected_meta["PK"],
            "SK": f"ITEM#{seq:03d}",
            "date": "2026-08-25",
            "store": "西松屋",
            **item.model_dump(mode="json"),
        }
        for seq, item in enumerate(confirmed_items, start=1)
    ]
    entities = dynamo_repo.query_receipt(stored_receipt.receipt_id)
    assert entities[-1] == stored_receipt
    assert [item.seq for item in entities if isinstance(item, LineItem)] == [1, 2, 3]
    assert entities[2].price == -100
    assert type(entities[2].price) is int
    assert type(entities[-1].total) is int


def test_save_receipt_999_items_chunks_all_entities(
    dynamo_repo: DynamoRepository,
    dynamo_table: Any,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    client = dynamo_repo.table.meta.client
    with patch.object(
        client, "batch_write_item", wraps=client.batch_write_item
    ) as batch:
        dynamo_repo.save_receipt(
            stored_receipt, [confirmed_items[0]] * LARGE_ITEM_COUNT
        )

    assert batch.call_count == 40
    assert all(
        len(call.kwargs["RequestItems"][dynamo_table.name]) <= MAX_BATCH_SIZE
        for call in batch.call_args_list
    )
    entities = dynamo_repo.query_receipt(stored_receipt.receipt_id)
    assert len(entities) == 1000
    assert [item.seq for item in entities if isinstance(item, LineItem)] == list(
        range(1, 1000)
    )


def test_query_scan_and_token_lookup_follow_filtered_empty_pages(
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    dynamo_repo.save_receipt(stored_receipt, confirmed_items)
    table = dynamo_repo.table
    original_query, original_scan = table.query, table.scan

    def query_page(**kwargs: Any) -> dict[str, Any]:
        return original_query(**kwargs, Limit=PAGE_SIZE)

    def scan_page(**kwargs: Any) -> dict[str, Any]:
        return original_scan(**kwargs, Limit=PAGE_SIZE)

    with patch.object(table, "query", side_effect=query_page) as query:
        entities = dynamo_repo.query_receipt(stored_receipt.receipt_id)
    with patch.object(table, "scan", side_effect=scan_page) as scan:
        scanned = dynamo_repo.scan()
        found = dynamo_repo.find_by_client_token(stored_receipt.client_token)
        missing = dynamo_repo.find_by_client_token(uuid4())

    assert query.call_count == 2
    assert scan.call_count == 6
    assert len(scanned) == len(entities) == 4
    assert found == stored_receipt
    assert missing is None
    assert "ExclusiveStartKey" in query.call_args.kwargs


def test_batch_unprocessed_subset_retries_only_remaining_items(
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    client = dynamo_repo.table.meta.client
    original = client.batch_write_item
    requests: list[list[dict[str, Any]]] = []

    def partial_write(**kwargs: Any) -> dict[str, Any]:
        entries = kwargs["RequestItems"][dynamo_repo.table.name]
        requests.append(entries)
        if len(requests) == 1:
            original(RequestItems={dynamo_repo.table.name: entries[:2]})
            return {"UnprocessedItems": {dynamo_repo.table.name: entries[2:]}}
        return original(**kwargs)

    with patch.object(client, "batch_write_item", side_effect=partial_write):
        dynamo_repo.save_receipt(stored_receipt, confirmed_items)

    assert requests[1] == requests[0][2:]
    assert len(requests) == 2
    assert len(dynamo_repo.query_receipt(stored_receipt.receipt_id)) == 4


@pytest.mark.parametrize("failure", ["exception", "unprocessed", "lost_response"])
def test_save_partial_failure_cleans_only_target_partition(
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
    failure: str,
) -> None:
    other = stored_receipt.model_copy(
        update={"receipt_id": "01J5ZAAAAAAAAAAAAAAAAAAAAA", "client_token": uuid4()}
    )
    dynamo_repo.save_receipt(other, confirmed_items)
    client = dynamo_repo.table.meta.client
    original = client.batch_write_item
    put_calls = 0

    def fail_after_chunk(**kwargs: Any) -> dict[str, Any]:
        nonlocal put_calls
        entries = kwargs["RequestItems"][dynamo_repo.table.name]
        if "DeleteRequest" in entries[0]:
            return original(**kwargs)
        put_calls += 1
        if put_calls == 1:
            return original(**kwargs)
        if failure == "unprocessed":
            return {"UnprocessedItems": kwargs["RequestItems"]}
        if failure == "lost_response":
            original(**kwargs)
        raise ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "write failed"}},
            "BatchWriteItem",
        )

    error_type = UnprocessedItemsError if failure == "unprocessed" else ClientError
    with patch.object(client, "batch_write_item", side_effect=fail_after_chunk):
        with pytest.raises(error_type):
            dynamo_repo.save_receipt(stored_receipt, [confirmed_items[0]] * 30)

    assert 2 <= put_calls <= 5
    assert dynamo_repo.query_receipt(stored_receipt.receipt_id) == []
    assert dynamo_repo.find_by_client_token(stored_receipt.client_token) is None
    assert len(dynamo_repo.query_receipt(other.receipt_id)) == 4


def test_cleanup_failure_logs_and_preserves_original_error(
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = dynamo_repo.table.meta.client
    original = client.batch_write_item
    write_error = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "original failure"}},
        "BatchWriteItem",
    )
    calls = 0

    def fail_write_and_cleanup(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(**kwargs)
        if calls == 2:
            raise write_error
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "cleanup denied"}},
            "BatchWriteItem",
        )

    with patch.object(client, "batch_write_item", side_effect=fail_write_and_cleanup):
        with pytest.raises(ClientError) as error:
            dynamo_repo.save_receipt(stored_receipt, [confirmed_items[0]] * 30)

    assert error.value is write_error
    assert "cleanup" in caplog.text.lower()
    assert calls == 3


def test_delete_receipt_paginates_chunks_and_preserves_other_partition(
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    other = stored_receipt.model_copy(
        update={"receipt_id": "01J5ZAAAAAAAAAAAAAAAAAAAAA", "client_token": uuid4()}
    )
    dynamo_repo.save_receipt(stored_receipt, [confirmed_items[0]] * 30)
    dynamo_repo.save_receipt(other, confirmed_items)
    table = dynamo_repo.table
    original_query = table.query

    def query_page(**kwargs: Any) -> dict[str, Any]:
        return original_query(**kwargs, Limit=PAGE_SIZE)

    with patch.object(table, "query", side_effect=query_page):
        with patch.object(
            table.meta.client,
            "batch_write_item",
            wraps=table.meta.client.batch_write_item,
        ) as batch:
            deleted = dynamo_repo.delete_receipt(stored_receipt.receipt_id)

    assert deleted is True
    assert batch.call_count == 2
    assert dynamo_repo.query_receipt(stored_receipt.receipt_id) == []
    assert len(dynamo_repo.query_receipt(other.receipt_id)) == 4
    assert dynamo_repo.delete_receipt(stored_receipt.receipt_id) is False


def test_empty_table_returns_empty_results(dynamo_repo: DynamoRepository) -> None:
    assert dynamo_repo.scan() == []
    assert dynamo_repo.query_receipt("01J5ZC8YV3Q4R6T8W9XABCDEF0") == []
    assert dynamo_repo.find_by_client_token(uuid4()) is None


@pytest.mark.parametrize(
    "operation", ["scan", "query_receipt", "find_by_client_token", "delete_receipt"]
)
def test_repository_aws_read_errors_propagate(
    dynamo_repo: DynamoRepository,
    dynamo_table: Any,
    operation: str,
) -> None:
    dynamo_table.delete()
    method = getattr(dynamo_repo, operation)
    args = () if operation == "scan" else ("01J5ZC8YV3Q4R6T8W9XABCDEF0",)

    with pytest.raises(ClientError):
        method(*args)
