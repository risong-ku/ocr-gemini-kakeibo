"""JS-free acceptance through HTTP, real services, moto and mocked Gemini HTTP."""

import base64
import codecs
import csv
import io
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from kakeibo.container import AppContainer
from kakeibo.models import now_jst
from kakeibo.services.gemini import GeminiParseError

IMAGE = b"\xff\xd8receipt\xff\xd9"


def test_login_parse_save_summary_export_delete_flow(
    web_client: TestClient,
    app_container: AppContainer,
    gemini_requests: list[httpx.Request],
) -> None:
    login = web_client.post(
        "/login", data={"passcode": app_container.settings.app_passcode}
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"
    assert web_client.get("/").status_code == 200
    files = {"file": ("receipt.jpg", IMAGE, "image/jpeg")}
    parsed = web_client.post("/api/receipts/parse", files=files)
    assert parsed.status_code == 200
    draft = parsed.json()
    assert draft["warnings"]
    assert len(gemini_requests) == 1
    inline = json.loads(gemini_requests[0].content)["contents"][0]["parts"][1][
        "inline_data"
    ]
    assert inline == {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(IMAGE).decode(),
    }
    assert app_container.dynamo.scan() == []
    assert (
        app_container.s3.client.list_objects_v2(
            Bucket=app_container.settings.bucket_name
        )["KeyCount"]
        == 0
    )

    # Mirror confirmation edits, preserving a nonblocking total mismatch.
    payload = {
        "client_token": str(uuid4()),
        "date": draft["date"],
        "store": "西松屋 本店",
        "total": draft["total"],
        "items": [
            {key: item[key] for key in ("name", "price", "category")}
            for item in draft["items"]
        ],
    }
    payload["items"][1].update(name="ハンカチ（修正）", category="child")
    saved = web_client.post(
        "/api/receipts", data={"payload": json.dumps(payload)}, files=files
    )
    assert saved.status_code == 201
    receipt_id = saved.json()["receipt_id"]
    image_key = f"receipts/2026/08/{receipt_id}.jpg"
    assert app_container.s3.get_image(image_key) == IMAGE
    retry = web_client.post(
        "/api/receipts", data={"payload": json.dumps(payload)}, files=files
    )
    assert retry.status_code == 201
    assert retry.json() == saved.json()

    summary_response = web_client.get("/api/summary?month=2026-08")
    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["receipt_count"] == 1
    assert summary["totals"] == {"child": 1738, "couple": 0}
    assert summary["daily"] == [{"date": draft["date"], "child": 1738, "couple": 0}]
    assert summary["receipts"] == [
        {
            "receipt_id": receipt_id,
            "date": payload["date"],
            "store": payload["store"],
            "total": payload["total"],
            "subtotals": {"child": 1738, "couple": 0, "excluded": -100},
            "items": [
                {"seq": seq, **item} for seq, item in enumerate(payload["items"], 1)
            ],
        }
    ]
    for month in ("2026-08", "all"):
        exported = web_client.get("/export.csv", params={"month": month})
        assert exported.status_code == 200
        assert exported.content.startswith(codecs.BOM_UTF8)
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert rows == [
            {
                "receipt_id": receipt_id,
                "date": payload["date"],
                "store": payload["store"],
                "item": item["name"],
                "price": str(item["price"]),
                "category": item["category"],
            }
            for item in payload["items"]
        ]

    deleted = web_client.delete(f"/api/receipts/{receipt_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": receipt_id}
    empty = web_client.get("/api/summary?month=2026-08")
    assert empty.status_code == 200
    assert empty.json() == {
        "month": "2026-08",
        "daily": [],
        "totals": {"child": 0, "couple": 0},
        "receipt_count": 0,
        "receipts": [],
    }
    assert app_container.dynamo.scan() == []
    assert app_container.s3.get_image(image_key) == IMAGE


@pytest.mark.parametrize("with_image", [False, True])
def test_manual_confirmation_save_summary_export_delete_flow(
    logged_in_client: TestClient, app_container: AppContainer, with_image: bool
) -> None:
    files = {"file": ("receipt.jpg", IMAGE, "image/jpeg")} if with_image else {}
    if with_image:
        with patch.object(
            app_container.gemini, "parse_receipt", side_effect=GeminiParseError()
        ):
            failed = logged_in_client.post("/api/receipts/parse", files=files)
        assert failed.status_code == 502
    assert app_container.dynamo.scan() == []
    today = now_jst().date().isoformat()
    payload = {
        "client_token": str(uuid4()),
        "date": today,
        "store": "",
        "total": 0,
        "items": [{"name": "", "price": 0, "category": "couple"}],
    }
    saved = logged_in_client.post(
        "/api/receipts", data={"payload": json.dumps(payload)}, files=files
    )
    assert saved.status_code == 201
    response = logged_in_client.get("/api/summary", params={"month": today[:7]})
    assert response.status_code == 200
    assert response.json()["receipts"][0]["items"] == [
        {"seq": 1, **payload["items"][0]}
    ]
    assert response.json()["receipts"][0]["subtotals"] == {
        "child": 0,
        "couple": 0,
        "excluded": 0,
    }
    receipt_id = saved.json()["receipt_id"]
    retry = logged_in_client.post(
        "/api/receipts", data={"payload": json.dumps(payload)}, files=files
    )
    assert retry.status_code == 201
    assert retry.json() == saved.json()
    exported = logged_in_client.get("/export.csv", params={"month": today[:7]})
    assert exported.status_code == 200
    reader = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
    assert reader.fieldnames[0] == "receipt_id"
    assert list(reader) == [
        {
            "receipt_id": receipt_id,
            "date": today,
            "store": "",
            "item": "",
            "price": "0",
            "category": "couple",
        }
    ]
    assert logged_in_client.delete(f"/api/receipts/{receipt_id}").status_code == 200
    assert app_container.dynamo.scan() == []
    assert app_container.s3.client.list_objects_v2(
        Bucket=app_container.settings.bucket_name
    )["KeyCount"] == int(with_image)
