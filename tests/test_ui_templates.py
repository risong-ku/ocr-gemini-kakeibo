"""Rendered UI contracts and public assets without a JavaScript runtime."""

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from kakeibo.main import create_app

CHART_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js"


class Elements(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def by_id(self, element_id: str) -> dict[str, str | None]:
        return next(attrs for _, attrs in self.tags if attrs.get("id") == element_id)


@pytest.mark.parametrize("path", ["/login", "/", "/dashboard"])
def test_pages_share_mobile_layout_and_only_chart_external_asset(
    logged_in_client: TestClient, path: str
) -> None:
    response = logged_in_client.get(path)
    assert response.status_code == 200
    elements = Elements(response.text)
    assert any(
        tag == "meta"
        and attrs.get("name") == "viewport"
        and attrs.get("content") == "width=device-width, initial-scale=1"
        for tag, attrs in elements.tags
    )
    scripts = [attrs for tag, attrs in elements.tags if tag == "script"]
    assert scripts[0]["src"] == "/static/app.js"
    assert all("src" in script and "defer" in script for script in scripts)
    assets = [
        attrs.get("src") if tag == "script" else attrs.get("href")
        for tag, attrs in elements.tags
        if tag in {"script", "link"}
    ]
    assert "/static/style.css" in assets
    assert [url for url in assets if url and not url.startswith("/")] == (
        [CHART_URL] if path == "/dashboard" else []
    )
    assert "hidden" in elements.by_id("session-expired")
    assert 'href="/login"' in response.text
    ids = [attrs["id"] for _, attrs in elements.tags if "id" in attrs]
    assert len(ids) == len(set(ids))


def test_login_password_and_failure_message(web_client: TestClient) -> None:
    response = web_client.get("/login")
    elements = Elements(response.text)
    assert elements.by_id("login-form")["action"] == "/login"
    assert elements.by_id("login-form")["method"] == "post"
    password = elements.by_id("passcode")
    assert password["type"] == "password"
    assert password["autocomplete"] == "current-password"
    assert "inputmode" not in password
    failed = web_client.post("/login", data={"passcode": "incorrect"})
    assert failed.status_code == 200
    assert Elements(failed.text).by_id("login-error")["role"] == "alert"
    assert "パスコードが違います" in failed.text


def test_upload_has_confirmation_controls(logged_in_client: TestClient) -> None:
    elements = Elements(logged_in_client.get("/").text)
    for hook in (
        "upload-form",
        "upload-view",
        "manual-entry",
        "parsing-view",
        "confirmation-view",
        "confirmation-form",
        "confirmation-fields",
        "confirmation-heading",
        "date",
        "store",
        "total",
        "warnings",
        "total-warning",
        "uncertain-count",
        "items",
        "item-template",
        "collapse-items",
        "add-item",
        "subtotals",
        "child-subtotal",
        "couple-subtotal",
        "excluded-subtotal",
        "items-total",
        "save",
        "restart",
        "save-status",
    ):
        assert elements.by_id(hook)
    file_input = elements.by_id("file")
    assert file_input["type"] == "file"
    assert file_input["accept"] == "image/*"
    # No capture attribute: iOS must offer the photo library as well as the camera.
    assert "capture" not in file_input
    assert file_input["name"] == "file"
    assert "hidden" not in elements.by_id("upload-view")
    for view in ("parsing-view", "confirmation-view"):
        assert "hidden" in elements.by_id(view)
    assert elements.by_id("confirmation-form")["action"] == "/api/receipts"
    assert elements.by_id("confirmation-form")["enctype"] == "multipart/form-data"
    assert elements.by_id("date")["type"] == "date"
    assert "required" in elements.by_id("date")
    assert "required" not in elements.by_id("store")
    amount_inputs = [
        attrs
        for tag, attrs in elements.tags
        if tag == "input" and attrs.get("inputmode") == "numeric"
    ]
    assert len(amount_inputs) == 2
    assert all(attrs["pattern"] == "-?[0-9]+" for attrs in amount_inputs)
    categories = [
        attrs["data-category"] for _, attrs in elements.tags if "data-category" in attrs
    ]
    assert categories == ["child", "couple", "excluded"]
    for hook in ("item-name", "item-price", "delete-item", "toggle-price-sign"):
        assert any(
            hook in (attrs.get("class") or "").split() for _, attrs in elements.tags
        )


def test_upload_collapse_button_and_toggle_labels(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/")
    assert Elements(response.text).by_id("collapse-items")["type"] == "button"
    assert "1行にまとめる" in response.text
    script = logged_in_client.get("/static/app.js").text
    assert "1行にまとめる" in script
    assert "内訳に戻す" in script


def test_upload_manual_entry_button(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/")
    assert Elements(response.text).by_id("manual-entry")["type"] == "button"
    assert "手入力で登録" in response.text
    script = logged_in_client.get("/static/app.js").text
    assert 'byId("manual-entry").addEventListener("click"' in script


def test_dashboard_excluded_labels_and_muted_style(
    logged_in_client: TestClient,
) -> None:
    script = logged_in_client.get("/static/dashboard.js").text
    assert 'excluded: "対象外"' in script
    assert "receipt.subtotals[category]" in script
    assert 'category === "excluded" && amount === 0' in script
    assert 'subtotal.className = "excluded"' in script
    assert 'item.category === "excluded") entry.className = "excluded"' in script
    css = logged_in_client.get("/static/style.css").text
    assert ".receipt-row .excluded { color:" in css


def test_dashboard_has_month_summary_chart_export_and_receipts(
    logged_in_client: TestClient,
) -> None:
    response = logged_in_client.get("/dashboard")
    elements = Elements(response.text)
    for hook in (
        "previous-month",
        "month",
        "next-month",
        "child-total",
        "couple-total",
        "daily-chart",
        "export-month",
        "export-all",
        "receipts",
        "dashboard-status",
        "retry-summary",
        "summary-content",
        "chart-status",
    ):
        assert elements.by_id(hook)
    assert elements.by_id("export-all")["href"] == "/export.csv?month=all"
    assert elements.by_id("daily-chart")["role"] == "img"
    assert "◀" in response.text and "▶" in response.text
    assert 'href="/"' in response.text
    assert [attrs["src"] for tag, attrs in elements.tags if tag == "script"] == [
        "/static/app.js",
        CHART_URL,
        "/static/dashboard.js",
    ]


@pytest.mark.parametrize("asset", ["app.js", "dashboard.js", "style.css"])
def test_static_assets_are_public_without_configuration(asset: str) -> None:
    # No lifespan: public static requests must not resolve auth or AWS settings.
    client = TestClient(create_app(), follow_redirects=False)
    try:
        response = client.get(f"/static/{asset}")
        assert response.status_code == 200
        assert len(response.content) > 100
        if asset == "app.js":
            assert "function discountCategoryWarnings(items)" in response.text
            assert "の品目がありません" in response.text
            # The live items-sum warning carries the same tax hint as the server,
            # and the duplicated server warning is filtered out on the client.
            assert "税抜表示の可能性" in response.text
            assert 'warning.startsWith("品目合計")' in response.text
        assert "location" not in response.headers
        assert (
            "javascript" in response.headers["content-type"]
            if asset.endswith(".js")
            else "text/css" in response.headers["content-type"]
        )
    finally:
        client.close()
