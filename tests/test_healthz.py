"""Tests for the /healthz endpoint."""

from fastapi.testclient import TestClient

from kakeibo.main import create_app


def test_healthz_returns_200_and_status_ok() -> None:
    # Arrange
    client = TestClient(create_app())

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
