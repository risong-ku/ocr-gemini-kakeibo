"""Confirmation and deletion with real repositories backed by moto."""

from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from ulid import ULID

from kakeibo.models import LineItem, Receipt, SaveReceipt
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.repositories.s3 import S3Repository
from kakeibo.services.receipts import ImageUploadError, ReceiptSaveError, ReceiptService

IMAGE = b"\xff\xd8receipt\xff\xd9"


@pytest.fixture
def receipt_service(
    dynamo_repo: DynamoRepository, s3_repo: S3Repository
) -> ReceiptService:
    return ReceiptService(dynamo_repo, s3_repo)


def test_save_receipt_uses_confirmed_date_and_uploads_before_database(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    receipt_payload["date"] = "2026-07-31"
    client = dynamo_repo.table.meta.client
    original = client.batch_write_item

    def verify_image_first(**kwargs: Any) -> dict[str, Any]:
        meta = kwargs["RequestItems"][dynamo_repo.table.name][0]["PutRequest"]["Item"]
        assert s3_repo.get_image(meta["s3_key"]) == IMAGE
        return original(**kwargs)

    with patch.object(client, "batch_write_item", side_effect=verify_image_first):
        receipt_id = receipt_service.save_receipt(
            SaveReceipt.model_validate(receipt_payload), IMAGE
        )
    assert str(ULID.from_str(receipt_id)) == receipt_id
    entities = dynamo_repo.query_receipt(receipt_id)
    meta = next(entity for entity in entities if isinstance(entity, Receipt))
    assert meta.s3_key == f"receipts/2026/07/{receipt_id}.jpg"
    assert meta.total == 1738  # Deliberately differs from item sum.
    assert meta.created_at.isoformat().endswith("+09:00")
    assert [item.seq for item in entities if isinstance(item, LineItem)] == [1, 2, 3]
    assert all("uncertain" not in row for row in dynamo_repo.table.scan()["Items"])


@pytest.mark.parametrize("count", [1, 999])
def test_save_receipt_item_boundaries(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    count: int,
) -> None:
    receipt_payload["items"] = [receipt_payload["items"][0]] * count
    receipt_id = receipt_service.save_receipt(receipt_payload, IMAGE)
    assert len(dynamo_repo.query_receipt(receipt_id)) == count + 1


def test_manual_receipt_save_resend_and_delete_skip_s3(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    with patch.object(s3_repo.client, "_make_api_call") as s3_call:
        receipt_id = receipt_service.save_receipt(receipt_payload)
        meta = dynamo_repo.find_by_client_token(receipt_payload["client_token"])
        assert meta is not None
        assert meta.s3_key is None
        assert meta.total == receipt_payload["total"]
        assert receipt_service.save_receipt(receipt_payload, None) == receipt_id
        assert len(dynamo_repo.query_receipt(receipt_id)) == 4
        assert receipt_service.delete_receipt(receipt_id) is True
    s3_call.assert_not_called()
    assert dynamo_repo.scan() == []


def test_manual_receipt_database_failure_skips_image_cleanup(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    with (
        patch.object(s3_repo.client, "_make_api_call") as s3_call,
        patch.object(
            dynamo_repo.table.meta.client,
            "batch_write_item",
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        with pytest.raises(ReceiptSaveError, match="save failed"):
            receipt_service.save_receipt(receipt_payload)
    s3_call.assert_not_called()
    assert dynamo_repo.scan() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_token", None),
        ("client_token", "invalid"),
        ("client_token", "6f1d2c1e-6a5b-1e2f-9c3a-8b7d5e4f1a20"),
        ("items", []),
        ("items", 1000),
        ("date", "2026-00-01"),
        ("date", "2026-13-01"),
        ("date", "2026-02-30"),
        ("total", "1738"),
        ("total", True),
        ("price", "1480"),
        ("price", 1.5),
        ("price", False),
        ("category", "other"),
    ],
)
def test_save_receipt_invalid_payload_has_no_storage_calls(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
    field: str,
    value: Any,
) -> None:
    if field in {"price", "category"}:
        receipt_payload["items"][0][field] = value
    elif field == "items" and value == 1000:
        receipt_payload[field] = [receipt_payload["items"][0]] * value
    elif field == "client_token" and value is None:
        del receipt_payload[field]
    else:
        receipt_payload[field] = value
    with (
        patch.object(dynamo_repo.table, "scan") as scan,
        patch.object(s3_repo.client, "put_object") as put,
    ):
        with pytest.raises(ValidationError):
            receipt_service.save_receipt(receipt_payload, IMAGE)
    scan.assert_not_called()
    put.assert_not_called()


def test_save_receipt_resend_returns_existing_id_without_writes(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    receipt_id = receipt_service.save_receipt(receipt_payload, IMAGE)
    receipt_payload["total"] = 42
    with (
        patch.object(s3_repo.client, "put_object") as put,
        patch.object(dynamo_repo.table.meta.client, "batch_write_item") as batch,
    ):
        assert receipt_service.save_receipt(receipt_payload, b"different") == receipt_id
    put.assert_not_called()
    batch.assert_not_called()
    assert (
        dynamo_repo.find_by_client_token(receipt_payload["client_token"]).total == 1738
    )


def test_save_receipt_s3_failure_is_typed_and_database_stays_empty(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    with patch.object(
        s3_repo.client, "put_object", side_effect=RuntimeError("S3 unavailable")
    ):
        with pytest.raises(ImageUploadError, match="image_upload_failed"):
            receipt_service.save_receipt(receipt_payload, IMAGE)
    assert dynamo_repo.scan() == []


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_save_receipt_partial_database_failure_cleans_up_and_preserves_error(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
    cleanup_fails: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receipt_payload["items"] = [receipt_payload["items"][0]] * 30
    client = dynamo_repo.table.meta.client
    original_batch = client.batch_write_item
    original_delete = s3_repo.client.delete_object
    failure = RuntimeError("database write failed")
    put_count = 0

    def fail_second_chunk(**kwargs: Any) -> dict[str, Any]:
        nonlocal put_count
        entries = kwargs["RequestItems"][dynamo_repo.table.name]
        if "DeleteRequest" in entries[0]:
            if cleanup_fails:
                raise RuntimeError("database cleanup failed")
            return original_batch(**kwargs)
        put_count += 1
        if put_count == 1:
            return original_batch(**kwargs)
        raise failure

    def delete_image(**kwargs: Any) -> dict[str, Any]:
        if cleanup_fails:
            raise RuntimeError("image cleanup failed")
        return original_delete(**kwargs)

    with (
        patch.object(client, "batch_write_item", side_effect=fail_second_chunk),
        patch.object(
            s3_repo.client, "delete_object", side_effect=delete_image
        ) as delete,
    ):
        with pytest.raises(ReceiptSaveError, match="save failed") as error:
            receipt_service.save_receipt(receipt_payload, IMAGE)
    assert error.value.__cause__ is failure
    delete.assert_called_once()
    if cleanup_fails:
        assert "cleanup" in caplog.text.lower()
        return
    assert dynamo_repo.scan() == []
    assert s3_repo.client.list_objects_v2(Bucket=s3_repo.bucket_name)["KeyCount"] == 0
    # A retry after successful compensation can persist normally.
    assert receipt_service.save_receipt(receipt_payload, IMAGE)


def test_save_receipt_lookup_failure_is_typed_before_upload(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    with (
        patch.object(
            dynamo_repo.table, "scan", side_effect=RuntimeError("lookup failed")
        ),
        patch.object(s3_repo.client, "put_object") as put,
    ):
        with pytest.raises(ReceiptSaveError):
            receipt_service.save_receipt(receipt_payload, IMAGE)
    put.assert_not_called()


def test_delete_receipt_removes_all_entities_keeps_image_and_reports_missing(
    receipt_service: ReceiptService,
    receipt_payload: dict[str, Any],
    dynamo_repo: DynamoRepository,
    s3_repo: S3Repository,
) -> None:
    receipt_payload["items"] = [receipt_payload["items"][0]] * 30
    receipt_id = receipt_service.save_receipt(receipt_payload, IMAGE)
    meta = dynamo_repo.find_by_client_token(receipt_payload["client_token"])
    assert receipt_service.delete_receipt(receipt_id) is True
    assert dynamo_repo.query_receipt(receipt_id) == []
    assert s3_repo.get_image(meta.s3_key) == IMAGE
    assert receipt_service.delete_receipt(receipt_id) is False


def test_delete_receipt_database_failure_propagates(
    receipt_service: ReceiptService, dynamo_repo: DynamoRepository
) -> None:
    dynamo_repo.table.delete()
    with pytest.raises(ClientError):
        receipt_service.delete_receipt("01J5ZC8YV3Q4R6T8W9XABCDEF0")
