"""Shared pytest fixtures for kakeibo tests."""

import json
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from kakeibo.auth import AuthMiddleware, SessionAuth
from kakeibo.config import load_settings
from kakeibo.container import AppContainer
from kakeibo.main import create_app
from kakeibo.models import ConfirmedItem, Receipt, SaveReceipt
from kakeibo.repositories.dynamo import DynamoRepository
from kakeibo.repositories.s3 import S3Repository


@pytest.fixture
def receipt_payload() -> dict[str, Any]:
    return {
        "client_token": "6f1d2c1e-6a5b-4e2f-9c3a-8b7d5e4f1a20",
        "date": "2026-08-25",
        "store": "西松屋",
        "total": 1738,
        "items": [
            {"name": "おむつM", "price": 1480, "category": "child"},
            {"name": "ハンカチ", "price": 258, "category": "couple"},
            {"name": "ポイント利用", "price": -100, "category": "excluded"},
        ],
    }


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "KAKEIBO_TABLE_NAME": "kakeibo-test",
        "KAKEIBO_BUCKET_NAME": "kakeibo-test-images",
        "AWS_REGION": "ap-northeast-1",
        "SESSION_SECRET": "test-signing-secret-only",
        "APP_PASSCODE": "test passphrase only",
        "GEMINI_API_KEY": "test-gemini-key-only",
        "COOKIE_SECURE": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def auth(settings_env: dict[str, str]) -> SessionAuth:
    return SessionAuth(load_settings())


@pytest.fixture
def auth_client(auth: SessionAuth) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, auth=auth)

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    def endpoint(path: str) -> dict[str, str]:
        return {"path": path}

    with TestClient(
        app, base_url="https://testserver", follow_redirects=False
    ) as client:
        yield client


@pytest.fixture
def aws_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    with mock_aws():
        yield


@pytest.fixture
def dynamo_table(aws_mock: None, settings_env: dict[str, str]) -> Any:
    return boto3.resource(
        "dynamodb", region_name=settings_env["AWS_REGION"]
    ).create_table(
        TableName=settings_env["KAKEIBO_TABLE_NAME"],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": key, "AttributeType": "S"} for key in ("PK", "SK")
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def dynamo_repo(dynamo_table: Any, settings_env: dict[str, str]) -> DynamoRepository:
    return DynamoRepository(
        settings_env["KAKEIBO_TABLE_NAME"], settings_env["AWS_REGION"]
    )


@pytest.fixture
def stored_receipt(receipt_payload: dict[str, Any]) -> Receipt:
    receipt_id = "01J5ZC8YV3Q4R6T8W9XABCDEF0"
    return Receipt(
        receipt_id=receipt_id,
        **{key: value for key, value in receipt_payload.items() if key != "items"},
        s3_key=f"receipts/2026/08/{receipt_id}.jpg",
        created_at="2026-08-25T10:12:34+09:00",
    )


@pytest.fixture
def confirmed_items(receipt_payload: dict[str, Any]) -> list[ConfirmedItem]:
    return SaveReceipt.model_validate(receipt_payload).items


@pytest.fixture
def s3_client(aws_mock: None, settings_env: dict[str, str]) -> Any:
    client = boto3.client("s3", region_name=settings_env["AWS_REGION"])
    client.create_bucket(
        Bucket=settings_env["KAKEIBO_BUCKET_NAME"],
        CreateBucketConfiguration={"LocationConstraint": settings_env["AWS_REGION"]},
    )
    return client


@pytest.fixture
def s3_repo(s3_client: Any, settings_env: dict[str, str]) -> S3Repository:
    return S3Repository(settings_env["KAKEIBO_BUCKET_NAME"], settings_env["AWS_REGION"])


@pytest.fixture
def gemini_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def app_container(
    dynamo_table: Any,
    s3_client: Any,
    settings_env: dict[str, str],
    receipt_payload: dict[str, Any],
    gemini_requests: list[httpx.Request],
) -> Iterator[AppContainer]:
    def respond(request: httpx.Request) -> httpx.Response:
        gemini_requests.append(request)
        draft = {
            key: value
            for key, value in receipt_payload.items()
            if key != "client_token"
        }
        draft["items"] = [{**item, "uncertain": False} for item in draft["items"]]
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(draft)}]}}]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        container = AppContainer(load_settings(), http_client=client)
        try:
            yield container
        finally:
            container.close()


@pytest.fixture
def web_client(app_container: AppContainer) -> Iterator[TestClient]:
    with TestClient(
        create_app(container=app_container),
        base_url="https://testserver",
        follow_redirects=False,
    ) as client:
        yield client


@pytest.fixture
def logged_in_client(web_client: TestClient, app_container: AppContainer) -> TestClient:
    response = web_client.post(
        "/login", data={"passcode": app_container.settings.app_passcode}
    )
    assert response.status_code == 303
    return web_client
