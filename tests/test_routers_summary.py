"""Monthly summary JSON and query validation contracts."""

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from kakeibo.container import AppContainer


def test_summary_returns_confirmed_receipts_and_item_totals(
    logged_in_client: TestClient,
    app_container: AppContainer,
    receipt_payload: dict[str, Any],
) -> None:
    receipt_id = app_container.receipts.save_receipt(receipt_payload, b"image")
    response = logged_in_client.get("/api/summary", params={"month": "2026-08"})
    assert response.status_code == 200
    assert response.json() == {
        "month": "2026-08",
        "daily": [{"date": "2026-08-25", "child": 1480, "couple": 258}],
        "totals": {"child": 1480, "couple": 258},
        "receipt_count": 1,
        "receipts": [
            {
                "receipt_id": receipt_id,
                "date": "2026-08-25",
                "store": "西松屋",
                "total": 1738,
                "subtotals": {"child": 1480, "couple": 258, "excluded": -100},
                "items": [
                    {"seq": seq, **item}
                    for seq, item in enumerate(receipt_payload["items"], 1)
                ],
            }
        ],
    }
    assert logged_in_client.get("/api/summary?month=2026-09").json() == {
        "month": "2026-09",
        "daily": [],
        "totals": {"child": 0, "couple": 0},
        "receipt_count": 0,
        "receipts": [],
    }


@pytest.mark.parametrize(
    "month", [None, "", "all", "2026-8", "2026-00", "2026-13", "26-08", "2026-08\n"]
)
def test_invalid_month_returns_422_without_scan(
    logged_in_client: TestClient, app_container: AppContainer, month: str | None
) -> None:
    with patch.object(app_container.dynamo, "scan") as scan:
        response = logged_in_client.get(
            "/api/summary", params={} if month is None else {"month": month}
        )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "month"]
    scan.assert_not_called()
