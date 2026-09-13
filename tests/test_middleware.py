"""Authentication policy on the real application, before validation."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from kakeibo.auth import SESSION_MAX_AGE
from kakeibo.container import AppContainer


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/", 303),
        ("GET", "/dashboard", 303),
        ("GET", "/export.csv?month=all", 303),
        ("GET", "/docs", 303),
        ("POST", "/api/receipts/parse", 401),
        ("POST", "/api/receipts", 401),
        ("DELETE", "/api/receipts/missing", 401),
        ("GET", "/api/summary?month=bad", 401),
    ],
)
@pytest.mark.parametrize("cookie", [None, "tampered", "expired", "old-passcode"])
def test_auth_gates_pages_and_apis(
    web_client: TestClient,
    app_container: AppContainer,
    method: str,
    path: str,
    status: int,
    cookie: str | None,
) -> None:
    if cookie == "expired":
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=1):
            cookie = app_container.auth.create_token()
    elif cookie == "old-passcode":
        cookie = app_container.auth.serializer.dumps({"auth": True, "pv": "obsolete"})
    if cookie is not None:
        web_client.cookies.set("session", cookie)
    response = web_client.request(method, path)
    assert response.status_code == status
    if status == 401:
        assert response.json() == {"detail": "not authenticated"}
        return
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    "path,status", [("/healthz", 200), ("/login", 200), ("/static/missing.css", 404)]
)
def test_public_paths_bypass_invalid_cookie(
    web_client: TestClient, path: str, status: int
) -> None:
    web_client.cookies.set("session", "bad")
    response = web_client.get(path)
    assert response.status_code == status
    assert "location" not in response.headers


def test_valid_cookie_is_checked_with_ninety_day_expiry(
    logged_in_client: TestClient, app_container: AppContainer
) -> None:
    with patch.object(
        app_container.auth.serializer,
        "loads",
        wraps=app_container.auth.serializer.loads,
    ) as loads:
        assert logged_in_client.get("/dashboard").status_code == 200
    assert loads.call_args.kwargs["max_age"] == SESSION_MAX_AGE
