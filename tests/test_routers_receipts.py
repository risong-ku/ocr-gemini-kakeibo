"""Multipart receipt contracts through real services and mocked providers."""

import base64
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from kakeibo.container import AppContainer
from kakeibo.models import Receipt

IMAGE = b"\xff\xd8receipt\xff\xd9"
MAX_BYTES = 5 * 1024 * 1024


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png"])
def test_parse_returns_draft_without_persistence(
    logged_in_client: TestClient,
    app_container: AppContainer,
    gemini_requests: list[httpx.Request],
    content_type: str,
) -> None:
    response = logged_in_client.post(
        "/api/receipts/parse", files={"file": ("receipt", IMAGE, content_type)}
    )
    assert response.status_code == 200
    assert response.json() == {
        "store": "西松屋",
        "date": "2026-08-25",
        "total": 1738,
        "items": [
            {"name": "おむつM", "price": 1480, "category": "child", "uncertain": False},
            {
                "name": "ハンカチ",
                "price": 258,
                "category": "couple",
                "uncertain": False,
            },
            {
                "name": "ポイント利用",
                "price": -100,
                "category": "excluded",
                "uncertain": False,
            },
        ],
        "warnings": ["品目合計 1638円 がレシート記載合計 1738円 と一致しません"],
    }
    inline = json.loads(gemini_requests[0].content)["contents"][0]["parts"][1][
        "inline_data"
    ]
    assert inline == {
        "mime_type": content_type,
        "data": base64.b64encode(IMAGE).decode(),
    }
    assert app_container.dynamo.scan() == []
    assert (
        app_container.s3.client.list_objects_v2(
            Bucket=app_container.settings.bucket_name
        )["KeyCount"]
        == 0
    )


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/api/receipts/parse", "missing"),
        ("/api/receipts/parse", "wrong-type"),
        ("/api/receipts/parse", "too-large"),
        ("/api/receipts", "wrong-type"),
        ("/api/receipts", "too-large"),
    ],
)
def test_invalid_images_return_standard_422_before_service_calls(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    path: str,
    kind: str,
) -> None:
    files = (
        {}
        if kind == "missing"
        else {
            "file": (
                "receipt",
                b"x" * (MAX_BYTES + 1) if kind == "too-large" else IMAGE,
                "text/plain" if kind == "wrong-type" else "image/jpeg",
            )
        }
    )
    with (
        patch.object(app_container.gemini, "parse_receipt") as parse,
        patch.object(app_container.receipts, "save_receipt") as save,
    ):
        response = logged_in_client.post(
            path, data={"payload": json.dumps(receipt_payload)}, files=files
        )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert isinstance(errors, list)
    assert errors[0]["loc"] == ["body", "file"]
    assert {"type", "msg"} <= errors[0].keys()
    parse.assert_not_called()
    save.assert_not_called()


@pytest.mark.parametrize("multipart", [False, True])
def test_save_without_file_omits_s3_key_and_never_calls_s3(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    multipart: bool,
) -> None:
    payload = json.dumps(receipt_payload)
    with patch.object(app_container.s3.client, "_make_api_call") as s3_call:
        response = logged_in_client.post(
            "/api/receipts",
            data=None if multipart else {"payload": payload},
            files={"payload": (None, payload)} if multipart else None,
        )
    assert response.status_code == 201
    receipt_id = response.json()["receipt_id"]
    meta = app_container.dynamo.table.get_item(
        Key={"PK": f"RECEIPT#{receipt_id}", "SK": "META"}
    )["Item"]
    assert "s3_key" not in meta
    assert meta["total"] == receipt_payload["total"]
    assert len(app_container.dynamo.query_receipt(receipt_id)) == 4
    s3_call.assert_not_called()
    assert (
        app_container.s3.client.list_objects_v2(
            Bucket=app_container.settings.bucket_name
        )["KeyCount"]
        == 0
    )


@pytest.mark.parametrize(
    "path,status", [("/api/receipts/parse", 200), ("/api/receipts", 201)]
)
def test_image_at_size_limit_is_accepted(
    logged_in_client: TestClient,
    receipt_payload: dict[str, Any],
    path: str,
    status: int,
) -> None:
    response = logged_in_client.post(
        path,
        data={"payload": json.dumps(receipt_payload)},
        files={"file": ("receipt.jpg", b"x" * MAX_BYTES, "image/jpeg")},
    )
    assert response.status_code == status


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png"])
def test_save_and_resave_preserve_id_without_rewriting(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    content_type: str,
) -> None:
    response = logged_in_client.post(
        "/api/receipts",
        data={"payload": json.dumps(receipt_payload)},
        files={"file": ("receipt", IMAGE, content_type)},
    )
    assert response.status_code == 201
    receipt_id = response.json()["receipt_id"]
    assert set(response.json()) == {"receipt_id"}
    entities = app_container.dynamo.query_receipt(receipt_id)
    meta = next(entity for entity in entities if isinstance(entity, Receipt))
    assert meta.total == 1738
    assert len(entities) == 4
    assert app_container.s3.get_image(meta.s3_key) == IMAGE
    assert (
        app_container.s3.client.head_object(
            Bucket=app_container.settings.bucket_name, Key=meta.s3_key
        )["ContentType"]
        == content_type
    )
    receipt_payload["total"] = 42
    with (
        patch.object(app_container.s3.client, "put_object") as put,
        patch.object(
            app_container.dynamo.table.meta.client, "batch_write_item"
        ) as batch,
    ):
        retry = logged_in_client.post(
            "/api/receipts",
            data={"payload": json.dumps(receipt_payload)},
            files={"file": ("receipt", b"different", content_type)},
        )
    assert retry.status_code == 201
    assert retry.json() == {"receipt_id": receipt_id}
    put.assert_not_called()
    batch.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", "{broken"),
        ("payload", None),
        ("payload", "[]"),
        ("client_token", None),
        ("client_token", "invalid"),
        ("client_token", "6f1d2c1e-6a5b-1e2f-9c3a-8b7d5e4f1a20"),
        ("date", "2026-02-30"),
        ("date", "2026-8-25"),
        ("items", []),
        ("items", 1000),
        ("price", "1480"),
        ("price", 1.5),
        ("price", True),
        ("category", "other"),
        ("total", "1738"),
        ("total", False),
        ("uncertain", True),
    ],
)
def test_save_invalid_payload_is_field_addressable_422(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    if field in {"price", "category", "uncertain"}:
        receipt_payload["items"][0][field] = value
    elif field == "items" and value == 1000:
        receipt_payload[field] = [receipt_payload["items"][0]] * value
    elif value is None:
        receipt_payload.pop(field, None)
    else:
        receipt_payload[field] = value
    data = {"payload": json.dumps(receipt_payload)}
    if field == "payload":
        data = {} if value is None else {"payload": value}
    with patch.object(app_container.receipts, "save_receipt") as save:
        response = logged_in_client.post(
            "/api/receipts",
            data=data,
            files={"file": ("receipt.jpg", IMAGE, "image/jpeg")},
        )
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"][:2] == ["body", "payload"]
    assert {"type", "msg"} <= error.keys()
    save.assert_not_called()


@pytest.mark.parametrize("upstream_status", [429, 500])
def test_gemini_failure_maps_to_502(
    logged_in_client: TestClient, app_container: AppContainer, upstream_status: int
) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(upstream_status)
            )
        ) as client,
        patch.object(app_container.gemini, "client", client),
        patch("kakeibo.services.gemini.time.sleep"),
    ):
        response = logged_in_client.post(
            "/api/receipts/parse", files={"file": ("receipt.jpg", IMAGE, "image/jpeg")}
        )
    assert response.status_code == 502
    assert response.json() == {"detail": "gemini_parse_failed"}
    assert app_container.dynamo.scan() == []


@pytest.mark.parametrize(
    "failure,status,detail",
    [("image", 502, "image_upload_failed"), ("database", 500, "save failed")],
)
def test_save_failure_mapping_and_compensation(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    failure: str,
    status: int,
    detail: str,
) -> None:
    target = app_container.s3 if failure == "image" else app_container.dynamo
    method = "put_image" if failure == "image" else "save_receipt"
    with patch.object(
        target, method, side_effect=RuntimeError("private provider details")
    ):
        response = logged_in_client.post(
            "/api/receipts",
            data={"payload": json.dumps(receipt_payload)},
            files={"file": ("receipt.jpg", IMAGE, "image/jpeg")},
        )
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert app_container.dynamo.scan() == []
    assert (
        app_container.s3.client.list_objects_v2(
            Bucket=app_container.settings.bucket_name
        )["KeyCount"]
        == 0
    )


def test_delete_removes_database_retains_image_and_then_returns_404(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
) -> None:
    receipt_id = app_container.receipts.save_receipt(receipt_payload, IMAGE)
    response = logged_in_client.delete(f"/api/receipts/{receipt_id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": receipt_id}
    assert app_container.dynamo.scan() == []
    assert app_container.s3.get_image(f"receipts/2026/08/{receipt_id}.jpg") == IMAGE
    for missing in (receipt_id, "not-a-receipt"):
        response = logged_in_client.delete(f"/api/receipts/{missing}")
        assert response.status_code == 404
        assert response.json() == {"detail": "receipt not found"}
