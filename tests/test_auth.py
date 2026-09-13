"""Configuration and signed-cookie policy without application router wiring."""

import hashlib
import logging
from dataclasses import replace
from http.cookies import SimpleCookie
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from kakeibo.auth import SessionAuth
from kakeibo.config import load_settings

TOKEN_ISSUED_AT = 1_800_000_000
NINETY_DAYS_SECONDS = 7_776_000


def test_config_reads_documented_environment_and_hides_secrets(
    settings_env: dict[str, str],
) -> None:
    settings = load_settings()

    assert settings.table_name == settings_env["KAKEIBO_TABLE_NAME"]
    assert settings.bucket_name == settings_env["KAKEIBO_BUCKET_NAME"]
    assert settings.aws_region == settings_env["AWS_REGION"]
    assert settings.session_secret == settings_env["SESSION_SECRET"]
    assert settings.app_passcode == settings_env["APP_PASSCODE"]
    assert settings.gemini_api_key == settings_env["GEMINI_API_KEY"]
    for key in ("SESSION_SECRET", "APP_PASSCODE", "GEMINI_API_KEY"):
        assert settings_env[key] not in repr(settings)


def test_config_optional_values_use_spec_defaults(
    settings_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("KAKEIBO_TABLE_NAME", "AWS_REGION", "COOKIE_SECURE"):
        monkeypatch.delenv(key)

    settings = load_settings()

    assert settings.table_name == "kakeibo"
    assert settings.aws_region == "ap-northeast-1"
    assert settings.cookie_secure is True


@pytest.mark.parametrize(
    "key", ["KAKEIBO_BUCKET_NAME", "SESSION_SECRET", "GEMINI_API_KEY"]
)
@pytest.mark.parametrize("missing", [True, False])
def test_config_missing_required_value_fails_without_secret_values(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    missing: bool,
) -> None:
    if missing:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, "")

    with pytest.raises(ValueError, match=key) as error:
        load_settings()

    assert settings_env["SESSION_SECRET"] not in str(error.value)


@pytest.mark.parametrize(
    "passcode,warns",
    [("", True), ("a" * 11, True), ("a" * 12, False), ("あ" * 12, False)],
)
def test_config_passphrase_length_is_warn_only(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    passcode: str,
    warns: bool,
) -> None:
    monkeypatch.setenv("APP_PASSCODE", passcode)

    with caplog.at_level(logging.WARNING):
        settings = load_settings()

    assert settings.app_passcode == passcode
    assert ("APP_PASSCODE" in caplog.text) is warns
    if passcode:
        assert passcode not in caplog.text


def test_config_unset_passcode_is_not_silently_defaulted(
    settings_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APP_PASSCODE")

    with pytest.raises(ValueError, match="APP_PASSCODE"):
        load_settings()


@pytest.mark.parametrize(
    "value,secure", [("false", False), ("true", True), ("FALSE", False)]
)
def test_cookie_flags_follow_configuration(
    settings_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    secure: bool,
) -> None:
    monkeypatch.setenv("COOKIE_SECURE", value)
    auth = SessionAuth(load_settings())
    response = Response()

    auth.set_cookie(response)

    cookie = SimpleCookie(response.headers["set-cookie"])["session"]
    assert auth.verify_token(cookie.value)
    assert bool(cookie["secure"]) is secure
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["max-age"] == str(NINETY_DAYS_SECONDS)
    assert cookie["path"] == "/"


def test_config_invalid_cookie_secure_fails(
    settings_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COOKIE_SECURE", "typo")

    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        load_settings()


def test_session_payload_matches_spec(
    auth: SessionAuth, settings_env: dict[str, str]
) -> None:
    token = auth.create_token()

    payload = URLSafeTimedSerializer(settings_env["SESSION_SECRET"]).loads(token)

    assert payload == {
        "auth": True,
        "pv": hashlib.sha256(settings_env["APP_PASSCODE"].encode()).hexdigest()[:8],
    }
    assert auth.verify_token(token)


@pytest.mark.parametrize(
    "elapsed,valid",
    [
        (0, True),
        (NINETY_DAYS_SECONDS, True),
        (NINETY_DAYS_SECONDS + 1, False),
        (-1, False),
    ],
)
def test_session_age_boundary(auth: SessionAuth, elapsed: int, valid: bool) -> None:
    with patch("itsdangerous.timed.time.time", return_value=TOKEN_ISSUED_AT):
        token = auth.create_token()

    with patch("itsdangerous.timed.time.time", return_value=TOKEN_ISSUED_AT + elapsed):
        result = auth.verify_token(token)

    assert result is valid


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {},
        {"auth": True},
        {"auth": False, "pv": "current"},
        {"auth": 1, "pv": "current"},
        {"auth": "true", "pv": "current"},
        {"auth": True, "pv": []},
        {"auth": True, "pv": "wrong"},
        {"auth": True, "pv": "あ"},
    ],
)
def test_session_invalid_signed_payload_is_rejected(
    auth: SessionAuth, settings_env: dict[str, str], payload: Any
) -> None:
    if isinstance(payload, dict) and payload.get("pv") == "current":
        payload = {
            **payload,
            "pv": hashlib.sha256(settings_env["APP_PASSCODE"].encode()).hexdigest()[:8],
        }
    token = URLSafeTimedSerializer(settings_env["SESSION_SECRET"]).dumps(payload)

    assert auth.verify_token(token) is False


def test_session_missing_malformed_tampered_and_rotated_are_rejected(
    auth: SessionAuth,
) -> None:
    token = auth.create_token()
    settings = load_settings()

    for invalid in (None, "", "garbage", "x" + token):
        assert auth.verify_token(invalid) is False
    assert (
        SessionAuth(replace(settings, app_passcode="changed passphrase")).verify_token(
            token
        )
        is False
    )
    assert (
        SessionAuth(replace(settings, session_secret="changed secret")).verify_token(
            token
        )
        is False
    )


def test_passcode_repeated_failures_do_not_lock_out(
    auth: SessionAuth, settings_env: dict[str, str]
) -> None:
    for _ in range(20):
        assert auth.verify_passcode("incorrect") is False

    assert auth.verify_passcode(settings_env["APP_PASSCODE"]) is True
    assert auth.verify_passcode(settings_env["APP_PASSCODE"] + " ") is False
    unicode_auth = SessionAuth(replace(load_settings(), app_passcode="あ" * 12))
    assert unicode_auth.verify_passcode("あ" * 12) is True
    assert unicode_auth.verify_passcode("い" * 12) is False


@pytest.mark.parametrize("path", ["/api/receipts", "/api/summary"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_auth_missing_session_returns_api_401(
    auth_client: TestClient, path: str, method: str
) -> None:
    response = auth_client.request(method, path)

    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/dashboard",
        "/export.csv",
        "/login/extra",
        "/healthz/extra",
        "/staticity/app.js",
        "/static",
        "/docs",
        "/openapi.json",
    ],
)
def test_auth_missing_session_redirects_protected_paths(
    auth_client: TestClient, path: str
) -> None:
    response = auth_client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/healthz", "/login", "/static/app.js"])
def test_auth_public_paths_allow_invalid_cookie(
    auth_client: TestClient, path: str
) -> None:
    auth_client.cookies.set("session", "invalid")

    response = auth_client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/", "/export.csv", "/api/receipts"])
def test_auth_valid_cookie_allows_protected_paths(
    auth_client: TestClient, auth: SessionAuth, path: str
) -> None:
    auth_client.cookies.set("session", auth.create_token())

    response = auth_client.get(path)

    assert response.status_code == 200


def test_auth_expired_cookie_returns_401(
    auth_client: TestClient, auth: SessionAuth
) -> None:
    with patch("itsdangerous.timed.time.time", return_value=TOKEN_ISSUED_AT):
        auth_client.cookies.set("session", auth.create_token())

    with patch(
        "itsdangerous.timed.time.time",
        return_value=TOKEN_ISSUED_AT + NINETY_DAYS_SECONDS + 1,
    ):
        response = auth_client.get("/api/receipts")

    assert response.status_code == 401
