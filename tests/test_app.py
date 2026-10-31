from fastapi.testclient import TestClient

from app.main import app


def test_app_serves_api_docs() -> None:
    response = TestClient(app).get("/docs")
    assert response.status_code == 200
