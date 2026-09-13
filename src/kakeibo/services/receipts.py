"""Confirmed-save coordination and database-only receipt deletion."""

import logging
from typing import Any

from ulid import ULID

from kakeibo.models import Receipt, SaveReceipt
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.repositories.s3 import JPEG_CONTENT_TYPE, S3Repository, build_image_key

logger = logging.getLogger(__name__)


class ImageUploadError(RuntimeError):
    """Image persistence failed; the HTTP layer maps this to 502."""

    def __init__(self) -> None:
        super().__init__("image_upload_failed")


class ReceiptSaveError(RuntimeError):
    """Database persistence failed; the HTTP layer maps this to 500."""

    def __init__(self) -> None:
        super().__init__("save failed")


class ReceiptService:
    def __init__(self, dynamo: DynamoRepository, s3: S3Repository) -> None:
        self.dynamo = dynamo
        self.s3 = s3

    def save_receipt(
        self,
        payload: SaveReceipt | dict[str, Any],
        image: bytes | None = None,
        *,
        content_type: str = JPEG_CONTENT_TYPE,
    ) -> str:
        """Validate, reuse a token, or persist an optional image before DynamoDB.

        Scan-based idempotency covers sequential resends, not concurrent saves.
        DynamoRepository owns cleanup of partial database writes; this service
        compensates the image even if database cleanup itself failed.
        """
        # Revalidate even model instances that were mutated or model_construct'ed.
        if isinstance(payload, SaveReceipt):
            payload = payload.model_dump()
        confirmed = SaveReceipt.model_validate(payload)
        try:
            existing = self.dynamo.find_by_client_token(confirmed.client_token)
        except Exception as error:
            raise ReceiptSaveError() from error
        if existing is not None:
            return existing.receipt_id

        receipt_id = str(ULID())
        key = build_image_key(receipt_id, confirmed.date) if image is not None else None
        receipt = Receipt(
            receipt_id=receipt_id,
            s3_key=key,
            **confirmed.model_dump(exclude={"items"}),
        )
        if key is not None and image is not None:
            try:
                self.s3.put_image(key, image, content_type=content_type)
            except Exception as error:
                raise ImageUploadError() from error
        try:
            self.dynamo.save_receipt(receipt, confirmed.items)
        except Exception as error:
            if key is not None:
                try:
                    self.s3.delete_image(key)
                except Exception:
                    logger.exception("Image cleanup failed for receipt %s", receipt_id)
            raise ReceiptSaveError() from error
        return receipt_id

    def delete_receipt(self, receipt_id: str) -> bool:
        """Query and delete META plus all ITEMs, retaining the original S3 image."""
        return self.dynamo.delete_receipt(receipt_id)
