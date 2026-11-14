from fastapi.testclient import TestClient

from duit.main_api import app


def test_healthz_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_database_up() -> None:
    with TestClient(app) as client:  # `with` runs the lifespan, which opens the engine
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}
