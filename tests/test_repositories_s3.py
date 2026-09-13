"""S3 image operations against moto; storage errors remain service concerns."""

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from kakeibo.models import ConfirmedItem, Receipt
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.repositories.s3 import S3Repository, build_image_key

RECEIPT_ID = "01J5ZC8YV3Q4R6T8W9XABCDEF0"
JPEG_BYTES = b"\xff\xd8\xff\xe0test image\xff\xd9"
PNG_BYTES = b"\x89PNG\r\n\x1a\ntest image"


@pytest.mark.parametrize(
    "confirmed_date,expected",
    [
        (date(2026, 7, 31), "2026/07"),
        (date(2025, 12, 31), "2025/12"),
        (date(2024, 2, 29), "2024/02"),
    ],
)
def test_image_key_uses_confirmed_receipt_month(
    confirmed_date: date, expected: str
) -> None:
    key = build_image_key(RECEIPT_ID, confirmed_date)

    assert key == f"receipts/{expected}/{RECEIPT_ID}.jpg"


@pytest.mark.parametrize(
    "data,content_type", [(JPEG_BYTES, "image/jpeg"), (PNG_BYTES, "image/png")]
)
def test_put_and_get_preserve_original_bytes_and_content_type(
    s3_repo: S3Repository,
    s3_client: Any,
    settings_env: dict[str, str],
    data: bytes,
    content_type: str,
) -> None:
    key = build_image_key(RECEIPT_ID, date(2026, 7, 31))

    s3_repo.put_image(key, data, content_type=content_type)

    assert s3_repo.get_image(key) == data
    metadata = s3_client.head_object(
        Bucket=settings_env["KAKEIBO_BUCKET_NAME"], Key=key
    )
    assert metadata["ContentType"] == content_type
    assert metadata["ContentLength"] == len(data)
    objects = s3_client.list_objects_v2(Bucket=settings_env["KAKEIBO_BUCKET_NAME"])[
        "Contents"
    ]
    assert [item["Key"] for item in objects] == [key]
    acl = s3_client.get_object_acl(Bucket=settings_env["KAKEIBO_BUCKET_NAME"], Key=key)
    assert all(grant["Grantee"]["Type"] == "CanonicalUser" for grant in acl["Grants"])


def test_put_image_defaults_to_jpeg(
    s3_repo: S3Repository, s3_client: Any, settings_env: dict[str, str]
) -> None:
    key = build_image_key(RECEIPT_ID, date(2026, 8, 25))

    s3_repo.put_image(key, JPEG_BYTES)

    assert (
        s3_client.head_object(Bucket=settings_env["KAKEIBO_BUCKET_NAME"], Key=key)[
            "ContentType"
        ]
        == "image/jpeg"
    )


def test_delete_image_removes_only_target_and_is_idempotent(
    s3_repo: S3Repository,
) -> None:
    key = build_image_key(RECEIPT_ID, date(2026, 8, 25))
    other_key = build_image_key(RECEIPT_ID, date(2026, 7, 25))
    s3_repo.put_image(key, JPEG_BYTES)
    s3_repo.put_image(other_key, PNG_BYTES, content_type="image/png")

    s3_repo.delete_image(key)
    s3_repo.delete_image(key)

    with pytest.raises(ClientError) as error:
        s3_repo.get_image(key)
    assert error.value.response["Error"]["Code"] == "NoSuchKey"
    assert s3_repo.get_image(other_key) == PNG_BYTES


def test_get_missing_image_propagates_not_found(s3_repo: S3Repository) -> None:
    with pytest.raises(ClientError) as error:
        s3_repo.get_image("missing.jpg")

    assert error.value.response["Error"]["Code"] == "NoSuchKey"


@pytest.mark.parametrize("operation", ["put_image", "get_image", "delete_image"])
def test_image_operation_aws_failure_propagates(
    s3_repo: S3Repository, operation: str
) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation
    )
    args = ("receipt.jpg", JPEG_BYTES) if operation == "put_image" else ("receipt.jpg",)

    with patch.object(s3_repo.client, "_make_api_call", side_effect=error):
        with pytest.raises(ClientError) as caught:
            getattr(s3_repo, operation)(*args)

    assert caught.value is error


def test_delete_receipt_keeps_image(
    s3_repo: S3Repository,
    dynamo_repo: DynamoRepository,
    stored_receipt: Receipt,
    confirmed_items: list[ConfirmedItem],
) -> None:
    s3_repo.put_image(stored_receipt.s3_key, JPEG_BYTES)
    dynamo_repo.save_receipt(stored_receipt, confirmed_items)

    assert dynamo_repo.delete_receipt(stored_receipt.receipt_id) is True

    assert s3_repo.get_image(stored_receipt.s3_key) == JPEG_BYTES
