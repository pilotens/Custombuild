import pytest
from app import main as main_module
from app.main import app
from app.readiness import DependencyFailure
from fastapi.testclient import TestClient


def test_request_id_is_preserved_when_valid() -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "acceptance-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "acceptance-123"


def test_unsafe_request_id_is_replaced() -> None:
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "bad header value"})

    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["X-Request-ID"] != "bad header value"


def test_readiness_checks_every_required_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module,
        "probe_dependencies",
        lambda _settings: (
            {
                "database": "ok",
                "redis": "ok",
                "object_storage": "ok",
                "rule_engine": "ok",
            },
            [],
        ),
    )
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "version": "0.1.0",
        "database": "ok",
        "redis": "ok",
        "object_storage": "ok",
        "rule_engine": "ok",
    }


def test_readiness_names_failed_dependencies_without_leaking_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "probe_dependencies",
        lambda _settings: (
            {"database": "ok", "redis": "unavailable", "object_storage": "unavailable"},
            [
                DependencyFailure(name="redis", error_type="AuthenticationError"),
                DependencyFailure(name="object_storage", error_type="SecretValueWasHere"),
            ],
        ),
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "status": "not_ready",
            "message": "Required dependencies are unavailable",
            "failed_dependencies": ["redis", "object_storage"],
        }
    }
    assert "SecretValueWasHere" not in response.text
