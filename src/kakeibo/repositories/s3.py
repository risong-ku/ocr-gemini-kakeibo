"""Private receipt image storage; HTTP errors and compensation live in services."""

from datetime import date

import boto3
from botocore.config import Config

from kakeibo.config import DEFAULT_AWS_REGION

IMAGE_PREFIX = "receipts"
JPEG_CONTENT_TYPE = "image/jpeg"
AWS_CONNECT_TIMEOUT_SECONDS = 2
AWS_READ_TIMEOUT_SECONDS = 5
AWS_MAX_ATTEMPTS = 3


def build_image_key(receipt_id: str, receipt_date: date) -> str:
    """Use the confirmed JST purchase date, never the upload clock."""
    return f"{IMAGE_PREFIX}/{receipt_date.year:04d}/{receipt_date.month:02d}/{receipt_id}.jpg"


class S3Repository:
    def __init__(self, bucket_name: str, region_name: str = DEFAULT_AWS_REGION) -> None:
        self.bucket_name = bucket_name
        self.client = boto3.client(
            "s3",
            region_name=region_name,
            config=Config(
                connect_timeout=AWS_CONNECT_TIMEOUT_SECONDS,
                read_timeout=AWS_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": AWS_MAX_ATTEMPTS},
            ),
        )

    def put_image(
        self, key: str, data: bytes, content_type: str = JPEG_CONTENT_TYPE
    ) -> None:
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    def get_image(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket_name, Key=key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def delete_image(self, key: str) -> None:
        """Compensate a failed save; normal receipt deletion keeps images."""
        self.client.delete_object(Bucket=self.bucket_name, Key=key)
