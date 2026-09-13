"""HTML contracts and application lifecycle with no external network."""

from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from mangum import Mangum

from kakeibo.container import AppContainer
from kakeibo.main import create_app, handler


def test_login_form_and_failed_passcode(web_client: TestClient) -> None:
    response = web_client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'action="/login"' in response.text
    assert 'name="passcode"' in response.text
    assert 'autocomplete="current-password"' in response.text
    response = web_client.post("/login", data={"passcode": "wrong"})
    assert response.status_code == 200
    assert "パスコードが違います" in response.text
    assert "set-cookie" not in response.headers
    assert web_client.get("/").status_code == 303


def test_successful_login_sets_signed_cookie(
    web_client: TestClient, app_container: AppContainer
) -> None:
    response = web_client.post(
        "/login", data={"passcode": app_container.settings.app_passcode}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    for attribute in (
        "HttpOnly",
        "Secure",
        "SameSite=lax",
        "Max-Age=7776000",
        "Path=/",
    ):
        assert attribute in cookie
    assert app_container.auth.verify_token(web_client.cookies.get("session"))


@pytest.mark.parametrize(
    "path,hooks",
    [
        (
            "/",
            ('id="upload-form"', 'name="file"', 'id="confirmation-form"', 'id="items"'),
        ),
        (
            "/dashboard",
            (
                'id="month"',
                'id="daily-chart"',
                'id="receipts"',
                "/export.csv?month=all",
            ),
        ),
    ],
)
def test_authenticated_pages_render(
    logged_in_client: TestClient, path: str, hooks: tuple[str, ...]
) -> None:
    response = logged_in_client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for hook in hooks:
        assert hook in response.text


def test_settings_injection_and_local_cookie(app_container: AppContainer) -> None:
    settings = replace(
        app_container.settings, cookie_secure=False, app_passcode="different passphrase"
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        response = client.post("/login", data={"passcode": settings.app_passcode})
        assert response.status_code == 303
        assert "Secure" not in response.headers["set-cookie"]
        assert client.get("/").status_code == 200


def test_factory_and_health_do_not_construct_external_clients() -> None:
    with (
        patch("boto3.client", side_effect=AssertionError("AWS forbidden")),
        patch("boto3.resource", side_effect=AssertionError("AWS forbidden")),
        patch(
            "httpx.HTTPTransport.handle_request",
            side_effect=AssertionError("network forbidden"),
        ),
    ):
        app = create_app()
        assert isinstance(handler, Mangum)
        assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_lifespan_closes_owned_http_client(app_container: AppContainer) -> None:
    app = create_app(app_container.settings)
    with TestClient(app):
        client = app.state.container.gemini.client
        assert not client.is_closed
        assert app.state.container.auth.settings == app_container.settings
    assert client.is_closed


def test_injected_container_remains_caller_owned(
    web_client: TestClient, app_container: AppContainer
) -> None:
    with TestClient(create_app(container=app_container)):
        client = app_container.gemini.client
    assert not client.is_closed


def test_repeated_lifespans_create_fresh_owned_clients(
    app_container: AppContainer,
) -> None:
    app = create_app(app_container.settings)
    with TestClient(app):
        first = app.state.container.gemini.client
    with TestClient(app):
        second = app.state.container.gemini.client
        assert first.is_closed
        assert second is not first
        assert not second.is_closed
    assert second.is_closed


def test_handler_with_function_url_health_event(settings_env: dict[str, str]) -> None:
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/healthz",
        "rawQueryString": "",
        "headers": {"host": "testserver"},
        "requestContext": {
            "domainName": "testserver",
            "stage": "$default",
            "http": {
                "method": "GET",
                "path": "/healthz",
                "sourceIp": "127.0.0.1",
                "protocol": "HTTP/1.1",
            },
        },
        "body": None,
        "isBase64Encoded": False,
    }
    with (
        patch("boto3.client", side_effect=AssertionError("AWS forbidden")),
        patch("boto3.resource", side_effect=AssertionError("AWS forbidden")),
    ):
        for _ in range(2):
            result = handler(event, object())
            assert result["statusCode"] == 200
            assert result["body"] == '{"status":"ok"}'


def test_separate_factories_do_not_duplicate_routes(
    app_container: AppContainer,
) -> None:
    first = create_app(app_container.settings)
    second = create_app(app_container.settings)
    schema = first.openapi()
    assert schema == second.openapi()
    operations = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    ]
    assert len(operations) == len(set(operations))
    assert set(schema["paths"]["/api/receipts"]) == {"post"}
    assert first.state.container is not second.state.container
