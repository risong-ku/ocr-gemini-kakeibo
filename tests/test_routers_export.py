"""CSV download encoding, headers, filtering, and validation."""

import codecs
import csv
import io
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kakeibo.container import AppContainer


@pytest.mark.parametrize("month", ["2026-08", "all", "2026-09"])
def test_csv_headers_bom_filter_and_quoting(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
    month: str,
) -> None:
    receipt_payload["store"] = '店,"支店"'
    receipt_payload["items"][0]["name"] = 'おむつ,"M"\n特価'
    current_id = app_container.receipts.save_receipt(receipt_payload, b"image")
    receipt_payload.update(date="2026-07-31", client_token=str(uuid4()))
    prior_id = app_container.receipts.save_receipt(receipt_payload, b"image")
    response = logged_in_client.get("/export.csv", params={"month": month})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="kakeibo_{month}.csv"'
    )
    assert response.content.startswith(codecs.BOM_UTF8)
    assert response.content.count(codecs.BOM_UTF8) == 1
    reader = csv.DictReader(
        io.StringIO(response.content.decode("utf-8-sig"), newline="")
    )
    assert reader.fieldnames == [
        "receipt_id",
        "date",
        "store",
        "item",
        "price",
        "category",
    ]
    rows = list(reader)
    ids = (
        [prior_id, current_id]
        if month == "all"
        else [current_id]
        if month == "2026-08"
        else []
    )
    assert [row["receipt_id"] for row in rows] == [
        receipt_id for receipt_id in ids for _ in range(3)
    ]
    if not ids:
        return
    assert rows[0]["store"] == '店,"支店"'
    assert rows[0]["item"] == 'おむつ,"M"\n特価'
    assert rows[2]["price"] == "-100"
    assert rows[2]["category"] == "excluded"


@pytest.mark.parametrize(
    "month", [None, "", "ALL", "2026-8", "2026-00", "2026-13", "2026-08\n"]
)
def test_csv_bad_month_is_422_before_scan(
    logged_in_client: TestClient, app_container: AppContainer, month: str | None
) -> None:
    with patch.object(app_container.dynamo, "scan") as scan:
        response = logged_in_client.get(
            "/export.csv", params={} if month is None else {"month": month}
        )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["query", "month"]
    scan.assert_not_called()
