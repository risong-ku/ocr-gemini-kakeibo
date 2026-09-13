"""Multipart validation and receipt service HTTP contracts."""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from kakeibo.container import ContainerDependency
from kakeibo.models import ReceiptCreated, ReceiptDeleted, ReceiptDraft, SaveReceipt
from kakeibo.services.gemini import GeminiParseError
from kakeibo.services.receipts import ImageUploadError, ReceiptSaveError

router = APIRouter(prefix="/api/receipts")
ACCEPTED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def read_image(file: UploadFile) -> tuple[bytes, str]:
    """Read at most one byte over the limit, before invoking any service."""
    content_type = file.content_type
    if content_type is None or content_type not in ACCEPTED_IMAGE_TYPES:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "file"),
                    "msg": "Image content-type must be image/jpeg or image/png",
                    "input": file.content_type,
                }
            ]
        )
    image = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(image) > MAX_IMAGE_BYTES:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "file"),
                    "msg": "Image must be at most 5MB",
                    "input": len(image),
                }
            ]
        )
    return image, content_type


@router.post("/parse", response_model=ReceiptDraft)
def parse_receipt(
    file: Annotated[UploadFile, File()], container: ContainerDependency
) -> ReceiptDraft:
    image, content_type = read_image(file)
    try:
        return container.gemini.parse_receipt(image, content_type=content_type)
    except GeminiParseError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "gemini_parse_failed"
        ) from error


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ReceiptCreated)
def save_receipt(
    payload: Annotated[str, Form()],
    container: ContainerDependency,
    file: Annotated[UploadFile | None, File()] = None,
) -> ReceiptCreated:
    try:
        confirmed = SaveReceipt.model_validate_json(payload)
    except ValidationError as error:
        errors = [
            {**entry, "loc": ("body", "payload", *entry["loc"])}
            for entry in error.errors(include_url=False, include_context=False)
        ]
        raise RequestValidationError(errors) from error
    try:
        if file is None:
            receipt_id = container.receipts.save_receipt(confirmed)
        else:
            image, content_type = read_image(file)
            receipt_id = container.receipts.save_receipt(
                confirmed, image, content_type=content_type
            )
    except ImageUploadError as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "image_upload_failed"
        ) from error
    except ReceiptSaveError as error:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "save failed"
        ) from error
    return ReceiptCreated(receipt_id=receipt_id)


@router.delete("/{id}", response_model=ReceiptDeleted)
def delete_receipt(id: str, container: ContainerDependency) -> ReceiptDeleted:
    if not container.receipts.delete_receipt(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "receipt not found")
    return ReceiptDeleted(deleted=id)
